"""异常体系。"""

from __future__ import annotations


class FissueError(Exception):
    """所有 Fissue 异常的基类。"""


class ConfigError(FissueError):
    """配置缺失或非法。"""


class PlatformError(FissueError):
    """代码托管平台 API 调用失败。"""


class RateLimitError(PlatformError):
    """触发平台限流。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMError(FissueError):
    """大模型调用失败。"""


class BudgetExceeded(LLMError):
    """超出 token / 金额预算。"""


class SandboxError(FissueError):
    """沙盒执行失败。"""


class VerifierError(FissueError):
    """验证器生成或校验失败。"""


class PolicyError(FissueError):
    """自动修复策略不允许该操作。"""
