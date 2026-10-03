"""Fissue —— Issue/PR 的 AI 评测与自动验证/修复系统。

四平台抓取（GitHub/Gitee/AtomGit/GitLab）→ 大模型评测（真实性/重要性/可行性/PR质量）
→ 生成验证器并在 Docker 沙盒中 F2P 验证 → 对低难度高重要性 Issue 自动修复并提 PR。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
