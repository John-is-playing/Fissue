"""适配器注册表：按平台拿到对应实现，并从配置构造实例。

用法::

    from fissue.platforms import build_adapter
    adapter = build_adapter(settings, repo_config)
    async with adapter:
        async for item in adapter.fetch_items(RepoRef(...)):
            ...
"""

from __future__ import annotations

from typing import Callable

from ..config import RepoConfig, Settings
from ..errors import ConfigError
from ..models import Platform, RepoRef
from .atomgit import AtomGitAdapter
from .base import PlatformAdapter
from .gitee import GiteeAdapter
from .github import GitHubAdapter
from .gitlab import GitLabAdapter

ADAPTERS: dict[Platform, type[PlatformAdapter]] = {
    Platform.GITHUB: GitHubAdapter,
    Platform.GITEE: GiteeAdapter,
    Platform.ATOMGIT: AtomGitAdapter,
    Platform.GITLAB: GitLabAdapter,
}


def get_adapter_class(platform: Platform) -> type[PlatformAdapter]:
    try:
        return ADAPTERS[platform]
    except KeyError as exc:  # pragma: no cover
        raise ConfigError(f"不支持的平台：{platform}") from exc


def build_adapter(
    settings: Settings,
    repo: RepoConfig | None = None,
    *,
    platform: Platform | None = None,
    token: str | None = None,
    api_base: str | None = None,
) -> PlatformAdapter:
    """构造适配器。

    ``repo`` 提供平台的全局 token 来源；显式传入的 ``token`` 优先级最高。
    """
    plat = platform or (repo.platform if repo else None)
    if plat is None:
        raise ConfigError("build_adapter 需要 platform 或 repo")
    cls = get_adapter_class(plat)
    return cls(
        token=token if token is not None else settings.tokens.get(plat),
        api_base=api_base or (repo.api_base if repo else None),
        per_page=50,
        max_retries=settings.llm.max_retries,
    )


def parse_repo_ref(spec: str, platform: Platform | None = None) -> RepoRef:
    """把 ``owner/name`` 或 ``platform:owner/name`` 解析为 RepoRef。"""
    text = spec.strip()
    plat = platform
    if ":" in text and "/" in text and text.index(":") < text.index("/"):
        prefix, _, rest = text.partition(":")
        try:
            plat = Platform(prefix.lower())
            text = rest
        except ValueError:
            pass
    if plat is None:
        raise ConfigError(f"无法从 '{spec}' 推断平台，请显式指定（github/gitee/atomgit/gitlab）")
    owner, sep, name = text.partition("/")
    if not sep or not owner or not name:
        raise ConfigError(f"仓库标识格式应为 owner/name，收到：{spec}")
    return RepoRef(platform=plat, owner=owner, name=name.rstrip("/"))


def resolve_repos(settings: Settings, specs: list[str] | None = None) -> list[RepoConfig]:
    """把 CLI 传的仓库标识解析成 RepoConfig 列表。

    * 未传 ``specs``：返回配置里所有启用的仓库（Q6：多仓库，先单仓库也能跑）。
    * 传了 ``specs``：优先匹配已登记的仓库配置；未登记的按显式平台构造临时配置。
    """
    if not specs:
        return settings.enabled_repos()

    out: list[RepoConfig] = []
    for spec in specs:
        ref = parse_repo_ref(spec, platform=Platform.GITHUB if ":" not in spec and "/" in spec else None)
        matched = [
            r for r in settings.repos if r.slug == ref.slug and r.platform == ref.platform
        ]
        if matched:
            out.append(matched[0])
            continue
        # 未登记：构造临时配置（token 仍从全局配置取）
        out.append(
            RepoConfig(platform=ref.platform, owner=ref.owner, name=ref.name, enabled=True, since_days=0)
        )
    return out
