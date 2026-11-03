"""部署相关的仓储层。

单独成文件而不是塞进 ``repository.py``：那个文件已经有 1400+ 行、服务的是「条目/
评测/队列」那条流水线；部署是另一条业务线，混进去只会让两边互相干扰（也更容易
在改一处时误伤另一处的查询）。

约定：**仓储层不碰凭据明文**。它只知道 ``credential`` 是一段密文，加解密在
:mod:`fissue.deploy.secretbox`，解出来的明文只在建立 SSH 连接那一瞬间存在。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update

from ..deploy.schema import AuthMethod, DeployStatus, HostSpec, StepStatus
from ..logging_setup import get_logger
from .db import Database
from .tables import (
    DeployEventRow,
    DeployGuideRow,
    DeployHostRow,
    DeploymentRow,
    DeploymentStepRow,
    LLMUsageRow,
)

log = get_logger(__name__)

#: 每步只留尾部这么多字符。远端 `pip install` 的日志轻松几十 MB，
#: 整段落库既撑爆表又没人看；要看全量应当 SSH 上去读日志文件。
OUTPUT_TAIL_CHARS = 8000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tail(text: str, limit: int = OUTPUT_TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"…（前 {len(text) - limit} 字符已省略）\n" + text[-limit:]


class DeployRepository:
    """主机、部署、步骤与审计事件。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- 主机 -------------------------------------------------------------

    def upsert_host(
        self,
        *,
        name: str,
        address: str,
        credential: str,
        port: int = 22,
        user: str = "root",
        auth_method: AuthMethod | str = AuthMethod.KEY,
        app_root: str | None = None,
        enabled: bool = True,
    ) -> int:
        """新增或覆盖一台主机。同名时**必须**给新凭据（不做「留空则沿用」的隐式行为：
        凭据这种字段最忌讳「我以为是新的，其实是旧的」）。
        """
        method = auth_method.value if isinstance(auth_method, AuthMethod) else str(auth_method)
        with self.db.session() as s:
            row = s.scalar(select(DeployHostRow).where(DeployHostRow.name == name))
            if row is None:
                row = DeployHostRow(name=name, address=address, credential=credential)
                if app_root:
                    row.app_root = app_root
                s.add(row)
            row.address = address
            row.port = port
            row.user = user
            row.auth_method = method
            row.credential = credential
            row.enabled = enabled
            if app_root:
                row.app_root = app_root
            s.flush()
            return int(row.id)

    def get_host(self, name: str) -> DeployHostRow | None:
        with self.db.session() as s:
            row = s.scalar(select(DeployHostRow).where(DeployHostRow.name == name))
            if row is not None:
                s.expunge(row)
            return row

    def get_host_by_id(self, host_id: int) -> DeployHostRow | None:
        with self.db.session() as s:
            row = s.get(DeployHostRow, host_id)
            if row is not None:
                s.expunge(row)
            return row

    def list_hosts(self, *, enabled_only: bool = False) -> list[DeployHostRow]:
        stmt = select(DeployHostRow).order_by(DeployHostRow.name)
        if enabled_only:
            stmt = stmt.where(DeployHostRow.enabled.is_(True))
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            for row in rows:
                s.expunge(row)
            return rows

    def remove_host(self, name: str) -> bool:
        """删除主机。部署历史随外键级联删除——但**审计事件会一起没**，
        所以 CLI 在删之前会先提示这一点（见 ``cli/deploy.py``）。
        """
        with self.db.session() as s:
            row = s.scalar(select(DeployHostRow).where(DeployHostRow.name == name))
            if row is None:
                return False
            s.delete(row)
            return True

    def set_host_enabled(self, name: str, enabled: bool) -> bool:
        with self.db.session() as s:
            row = s.scalar(select(DeployHostRow).where(DeployHostRow.name == name))
            if row is None:
                return False
            row.enabled = enabled
            return True

    def remember_host_fingerprint(self, host_id: int, fingerprint: str) -> None:
        """TOFU：首次连接成功后把指纹钉住，之后不匹配一律拒连。"""
        with self.db.session() as s:
            s.execute(
                update(DeployHostRow)
                .where(DeployHostRow.id == host_id)
                .values(host_fingerprint=fingerprint)
            )

    def touch_host(self, host_id: int) -> None:
        with self.db.session() as s:
            s.execute(
                update(DeployHostRow)
                .where(DeployHostRow.id == host_id)
                .values(last_seen_at=_now())
            )

    # -- 部署记录 ---------------------------------------------------------

    def create_deployment(
        self,
        *,
        host_id: int,
        repo_slug: str,
        ref: str,
        commit_sha: str | None,
        plan: dict[str, Any],
        app_root: str,
        previous_release: str | None = None,
        status: DeployStatus = DeployStatus.PLANNING,
    ) -> int:
        with self.db.session() as s:
            row = DeploymentRow(
                host_id=host_id,
                repo_slug=repo_slug,
                ref=ref,
                commit_sha=commit_sha,
                plan_json=plan,
                app_root=app_root,
                previous_release=previous_release,
                status=status.value,
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def get_deployment(self, deployment_id: int) -> DeploymentRow | None:
        with self.db.session() as s:
            row = s.get(DeploymentRow, deployment_id)
            if row is not None:
                s.expunge(row)
            return row

    def latest_deployment(self, *, host_id: int, repo_slug: str) -> DeploymentRow | None:
        """该仓库在该主机上的最近一次**成功**部署——回滚点就是它的 release。"""
        stmt = (
            select(DeploymentRow)
            .where(
                DeploymentRow.host_id == host_id,
                DeploymentRow.repo_slug == repo_slug,
                DeploymentRow.status == DeployStatus.SUCCESS.value,
            )
            .order_by(DeploymentRow.id.desc())
            .limit(1)
        )
        with self.db.session() as s:
            row = s.scalar(stmt)
            if row is not None:
                s.expunge(row)
            return row

    def list_deployments(
        self,
        *,
        host_id: int | None = None,
        repo_slug: str | None = None,
        limit: int = 50,
    ) -> list[DeploymentRow]:
        stmt = select(DeploymentRow).order_by(DeploymentRow.id.desc()).limit(limit)
        if host_id is not None:
            stmt = stmt.where(DeploymentRow.host_id == host_id)
        if repo_slug:
            stmt = stmt.where(DeploymentRow.repo_slug == repo_slug)
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            for row in rows:
                s.expunge(row)
            return rows

    def set_status(
        self,
        deployment_id: int,
        status: DeployStatus,
        *,
        error: str | None = None,
    ) -> None:
        """改状态；进入终态时顺手写 ``finished_at``，否则留着 None 表示「还在跑」。"""
        values: dict[str, Any] = {"status": status.value}
        if status.terminal:
            values["finished_at"] = _now()
        if error is not None:
            values["error"] = error
        with self.db.session() as s:
            s.execute(update(DeploymentRow).where(DeploymentRow.id == deployment_id).values(**values))

    def set_release(self, deployment_id: int, *, release: str, previous: str | None) -> None:
        with self.db.session() as s:
            s.execute(
                update(DeploymentRow)
                .where(DeploymentRow.id == deployment_id)
                .values(release=release, previous_release=previous)
            )

    def set_service_info(
        self, deployment_id: int, *, service_name: str | None, url: str | None
    ) -> None:
        with self.db.session() as s:
            s.execute(
                update(DeploymentRow)
                .where(DeploymentRow.id == deployment_id)
                .values(service_name=service_name, url=url)
            )

    def set_health(self, deployment_id: int, health: dict[str, Any]) -> None:
        with self.db.session() as s:
            s.execute(
                update(DeploymentRow).where(DeploymentRow.id == deployment_id).values(health_json=health)
            )

    # -- 步骤 -------------------------------------------------------------

    def start_step(
        self,
        *,
        deployment_id: int,
        seq: int,
        step_id: str,
        kind: str,
        command: str,
    ) -> int:
        with self.db.session() as s:
            row = DeploymentStepRow(
                deployment_id=deployment_id,
                seq=seq,
                step_id=step_id,
                kind=kind,
                command=_tail(command),
                status=StepStatus.RUNNING.value,
                started_at=_now(),
            )
            s.add(row)
            s.flush()
            return int(row.id)

    def finish_step(
        self,
        step_row_id: int,
        *,
        status: StepStatus,
        exit_code: int | None = None,
        output: str = "",
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status.value,
            "exit_code": exit_code,
            "output_tail": _tail(output),
            "finished_at": _now(),
        }
        if error is not None:
            values["error"] = _tail(error)
        with self.db.session() as s:
            s.execute(
                update(DeploymentStepRow).where(DeploymentStepRow.id == step_row_id).values(**values)
            )

    def steps_of(self, deployment_id: int) -> list[DeploymentStepRow]:
        stmt = (
            select(DeploymentStepRow)
            .where(DeploymentStepRow.deployment_id == deployment_id)
            .order_by(DeploymentStepRow.seq)
        )
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            for row in rows:
                s.expunge(row)
            return rows

    # -- 审计事件 ---------------------------------------------------------

    def add_event(self, deployment_id: int, message: str, *, level: str = "info") -> None:
        with self.db.session() as s:
            s.add(DeployEventRow(deployment_id=deployment_id, level=level, message=_tail(message)))

    def events_of(self, deployment_id: int, *, limit: int = 200) -> list[DeployEventRow]:
        stmt = (
            select(DeployEventRow)
            .where(DeployEventRow.deployment_id == deployment_id)
            .order_by(DeployEventRow.id)
            .limit(limit)
        )
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            for row in rows:
                s.expunge(row)
            return rows

    # -- 派生视图 ---------------------------------------------------------

    def host_spec(self, row: DeployHostRow) -> HostSpec:
        """把 ORM 行转成**不含凭据**的只读视图（CLI/Web 都用它，防止误带密文出去）。"""
        return HostSpec(
            name=row.name,
            address=row.address,
            port=row.port,
            user=row.user,
            auth_method=AuthMethod(row.auth_method),
            host_fingerprint=row.host_fingerprint,
            enabled=bool(row.enabled),
        )

    def record_usage(self, usage: Any, *, repo_key: str = "") -> None:
        """把一次 LLM 用量记进 ``llm_usage``。

        部署规划也烧 token，不记的话「今天的用量」会系统性偏低——而预算守卫正是
        按这张表决定要不要停下。
        """
        with self.db.session() as s:
            s.add(
                LLMUsageRow(
                    repo_key=repo_key,
                    purpose=getattr(usage, "purpose", "") or "deploy.plan",
                    model=getattr(usage, "model", ""),
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    cost_usd=float(getattr(usage, "cost_usd", 0.0) or 0.0),
                    success=bool(getattr(usage, "success", True)),
                    estimated=bool(getattr(usage, "estimated", False)),
                )
            )

    # -- 新手引导 ---------------------------------------------------------

    def upsert_guide(
        self,
        *,
        repo_slug: str,
        host: str,
        mode: str,
        stages: list[dict[str, Any]],
        notes: str = "",
        source: str = "ai",
    ) -> int:
        """生成/覆盖一份引导。

        一个 (repo_slug, host) 只留一份：引导描述的是「现在该怎么做」，
        留着旧版本只会让新手照着过期步骤操作。
        """
        with self.db.session() as s:
            row = s.scalar(
                select(DeployGuideRow).where(
                    DeployGuideRow.repo_slug == repo_slug, DeployGuideRow.host == host
                )
            )
            if row is None:
                row = DeployGuideRow(repo_slug=repo_slug, host=host)
                s.add(row)
            row.mode = mode
            row.stages = stages
            row.notes = notes
            row.source = source
            s.flush()
            return int(row.id)

    def get_guide(self, *, repo_slug: str, host: str = "") -> DeployGuideRow | None:
        stmt = select(DeployGuideRow).where(
            DeployGuideRow.repo_slug == repo_slug, DeployGuideRow.host == host
        )
        with self.db.session() as s:
            row = s.scalar(stmt)
            if row is not None:
                s.expunge(row)
            return row

    def latest_guide_for_host(self, host: str) -> DeployGuideRow | None:
        """该主机名下的最新一份引导（页面在没指定仓库时用它兜底）。"""
        stmt = (
            select(DeployGuideRow)
            .where(DeployGuideRow.host == host)
            .order_by(DeployGuideRow.updated_at.desc())
            .limit(1)
        )
        with self.db.session() as s:
            row = s.scalar(stmt)
            if row is not None:
                s.expunge(row)
            return row

    def list_guides(self, *, limit: int = 50) -> list[DeployGuideRow]:
        stmt = select(DeployGuideRow).order_by(DeployGuideRow.updated_at.desc()).limit(limit)
        with self.db.session() as s:
            rows = list(s.scalars(stmt))
            for row in rows:
                s.expunge(row)
            return rows

    def stats(self) -> dict[str, Any]:
        """概览：主机数、各状态部署数（Web 运维页的顶栏用）。"""
        with self.db.session() as s:
            hosts = s.scalar(select(func.count()).select_from(DeployHostRow)) or 0
            by_status = {
                status: int(
                    s.scalar(
                        select(func.count())
                        .select_from(DeploymentRow)
                        .where(DeploymentRow.status == status)
                    )
                    or 0
                )
                for status in (
                    DeployStatus.PLANNING.value,
                    DeployStatus.RUNNING.value,
                    DeployStatus.SUCCESS.value,
                    DeployStatus.FAILED.value,
                    DeployStatus.ROLLED_BACK.value,
                )
            }
        return {"hosts": int(hosts), "by_status": by_status}

    def delete_deployments(self, deployment_ids: list[int]) -> int:
        """删记录（不碰远端文件）。运维排障时用来清理噪声记录。"""
        if not deployment_ids:
            return 0
        with self.db.session() as s:
            result = s.execute(
                delete(DeploymentRow).where(DeploymentRow.id.in_(deployment_ids))
            )
            return int(getattr(result, "rowcount", 0) or 0)