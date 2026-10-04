"""运行时上下文：把配置、存储、适配器、AI、沙盒、验证器组装成可用的对象图。

流水线与 CLI 都通过 :class:`RuntimeContext` 拿到依赖，避免在业务代码里到处
``new`` 客户端、到处读配置。它同时也是**适配器与 LLM 客户端的生命周期管理者**
（异步上下文管理器）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from ..ai.client import BudgetGuard, LLMClient, Usage
from ..ai.evaluator import Evaluator
from ..config import RepoConfig, Settings, load_settings
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..models import Platform, RepoRef
from ..platforms.base import PlatformAdapter
from ..platforms.registry import build_adapter
from ..sandbox.forwarder import SandboxManager
from ..store.db import Database, build_database
from ..store.repository import Repository
from ..verifier.generator import VerifierGenerator
from ..verifier.regression import RegressionGate
from ..verifier.runner import VerifierRunner
from ..workspace import RepoWorkspace

log = get_logger(__name__)


@dataclass
class RuntimeContext:
    """一次运行所需的全部依赖。"""

    settings: Settings
    db: Database
    repo: Repository
    llm: LLMClient
    sandbox: SandboxManager
    evaluator: Evaluator
    verifier: VerifierRunner
    generator: VerifierGenerator
    regression_gate: RegressionGate
    _adapters: dict[str, PlatformAdapter] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # -- 构造 -------------------------------------------------------------

    @classmethod
    def create(cls, settings: Settings | None = None, *, overrides: dict[str, Any] | None = None) -> "RuntimeContext":
        st = settings or load_settings(overrides=overrides)
        db = build_database(st)
        repo = Repository(db)

        llm = LLMClient(st.llm, usage_sink=_make_usage_sink(repo))
        sandbox = SandboxManager(st.sandbox)
        evaluator = Evaluator(llm, repo, st)
        generator = VerifierGenerator(llm, st)
        verifier = VerifierRunner(sandbox, repo, st, client=llm)
        regression_gate = RegressionGate(sandbox, repo, st)

        return cls(
            settings=st,
            db=db,
            repo=repo,
            llm=llm,
            sandbox=sandbox,
            evaluator=evaluator,
            verifier=verifier,
            generator=generator,
            regression_gate=regression_gate,
        )

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: Settings | None = None, **kwargs: Any) -> AsyncIterator["RuntimeContext"]:
        """异步上下文：自动关闭 LLM 客户端与适配器。"""
        ctx = cls.create(settings, **kwargs)
        try:
            yield ctx
        finally:
            await ctx.aclose()

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            try:
                await adapter.aclose()
            except Exception:  # pragma: no cover
                pass
        self._adapters.clear()
        await self.llm.aclose()

    # -- schema -----------------------------------------------------------

    def init_db(self) -> None:
        self.db.create_all()

    # -- 适配器 -----------------------------------------------------------

    async def adapter_for_repo(self, repo: RepoConfig) -> PlatformAdapter:
        """按仓库配置取（并缓存）适配器。"""
        key = f"{repo.platform.value}:{repo.owner}/{repo.name}"
        async with self._lock:
            adapter = self._adapters.get(key)
            if adapter is None:
                adapter = build_adapter(self.settings, repo)
                self._adapters[key] = adapter
                log.debug("创建适配器 %s", key)
            return adapter

    def adapter_for_platform(self, platform: Platform) -> PlatformAdapter:
        """按平台取全局适配器（用于通用操作，如 whoami / fork）。"""
        key = f"platform:{platform.value}"
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = build_adapter(self.settings, platform=platform)
            self._adapters[key] = adapter
        return adapter

    def repo_ref(self, repo: RepoConfig) -> RepoRef:
        return RepoRef(platform=repo.platform, owner=repo.owner, name=repo.name)

    async def resolve_default_branch(self, repo: RepoConfig) -> str:
        """确定 base 分支：配置优先，否则探测。"""
        if repo.base_branch:
            return repo.base_branch
        adapter = await self.adapter_for_repo(repo)
        try:
            branch = await adapter.get_default_branch(self.repo_ref(repo))
            log.info("仓库 %s 默认分支：%s", repo.slug, branch)
            return branch or "main"
        except Exception as exc:
            log.warning("探测默认分支失败（回退 main）：%s", exc)
            return "main"

    # -- 工作区（克隆） ---------------------------------------------------

    async def clone_workspace(
        self,
        repo: RepoConfig,
        *,
        branch: str | None = None,
        depth: int = 1,
    ) -> RepoWorkspace:
        """克隆仓库到本地临时工作区（自动清理，调用方负责 cleanup）。"""
        adapter = await self.adapter_for_repo(repo)
        ref = self.repo_ref(repo)
        target_branch = branch or await self.resolve_default_branch(repo)
        return await asyncio.to_thread(
            RepoWorkspace.clone,
            adapter.clone_url(ref),
            slug=repo.slug,
            branch=target_branch,
            depth=depth,
            token=adapter.token,
            default_branch=target_branch,
        )

    # -- 预算 -------------------------------------------------------------

    def budget_guard(self, repo_label: str = "") -> BudgetGuard:
        """为某个仓库构造预算守卫（当日用量来自 llm_usage 表）。"""
        return BudgetGuard(
            self.settings.llm.budget,
            usage_getter=lambda: self.repo.usage_today(repo_key=repo_label or None),
            repo_label=repo_label,
        )

    def usage_report(self, repo_label: str | None = None) -> dict[str, Any]:
        return self.repo.usage_today(repo_key=repo_label or None)

    # -- 仓库配置 ---------------------------------------------------------

    def repo_config(self, slug: str, platform: Platform | None = None) -> RepoConfig:
        return self.settings.repo(slug, platform)

    def all_repos(self) -> list[RepoConfig]:
        """配置里的仓库；配置为空时返回空列表（调用方可显式传仓库）。"""
        return self.settings.enabled_repos()

    def ensure_repo_row(self, repo: RepoConfig) -> int:
        return self.repo.ensure_repo(
            self.repo_ref(repo),
            base_branch=repo.base_branch,
            enabled=repo.enabled,
            extra={"test_hint": repo.test_hint} if repo.test_hint else None,
        )


def _make_usage_sink(repo: Repository):
    """构造写入 llm_usage 表的回调。"""

    def sink(usage: Usage) -> None:
        repo.record_usage(
            repo_key=usage.purpose.split(":")[0] if ":" in usage.purpose else "",
            purpose=usage.purpose or "unknown",
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            success=usage.success,
        )

    return sink


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def require_repos(ctx: RuntimeContext, specs: list[str] | None, platform: Platform | None = None) -> list[RepoConfig]:
    """解析要处理的仓库列表（CLI/服务共用）。"""
    from ..platforms.registry import resolve_repos

    repos = resolve_repos(ctx.settings, specs)
    if not repos:
        raise ConfigError(
            "没有可处理的仓库：请在 config.yaml 的 repos 中登记，或用 --repo owner/name --platform github 指定"
        )
    return repos
