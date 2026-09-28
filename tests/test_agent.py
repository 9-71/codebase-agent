import asyncio
from pathlib import Path
import sys
import time
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentExecutionError, AgentService
from app.config import Settings
from app.main import app
from app.model import FakeModel
from app.tools import CodebaseTools, ToolError


FIXTURE_REPO = Path(__file__).parent / "fixture_repo"


def make_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        if sys.platform == "win32":
            pytest.skip(f"symlinks are unavailable: {exc}")
        raise


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


def test_search_code_skips_external_and_broken_symlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.txt").write_text("inside-marker", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("outside-marker", encoding="utf-8")
    make_symlink(repo / "external-link.txt", external)
    make_symlink(repo / "broken-link.txt", tmp_path / "missing.txt")

    tools = CodebaseTools(repo)
    assert tools.search_code("outside-marker")["matches"] == []
    assert tools.search_code("inside-marker")["matches"]
    assert tools.search_code("missing-marker")["matches"] == []
    assert tools.read_file("inside.txt")["content"] == "inside-marker"
    with pytest.raises(ToolError, match="escapes repository root"):
        tools.read_file("external-link.txt")


def test_in_repo_file_symlink_remains_readable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.txt"
    source.write_text("inside-link-marker", encoding="utf-8")
    make_symlink(repo / "linked.txt", source)

    tools = CodebaseTools(repo)
    assert tools.read_file("linked.txt")["content"] == "inside-link-marker"
    assert any("linked.txt:" in match for match in tools.search_code("inside-link-marker")["matches"])


def test_search_code_checks_resolved_file_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    linked = repo / "linked.txt"
    linked.write_text("outside-marker", encoding="utf-8")
    inside = repo / "inside.txt"
    inside.write_text("inside-marker", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-marker", encoding="utf-8")
    tools = CodebaseTools(repo)
    original_resolve = Path.resolve

    def resolve_path(path: Path, *args: object, **kwargs: object) -> Path:
        if path == linked:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_path)
    assert tools.search_code("outside-marker")["matches"] == []
    assert tools.search_code("inside-marker")["matches"]
    assert tools.read_file("inside.txt")["content"] == "inside-marker"


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


def test_pytest_output_is_bounded_while_running(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_output.py").write_text(
        "import os\n"
        "def test_output():\n"
        "    os.write(1, b'x' * 8_000_000)\n"
        "    os.write(2, b'y' * 8_000_000)\n"
        "    assert False\n",
        encoding="utf-8",
    )

    tracemalloc.start()
    try:
        result = CodebaseTools(repo, timeout_seconds=15).run_pytest("tests/test_output.py")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["status"] == "failed"
    assert len(result["output"]) <= 4000
    assert peak < 6_000_000


