"""pytest 公共夹具。

原则：所有单测**不打网络、不碰 Docker、不写用户目录**——
LLM 用打桩客户端，平台适配器用 respx 拦截，数据库用临时 SQLite。

其中「不打网络」由一个 autouse 的**全局网络守卫**强制保证（见下方
``_block_real_network``）：任何试图建立真实 socket 连接的代码都会立刻拿到
一个清晰的 ``RuntimeError``，而不是悄悄地连上外网再等超时。
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

# 让测试可直接 import fissue（无需先 pip install -e .）
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fissue.config import Settings, load_settings  # noqa: E402
from fissue.models import (  # noqa: E402
    Category,
    Comment,
    DimensionScore,
    Evaluation,
    ItemType,
    Platform,
    Priority,
    RawItem,
    RepoRef,
    Scores,
)
from fissue.store.db import Database  # noqa: E402
from fissue.store.repository import Repository  # noqa: E402


# ---------------------------------------------------------------------------
# 全局网络守卫
# ---------------------------------------------------------------------------

# 允许的真实连接目标（测试内部用的本地服务）。默认空——测试不该需要外网。
_ALLOWED_HOSTS: set[str] = {"127.0.0.1", "localhost", "::1"}

_REAL_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection


class RealNetworkAccessError(RuntimeError):
    """测试里出现了真实网络访问——应该改用 respx / 打桩。"""


def _host_of(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def _guard_connect(self: socket.socket, address: Any) -> Any:  # noqa: ANN401
    """拦截真实 socket 连接。

    respx 在 httpx 的 transport 层拦截，**不会**走到这里，所以被 mock 的
    请求不受影响；只有「忘了 mock」的真实外连才会触发。
    """
    if _host_of(address) in _ALLOWED_HOSTS:
        return _REAL_CONNECT(self, address)
    raise RealNetworkAccessError(
        f"测试试图连接真实网络：{address}\n"
        "单测必须离线：HTTP 请用 respx.mock 拦截，LLM 请用 StubLLM，"
        "或用 monkeypatch 打桩对应方法。"
    )


def _guard_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    if _host_of(address) in _ALLOWED_HOSTS:
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
    raise RealNetworkAccessError(f"测试试图连接真实网络：{address}")


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """autouse：阻断测试期间的真实网络连接。

    为什么需要它：曾经有个测试没 mock 打标签逻辑，本地因证书问题**快速失败**
    而侥幸通过；等 TLS 修好后它真的连上了 GitHub，触发 4 轮重试退避，
    把整个测试套件从 25 秒拖到 5 分钟以上（表现为"超时"）。

    有了这个守卫，同类问题会在第一次外连时立刻以清晰的报错暴露，
    而不是伪装成"跑得慢"。
    """
    monkeypatch.setattr(socket.socket, "connect", _guard_connect)
    monkeypatch.setattr(socket, "create_connection", _guard_create_connection)


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一份完全离线的配置（SQLite + 打桩 key + local 沙盒）。

    关键：**必须显式指向 tmp_path 里的配置文件**。
    若只传 overrides 而不指定 config_path，``load_settings()`` 会去读
    ``./config.yaml``——也就是仓库里那份**开发者真实配置**，
    于是测试会被环境里的 fallback_model / notify 渠道等污染，出现「本机过、CI 挂」
    或反向的诡异失败。这里生成一份最小配置，保证测试与外部环境彻底隔离。
    """
    db_file = tmp_path / "test.db"
    cfg_file = tmp_path / "config.test.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "database": {"url": f"sqlite:///{db_file.as_posix()}"},
                "llm": {
                    "api_key": "test-key",
                    "model": "test-model",
                    "base_url": "https://llm.invalid/v1",
                    # 显式留空，避免落到仓库 config.yaml 的 fallback 上
                    "fallback_model": None,
                },
                "sandbox": {"runtime": "local", "enabled": True},
                "app": {"data_dir": str(tmp_path / "data"), "log_level": "warning"},
                "export": {"output_dir": str(tmp_path / "exports")},
                "repos": [
                    {"platform": "github", "owner": "psf", "name": "requests", "since_days": 0},
                ],
                "notify": {"enabled": False, "channels": []},
            }
        ),
        encoding="utf-8",
    )
    # env 指向一个不存在的文件，确保不读仓库根的 .env
    return load_settings(cfg_file, tmp_path / "missing.env")


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database.url)
    database.create_all()
    return database


@pytest.fixture
def repo(db: Database) -> Repository:
    return Repository(db)


@pytest.fixture
def sample_issue() -> RawItem:
    return RawItem(
        platform=Platform.GITHUB,
        repo="psf/requests",
        number=42,
        item_type=ItemType.ISSUE,
        title="程序崩溃：requests.get 传入超时后抛异常",
        body="复现步骤：\n1. 调用 requests.get(url, timeout=1)\n2. 观察崩溃\n期望：正常抛 Timeout 异常",
        labels=["bug", "needs-triage"],
        author="reporter",
        comments=[Comment(author="maintainer", body="已确认，我来看看")],
    )


@pytest.fixture
def sample_pr() -> RawItem:
    return RawItem(
        platform=Platform.GITHUB,
        repo="psf/requests",
        number=43,
        item_type=ItemType.PR,
        title="fix: 修复超时处理",
        body="修复了 #42 的问题",
        labels=[],
        author="contributor",
        head_branch="fix-timeout",
        base_branch="main",
        additions=12,
        deletions=4,
        changed_files=2,
        linked_issues=[42],
    )


@pytest.fixture
def good_evaluation() -> Evaluation:
    return Evaluation(
        scores=Scores(
            authenticity=DimensionScore(score=92, reason="有完整复现步骤", confidence=0.9),
            importance=DimensionScore(score=88, reason="影响核心功能", confidence=0.85),
            feasibility=DimensionScore(score=80, reason="改动局部", confidence=0.8),
            difficulty=DimensionScore(score=25, reason="只需加一个判断", confidence=0.8),
        ),
        category=Category.BUG,
        priority=Priority.TIER1,
        summary="真实缺陷，建议立即修复",
        model="test-model",
    )


class StubLLM:
    """打桩的 LLM 客户端：按调用序号返回预设 JSON。"""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.budget_guard = None

    async def chat_json(self, messages, *, purpose: str = "", **kwargs):
        from fissue.ai.client import Usage

        self.calls.append({"purpose": purpose, "messages": messages})
        data = self.responses.pop(0) if self.responses else {}
        usage = Usage(purpose=purpose, model="test-model", prompt_tokens=100, completion_tokens=50, cost_usd=0.001)
        return data, usage

    async def chat(self, messages, *, purpose: str = "", **kwargs):
        from fissue.ai.client import LLMResponse, Usage

        data = self.responses.pop(0) if self.responses else {}
        usage = Usage(purpose=purpose, model="test-model")
        return LLMResponse(content="", model="test-model", usage=usage, raw=data)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def stub_llm() -> StubLLM:
    return StubLLM()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
