import asyncio
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentExecutionError, AgentService
from app.config import Settings
from app.main import app
from app.model import FakeModel
from app.tools import CodebaseTools, ToolError


FIXTURE_REPO = Path(__file__).parent / "fixture_repo"


@pytest.fixture
def service() -> AgentService:
    return AgentService(Settings(repo_root=FIXTURE_REPO), model=FakeModel())


def test_chat_runs_allowlisted_tool_and_caches(service: AgentService) -> None:
    first = asyncio.run(service.chat("s1", "search FastAPI"))
    second = asyncio.run(service.chat("s1", "search FastAPI"))
    assert first.tool_calls[0].name == "search_code"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert service.metrics["cache_hits"] == 1
    assert service.metrics["successes"] == 2


def test_path_traversal_is_rejected() -> None:
    tools = CodebaseTools(FIXTURE_REPO)
    with pytest.raises(ToolError, match="escapes"):
        tools.read_file("../secret.txt")


def test_fake_model_routes_path_traversal_to_read_file() -> None:
    result = FakeModel().complete([{"role": "user", "content": "read ../secret.txt"}], [])
    assert result["tool_calls"] == [{"name": "read_file", "arguments": {"path": "../secret.txt"}}]
    with pytest.raises(ToolError, match="Access denied: path outside working directory"):
        CodebaseTools(FIXTURE_REPO).read_file("../secret.txt")


def test_search_code_excludes_blacklisted_paths_and_suffixes() -> None:
    tools = CodebaseTools(FIXTURE_REPO)
    assert tools._is_search_excluded(FIXTURE_REPO / "__pycache__" / "cached.py")
    assert tools._is_search_excluded(FIXTURE_REPO / ".pytest_cache" / "state.txt")
    assert tools._is_search_excluded(FIXTURE_REPO / "venv" / "module.py")
    assert tools._is_search_excluded(FIXTURE_REPO / "compiled.PYC")
    assert tools.search_code("binary-only-marker")["matches"] == []
    assert tools.search_code("source-only-marker")["matches"]


def test_pytest_is_allowlisted() -> None:
    tools = CodebaseTools(FIXTURE_REPO)
    with pytest.raises(ToolError, match="limited"):
        tools.run_pytest("README.md")


def test_allowlisted_pytest_is_executed() -> None:
    result = CodebaseTools(FIXTURE_REPO).run_pytest("tests/test_smoke.py")
    assert result["status"] == "passed"


def test_pytest_path_falls_back_to_fixture_repo() -> None:
    result = CodebaseTools(Path(__file__).parent.parent).run_pytest("tests/test_smoke.py")
    assert result["status"] == "passed"


class FlakyModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            raise OSError("temporary model error")
        return {"content": "recovered", "tool_calls": []}


class SlowModel:
    def complete(self, messages, tools):
        time.sleep(0.05)
        return {"content": "too late", "tool_calls": []}


def test_model_temporary_failure_is_retried() -> None:
    model = FlakyModel()
    configured = Settings(repo_root=FIXTURE_REPO, model_retry_attempts=2, retry_backoff_seconds=0)
    service = AgentService(configured, model=model)
    result = asyncio.run(service.chat("retry", "hello"))
    assert result.answer == "recovered"
    assert model.calls == 2
    assert service.metrics["retries"] == 1


def test_model_timeout_becomes_structured_agent_error() -> None:
    configured = Settings(repo_root=FIXTURE_REPO, model_timeout_seconds=0.01, model_retry_attempts=1)
    service = AgentService(configured, model=SlowModel())
    with pytest.raises(AgentExecutionError):
        asyncio.run(service.chat("timeout", "hello"))
    assert service.metrics["timeouts"] == 1


def test_api_contract() -> None:
    client = TestClient(app)
    health = client.get("/healthz")
    response = client.post("/v1/chat", json={"session_id": "api", "message": "search FastAPI"})
    assert health.status_code == 200
    assert response.status_code == 200
    assert {"answer", "evidence", "tool_calls", "trace_id", "cache_hit"} <= response.json().keys()
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
    assert "codebase_agent_http_requests_total" in metrics.text
    assert "codebase_agent_http_request_duration_seconds_bucket" in metrics.text
    assert "codebase_agent_cache_hits_total" in metrics.text
    assert "codebase_agent_cache_misses_total" in metrics.text
