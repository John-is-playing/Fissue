"""配置文件写回：**只写覆盖层**，不动人手写的那份 config.yaml。

三层来源（优先级从低到高）
==========================

===============  ====================  ================================================
文件              谁写                    内容
===============  ====================  ================================================
``config.yaml``   人（满是注释）          全部配置；程序只**读**它
``config.local.yaml``  本模块（WebUI / onboard）  只放被改过的键，叠加在 config.yaml 之上
``.env``          人 / 本模块（密钥、部署项）  ``FISSUE_*`` 变量与各平台 token
===============  ====================  ================================================

为什么不直接改写 ``config.yaml``：YAML 回写会丢掉**全部注释与排版**，而那份文件的注释本身就是
它的文档（每条配置为什么这么写都写在旁边）。把人的输入当生成物覆盖掉，等于把项目自己的说明
删了。所以本模块有两条硬规矩：

* **只写自己拥有的键**。合并方向是「覆盖层 > config.yaml」，且**列表整体替换**；因此接管
  列表类配置（``repos``）之前必须先做**种子导入**（:func:`seed_repos`）——否则 config.yaml 里
  原有 3 条仓库、覆盖层只写新加的第 4 条，合并后前 3 条会**静默消失**。这是本模块最该防的
  数据丢失，:func:`seed_repos` 的注释记着具体场景。
* **``.env`` 只做逐行改写**（:func:`update_env_text`），不重新序列化整份文件——注释、顺序、
  引号风格与无关行一字不动。密钥写漏一个字符就登不上平台，这里不做任何「美化」。
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_OVERLAY_FILE, _apply_env_overrides, _deep_merge, _read_env_file
from .errors import ConfigError
from .logging_setup import get_logger

log = get_logger(__name__)

#: .env 的赋值行。允许 ``export`` 前缀与行首缩进——两种写法在 shell 与 dotenv 里都合法。
_ENV_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

#: 需要加引号的值：空值之外的空白、``#``、引号、反斜杠。不加引号会被 dotenv 截断或吞注释。
_ENV_NEEDS_QUOTES = re.compile(r"""[\s#'"\\]""")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def read_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """读 YAML 顶层映射；文件不存在或为空 → 空 dict。

    顶层不是映射就报错：那种文件多半是写坏了（比如把整份文件缩进错位），静默当成空会让
    「配置明明写了却不生效」——本项目对这类静默失败一向是响亮报错。
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件解析失败：{p}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射（key: value）：{p}")
    return data


def flatten_leaves(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套配置摊平成 ``a.b.c -> 叶子值``。

    **列表算叶子**：列表在合并时整体替换，逐项比较反而会给出误导性的答案——「这一条来自哪
    一层」在没有键的列表元素里没有意义。
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten_leaves(value, f"{path}."))
        else:
            out[path] = value
    return out


def config_sources(
    *,
    config_path: str | Path | None,
    overlay_path: str | Path | None,
    env_path: str | Path | None,
) -> dict[str, str]:
    """每个配置路径的**生效值来自哪一层**。

    给「设置」页标注来源用：排查「我明明改了为什么没生效」时，能一眼看到是哪一层在压着。
    层名直接写成用户看得懂的东西。
    """
    cfg = read_yaml_mapping(config_path) if config_path else {}
    overlay = read_yaml_mapping(overlay_path) if overlay_path else {}
    env_layer = _apply_env_overrides({}, _read_env_file(Path(env_path))) if env_path else {}

    env_leaves = flatten_leaves(env_layer)
    overlay_leaves = flatten_leaves(overlay)
    cfg_leaves = flatten_leaves(cfg)

    sources: dict[str, str] = {}
    for path in set(cfg_leaves) | set(overlay_leaves) | set(env_leaves):
        if path in env_leaves:
            sources[path] = "env"
        elif path in overlay_leaves:
            sources[path] = DEFAULT_OVERLAY_FILE
        elif path in cfg_leaves:
            sources[path] = "config.yaml"
        else:  # pragma: no cover - 上面的并集保证了至少命中一个
            sources[path] = "默认值"
    return sources


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def write_text_atomic(path: str | Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 ``os.replace``。

    为什么要原子：这个文件是**正在运行的服务的配置**。半截写入（或写到一半被 kill）会留下
    损坏的 YAML，下一次启动直接解析失败——而用户只是点了一下「保存」。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def update_overlay(
    path: str | Path,
    updates: Mapping[str, Any],
    *,
    seed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 ``updates`` 深合并进覆盖层并落盘，返回落盘后的内容。

    :param seed: 首次接管某个列表键时要先铺的底（见 :func:`seed_repos`）。**只用在该键还不
        存在时**（``setdefault``）——已有内容不该被种子盖回去，否则用户的修改会被悄悄回滚。
    """
    merged = dict(read_yaml_mapping(path))
    if seed:
        for key, value in seed.items():
            merged.setdefault(key, value)
    merged = _deep_merge(merged, updates)
    write_text_atomic(
        path, yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )
    return merged


def _env_value(value: str) -> str:
    """.env 里的取值写法。

    空字符串写成 ``KEY=``：dotenv 读成空串，语义最直白，也让人一眼看出「这个键被清掉了」，
    而不是「这个键消失了」。
    """
    if value == "":
        return ""
    if _ENV_NEEDS_QUOTES.search(value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return value


def update_env_text(text: str, values: Mapping[str, str]) -> str:
    """在 .env 文本里就地改/追加键值，**注释、顺序与无关行原样保留**。

    纯函数，便于单测直接断言「改完之后注释还在、其它键没动」。

    只替换**第一个**匹配行吗？不——同名的重复行会全被替换成同一个值。重复键在 .env 里本身
    就是歧义（dotenv 取最后一个），统一成同一值比留着两份更容易理解。
    """
    pending = {str(k): str(v) for k, v in values.items()}
    out: list[str] = []
    for line in text.splitlines():
        match = _ENV_ASSIGN.match(line)
        key = match.group(1) if match else ""
        if key and key in pending:
            out.append(f"{key}={_env_value(pending.pop(key))}")
        else:
            out.append(line)

    if pending:
        while out and not out[-1].strip():
            out.pop()  # 先收掉尾部空行，别在文件末尾堆一片空白
        if out:
            out.append("")
        for key, value in pending.items():
            out.append(f"{key}={_env_value(value)}")
    return "\n".join(out) + "\n"


def update_env_file(path: str | Path, values: Mapping[str, str]) -> None:
    """按 :func:`update_env_text` 的规则改写 .env 文件（不存在则新建）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    write_text_atomic(p, update_env_text(text, values))


# ---------------------------------------------------------------------------
# 仓库列表：种子导入
# ---------------------------------------------------------------------------


def repo_entry(repo: Any) -> dict[str, Any]:
    """单个仓库 → 覆盖层里的条目。

    ``exclude_defaults``：**只记与默认值不同的字段**。覆盖层是叠加层，把 9 个默认值逐条抄一遍
    既难看、又容易和 config.yaml 里的写法打架（两处都写 enabled: true，改起来要改两边）。
    """
    if hasattr(repo, "model_dump"):
        return repo.model_dump(exclude_none=True, exclude_defaults=True, mode="json")
    return dict(repo)


def seed_repos(repos: Iterable[Any]) -> dict[str, Any]:
    """首次接管仓库列表时的种子：把**当前生效的**仓库原样搬进覆盖层。

    为什么必须有它：``repos`` 是列表，合并时**整体替换**。具体会怎么丢——
    config.yaml 里登记了 ``envkit``、``lockkit`` 两条，用户在 WebUI 里加了 ``textkit``；
    如果覆盖层只写 ``textkit`` 一条，加载时覆盖层把整个 ``repos`` 替换掉，前两条**直接消失**：
    界面上下次就只剩一条，抓取也不再碰另外两个仓库，而且没有任何报错。

    所以规则是：第一次写 ``repos`` 之前，先把它铺成当前生效列表 + 本次改动。
    """
    return {"repos": [repo_entry(r) for r in repos]}
