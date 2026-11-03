"""部署与服务器运维。

契约与安全边界见 ``docs/DEPLOY.md``；本包内的分层：

- ``schema``     计划/步骤/规格的数据模型（契约，先改文档）
- ``secretbox``  凭据的可逆加密（SSH 私钥/密码要还原出原文才能用）
- ``ssh``        远端执行通道（唯一出口，负责指纹校验与超时）
- ``guard``      危险命令拦截与计划合法性校验
- ``planner``    AI 读仓库 → DeployPlan
- ``runner``     执行计划、判定证据、失败回滚、审计落库

与 ``sandbox/`` 的关系：**不复用**。沙盒是禁网、只读根、一次性的，用来跑
验证器；部署要写盘、要常驻、要联网。两者安全模型相反。
"""

from __future__ import annotations

__all__ = [
    "DeployPlan",
    "DeployStatus",
    "DeployStep",
    "ProbeSpec",
    "StepKind",
    "StepStatus",
]

from .schema import DeployPlan, DeployStatus, DeployStep, ProbeSpec, StepKind, StepStatus