"""新手引导的生成与契约测试（离线，不连模型、不联网）。

覆盖三件事：
1. **前两阶段永远来自代码**——模型抽风也不该让新手看不到「主机怎么登记」；
2. **模型不可用就降级**，而不是抛错让页面空白；
3. AI 只能写 ``deploy`` 这一个阶段，动作必须落在固定白名单里。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fissue.deploy.guide import (
    MAX_GUIDE_STEPS,
    GuideBuilder,
    fallback_guide,
    guide_prompt,
    intro_stage,
    prepare_stage,
)
from fissue.deploy.schema import GuideAction, GuidePlan, GuideStage, GuideStep


def _ai_payload(*, action: str | None = None, steps: int = 2, stage_id: str = "deploy") -> dict:
    return {
        "version": 1,
        "repo_slug": "acme/web",
        "host": "rpi",
        "mode": "half",
        "source": "ai",
        "notes": "模型备注",
        "stages": [
            {
                "id": stage_id,
                "title": "部署这个仓库",
                "goal": "让它跑起来",
                "steps": [
                    {
                        "id": f"s{i}",
                        "title": f"步骤{i}",
                        "why": "因为要跑起来",
                        "command": "pip install -r requirements.txt",
                        "verify": "没有报错",
                        "action": action,
                    }
                    for i in range(steps)
                ],
            }
        ],
    }


class FakeClient:
    """假 LLM：按顺序吐预设结果，并记录调用。"""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls: list[list[dict]] = []

    async def chat_json(self, messages, **kwargs):
        self.calls.append(list(messages))
        payload = self.payloads.pop(0) if self.payloads else {}
        if isinstance(payload, Exception):
            raise payload
        return payload, None


def test_fallback_guide_has_three_stages_and_manual_commands():
    plan = fallback_guide("acme/web", "rpi")
    assert plan.source == "fallback"
    assert [stage.id for stage in plan.stages] == ["intro", "prepare", "deploy"]
    # 每一步要么能照着手动做（命令/判定/动作），要么是纯讲解（有 why）。
    # 「它会做什么、不会做什么」这种步骤本来就没有命令——它是给人读的。
    for stage in plan.stages:
        for step in stage.steps:
            assert step.command or step.verify or step.action or step.why


def test_fixed_stages_do_not_depend_on_model():
    intro, prepare = intro_stage("rpi"), prepare_stage("rpi")
    assert "fissue host add rpi" in prepare.steps[1].command
    assert prepare.steps[1].action is GuideAction.ADD_HOST
    assert any(step.id == "enable" for step in intro.steps)


@pytest.mark.asyncio
async def test_model_failure_falls_back_instead_of_raising(tmp_path):
    client = FakeClient(RuntimeError("模型 401"))
    plan = await GuideBuilder(client).build(
        tmp_path, repo_slug="acme/web", host="rpi", host_address="10.0.0.5"
    )
    assert plan.source == "fallback"
    assert [stage.id for stage in plan.stages] == ["intro", "prepare", "deploy"]
    # 降级原因要写进 notes，页面据此提示"这是兜底"
    assert "模型不可用" in plan.notes


@pytest.mark.asyncio
async def test_ai_stage_is_appended_after_fixed_stages(tmp_path):
    client = FakeClient(_ai_payload())
    plan = await GuideBuilder(client).build(
        tmp_path, repo_slug="acme/web", host="rpi", host_address="10.0.0.5"
    )
    assert plan.source == "ai"
    assert [stage.id for stage in plan.stages] == ["intro", "prepare", "deploy"]
    assert plan.stages[-1].steps[0].command == "pip install -r requirements.txt"


@pytest.mark.asyncio
async def test_unknown_action_rejected_then_retried(tmp_path):
    bad = _ai_payload()
    bad["stages"][0]["steps"][0]["action"] = "delete-everything"
    client = FakeClient(bad, _ai_payload())
    plan = await GuideBuilder(client, max_repairs=1).build(
        tmp_path, repo_slug="acme/web", host="rpi"
    )
    assert plan.source == "ai"
    # 重试时要把校验错误原文喂回去，否则模型无从修正
    assert "没通过校验" in client.calls[1][-1]["content"]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_extra_stage_from_model_is_rejected(tmp_path):
    client = FakeClient(_ai_payload(stage_id="prepare"), _ai_payload(stage_id="prepare"))
    plan = await GuideBuilder(client, max_repairs=1).build(
        tmp_path, repo_slug="acme/web", host="rpi"
    )
    # 两次都不合法 → 降级，但绝不把模型的阶段拼进来
    assert plan.source == "fallback"


@pytest.mark.asyncio
async def test_too_many_steps_falls_back(tmp_path):
    client = FakeClient(_ai_payload(steps=MAX_GUIDE_STEPS + 1), _ai_payload(steps=MAX_GUIDE_STEPS + 1))
    plan = await GuideBuilder(client, max_repairs=1).build(
        tmp_path, repo_slug="acme/web", host="rpi"
    )
    assert plan.source == "fallback"


def test_prompt_carries_repo_facts_and_action_whitelist():
    messages = guide_prompt(
        repo_slug="acme/web",
        host="rpi",
        host_address="10.0.0.5",
        context="README：这是一个 Flask 服务",
        deploy_enabled=True,
    )
    user = messages[-1]["content"]
    assert "acme/web" in user
    assert "10.0.0.5" in user
    assert "Flask" in user
    assert "add-host" in user and "rollback" in user
    # 明确告诉模型不能指定接口
    assert "不能" in messages[0]["content"]


def test_guide_plan_rejects_duplicate_step_ids():
    with pytest.raises(ValidationError):
        GuidePlan(
            repo_slug="a/b",
            stages=[
                GuideStage(
                    id="deploy",
                    title="x",
                    steps=[GuideStep(id="dup", title="a"), GuideStep(id="dup", title="b")],
                )
            ],
        )


def test_guide_plan_rejects_empty_stage():
    with pytest.raises(ValidationError):
        GuidePlan(repo_slug="a/b", stages=[GuideStage(id="deploy", title="x", steps=[])])

def test_action_steps_get_canonical_commands():
    """有 action 的步骤，命令必须被规范写法覆盖。

    真机验证时模型两次编错 CLI（`fissue deploy add-host`、`fissue deploy rollback <slug>`），
    而这些命令是给新手直接复制的——错的比没有更糟。
    """
    from fissue.deploy.guide import _normalize_commands

    plan = GuidePlan(
        repo_slug="acme/web",
        stages=[
            GuideStage(
                id="deploy",
                title="部署",
                steps=[
                    GuideStep(
                        id="a",
                        title="登记主机",
                        command="fissue deploy add-host rpi --address 1.2.3.4",
                        action=GuideAction.ADD_HOST,
                    ),
                    GuideStep(
                        id="b",
                        title="回滚",
                        command="fissue deploy rollback acme/web --host rpi",
                        action=GuideAction.ROLLBACK,
                    ),
                ],
            )
        ],
    )
    _normalize_commands(plan, repo_slug="acme/web", host="rpi")
    steps = plan.stages[0].steps
    assert steps[0].command == (
        "fissue host add rpi --address <IP> --user <用户> --key-file <私钥路径>"
    )
    assert steps[1].command == "fissue deploy rollback <部署id>"
    # 被校正过的步骤要留痕，别让人以为模型本来就写对了
    assert any("校正" in c for c in steps[0].cautions)


def test_unknown_fissue_subcommand_gets_a_warning():
    """手动步骤里编造的 `fissue` 子命令不改写，但要提示人核对。"""
    from fissue.deploy.guide import _normalize_commands

    plan = GuidePlan(
        repo_slug="acme/web",
        stages=[
            GuideStage(
                id="deploy",
                title="部署",
                steps=[
                    GuideStep(id="m", title="查状态", command="fissue deploy-status all"),
                    GuideStep(id="n", title="看日志", command="journalctl -u app -n 50"),
                ],
            )
        ],
    )
    _normalize_commands(plan, repo_slug="acme/web", host="rpi")
    steps = plan.stages[0].steps
    assert steps[0].command == "fissue deploy-status all"  # 原样保留
    assert any("--help" in c for c in steps[0].cautions)
    assert steps[1].cautions == []  # 非 fissue 命令不加噪声