def _write_spawning_test(repo: Path, heartbeat: Path) -> None:
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    child_code = (
        "import pathlib, time\n"
        f"p = pathlib.Path({str(heartbeat)!r})\n"
        "while True:\n"
        "    with p.open('ab') as f: f.write(b'x')\n"
        "    time.sleep(0.05)\n"
    )
    (tests_dir / "test_spawn.py").write_text(
        "import subprocess, sys, time\n"
        "def test_spawn():\n"
        f"    subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )


def _assert_heartbeat_stopped(heartbeat: Path) -> None:
    assert heartbeat.exists(), "child process never started"
    size = heartbeat.stat().st_size
    time.sleep(0.3)
    assert heartbeat.stat().st_size == size


def test_pytest_timeout_kills_child_process(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    heartbeat = tmp_path / "heartbeat"
    _write_spawning_test(repo, heartbeat)

    with pytest.raises(ToolError, match="pytest timed out"):
        CodebaseTools(repo, timeout_seconds=3).run_pytest("tests/test_spawn.py")
    _assert_heartbeat_stopped(heartbeat)


def test_pytest_cancellation_kills_child_process(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    heartbeat = tmp_path / "heartbeat"
    _write_spawning_test(repo, heartbeat)

    async def cancel_after_child_starts() -> None:
        task = asyncio.create_task(CodebaseTools(repo, timeout_seconds=10).invoke("run_pytest", {"test_path": "tests/test_spawn.py"}))
        for _ in range(100):
            if heartbeat.exists():
                break
            await asyncio.sleep(0.05)
        else:
            task.cancel()
            pytest.fail("child process never started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_after_child_starts())
    _assert_heartbeat_stopped(heartbeat)


def test_pytest_allows_in_repo_test_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True)
    source = tests_dir / "test_real.py"
    source.write_text("def test_real(): assert 1 + 1 == 2\n", encoding="utf-8")
    make_symlink(tests_dir / "test_link.py", source)

    assert CodebaseTools(repo, timeout_seconds=10).run_pytest("tests/test_link.py")["status"] == "passed"


@pytest.mark.parametrize("location", ["direct", "fallback", "intermediate"])
def test_pytest_rejects_external_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    external = tmp_path / "outside"
    (external / "tests").mkdir(parents=True)
    external_test = external / "tests" / "test_escape.py"
    external_test.write_text("def test_escape(): raise AssertionError('external test executed')\n", encoding="utf-8")

    if location == "direct":
        make_symlink(repo / "tests" / "test_escape.py", external_test)
    elif location == "fallback":
        fallback_dir = repo / "tests" / "fixture_repo" / "tests"
        fallback_dir.mkdir(parents=True)
        make_symlink(fallback_dir / "test_escape.py", external_test)
    else:
        make_symlink(repo / "tests" / "fixture_repo", external, directory=True)

    monkeypatch.setattr("app.tools.subprocess.run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(ToolError, match="escapes repository root"):
        CodebaseTools(repo).run_pytest("tests/test_escape.py")


def test_pytest_checks_resolved_fallback_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    fallback = repo / "tests" / "fixture_repo" / "tests" / "test_escape.py"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("def test_escape(): raise AssertionError('fallback test executed')\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def test_escape(): raise AssertionError('external test executed')\n", encoding="utf-8")
    tools = CodebaseTools(repo)
    original_resolve = Path.resolve

    def resolve_path(path: Path, *args: object, **kwargs: object) -> Path:
        if path == fallback:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_path)
    monkeypatch.setattr("app.tools.subprocess.run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(ToolError, match="escapes repository root"):
        tools.run_pytest("tests/test_escape.py")


@pytest.mark.parametrize("destination", ["outside_repo", "outside_tests"])
def test_pytest_checks_resolved_direct_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str) -> None:
    repo = tmp_path / "repo"
    direct = repo / "tests" / "test_escape.py"
    direct.parent.mkdir(parents=True)
    direct.write_text("def test_escape(): raise AssertionError('unexpected test executed')\n", encoding="utf-8")
    resolved = tmp_path / "outside.py" if destination == "outside_repo" else repo / "source.py"
    resolved.write_text("def test_escape(): raise AssertionError('unexpected test executed')\n", encoding="utf-8")
    tools = CodebaseTools(repo)
    original_resolve = Path.resolve

    def resolve_path(path: Path, *args: object, **kwargs: object) -> Path:
        if path == direct:
            return resolved
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_path)
    monkeypatch.setattr("app.tools.subprocess.run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(ToolError, match="repository root|allowed tests directory"):
        tools.run_pytest("tests/test_escape.py")


def test_pytest_rejects_test_symlink_to_non_test_repo_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    source = repo / "source.py"
    source.write_text("def test_escape(): raise AssertionError('unexpected test executed')\n", encoding="utf-8")
    make_symlink(repo / "tests" / "test_escape.py", source)

    monkeypatch.setattr("app.tools.subprocess.run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(ToolError, match="allowed tests directory"):
        CodebaseTools(repo).run_pytest("tests/test_escape.py")


def test_pytest_broken_symlink_does_not_select_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    make_symlink(repo / "tests" / "test_missing.py", repo / "tests" / "absent.py")
    fallback_dir = repo / "tests" / "fixture_repo" / "tests"
    fallback_dir.mkdir(parents=True)
    (fallback_dir / "test_missing.py").write_text("def test_missing(): raise AssertionError('fallback test executed')\n", encoding="utf-8")

    monkeypatch.setattr("app.tools.subprocess.run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(ToolError, match="test file does not exist"):
        CodebaseTools(repo).run_pytest("tests/test_missing.py")


def test_pytest_does_not_search_for_same_named_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    other = repo / "examples" / "tests"
    other.mkdir(parents=True)
    (other / "test_unexpected.py").write_text("def test_unexpected(): raise AssertionError('unexpected test executed')\n", encoding="utf-8")

    monkeypatch.setattr("app.tools.subprocess.run", lambda *args, **kwargs: pytest.fail("pytest must not run"))
    with pytest.raises(ToolError, match="test file does not exist"):
        CodebaseTools(repo).run_pytest("tests/test_unexpected.py")


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
