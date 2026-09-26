import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from app import _pytest_runner as runner
from app.tools import CodebaseTools, ToolError


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tests").mkdir()
    # Exercise the runner's pipes, independent of installed third-party plugins
    # and pytest's own capture buffers.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-s")
    return tmp_path


def wait_until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.01)


@pytest.mark.parametrize("exitcode", [0, 7])
def test_output_is_bounded_while_pytest_is_still_running(repo, monkeypatch, exitcode):
    (repo / "tests/test_output.py").write_text(
        "import os, time\nfrom pathlib import Path\n"
        "for _ in range(512):\n"
        "    os.write(1, b'O' * 8192)\n"
        "    os.write(2, b'E' * 8192)\n"
        "os.write(2, b'stderr-end\\r\\n')\n"
        "Path('emitted').touch()\n"
        "while not Path('release').exists(): time.sleep(0.01)\n"
        f"os._exit({exitcode})\n",
        encoding="utf-8",
    )
    observed = {"bytes": 0, "tail": 0, "chunk": 0}
    original_feed = runner._OutputTail.feed

    def feed(tail, chunk, **kwargs):
        original_feed(tail, chunk, **kwargs)
        observed["bytes"] += len(chunk)
        observed["tail"] = max(observed["tail"], len(tail.text))
        observed["chunk"] = max(observed["chunk"], len(chunk))

    monkeypatch.setattr(runner._OutputTail, "feed", feed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(CodebaseTools(repo, 20).run_pytest, "tests/test_output.py")
        try:
            wait_until(lambda: (repo / "emitted").exists())
            wait_until(lambda: observed["bytes"] >= 8 * 1024 * 1024)
            assert not future.done()
            assert observed["tail"] <= 4000
            assert observed["chunk"] <= runner.READ_SIZE
        finally:
            (repo / "release").touch()
        result = future.result(timeout=10)
    assert result == {
        "status": "passed" if exitcode == 0 else "failed",
        "returncode": exitcode,
        "output": ("E" * 4000 + "stderr-end\n")[-4000:],
    }


def test_incremental_decoding_and_stdout_stderr_order(repo, monkeypatch):
    monkeypatch.setattr(runner.locale, "getpreferredencoding", lambda _: "utf-8")
    # Split every multibyte character and CRLF across read boundaries.
    monkeypatch.setattr(runner, "READ_SIZE", 1)
    (repo / "tests/test_text.py").write_text(
        "import os\n"
        "os.write(2, b'err\\r\\n')\n"
        "os.write(1, b'out: \\xe2\\x82\\xac\\r\\n')\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    result = CodebaseTools(repo, 10).run_pytest("tests/test_text.py")
    assert result["output"] == "out: €\nerr\n"


def test_invalid_output_preserves_decode_error_and_cleans_up_process(tree, monkeypatch):
    monkeypatch.setattr(runner.locale, "getpreferredencoding", lambda _: "utf-8")
    (tree / "tests/test_invalid.py").write_text(
        "import os, time\nfrom pathlib import Path\n"
        "Path('invalid.pid').write_text(str(os.getpid()))\n"
        "os.write(1, b'\\xff')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    with pytest.raises(UnicodeDecodeError):
        CodebaseTools(tree, 10).run_pytest("tests/test_invalid.py")
    assert not is_running(int((tree / "invalid.pid").read_text()))


def test_continuous_output_does_not_prevent_timeout(repo, monkeypatch):
    (repo / "tests/test_noisy.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path('noisy.pid').write_text(str(os.getpid()))\n"
        "while True:\n"
        "    os.write(1, b'x' * 8192)\n"
        "    os.write(2, b'y' * 8192)\n",
        encoding="utf-8",
    )
    original_feed = runner._OutputTail.feed
    count = 0

    def feed(tail, chunk, **kwargs):
        nonlocal count
        count += len(chunk)
        original_feed(tail, chunk, **kwargs)
        assert len(tail.text) <= 4000

    monkeypatch.setattr(runner._OutputTail, "feed", feed)
    with pytest.raises(ToolError, match="timed out"):
        CodebaseTools(repo, 5).run_pytest("tests/test_noisy.py")
    assert count > 4000
    assert not is_running(int((repo / "noisy.pid").read_text()))


def make_process_tree(repo: Path, *, parent_exits: bool = False) -> None:
    grandchild = (
        "import os, time, signal\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('grandchild.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    child = (
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(60)\n"
    )
    (repo / "tests/test_tree.py").write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "Path('parent.pid').write_text(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "while not Path('grandchild.pid').exists(): time.sleep(0.01)\n"
        + ("os._exit(0)\n" if parent_exits else "time.sleep(60)\n"),
        encoding="utf-8",
    )


def is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        from app import _windows_process as windows

        handle = windows._open_process(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            assert ctypes.get_last_error() == 87  # process no longer exists
            return False
        try:
            get_exit = windows._function(
                "GetExitCodeProcess", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL,
            )
            code = wintypes.DWORD()
            assert get_exit(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            windows._close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux containers may leave dead orphans as zombies until PID 1 reaps them.
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return sys.platform != "linux"


@pytest.fixture
def tree(repo):
    yield repo
    # A failing regression must not leak its deliberately long-lived children.
    for path in repo.glob("*.pid"):
        pid = int(path.read_text())
        if is_running(pid):
            os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


def assert_tree_stopped(repo):
    paths = [repo / f"{name}.pid" for name in ("parent", "child", "grandchild")]
    assert all(path.exists() for path in paths), "tree did not start before cleanup"
    pids = [int(path.read_text()) for path in paths]
    wait_until(lambda: not any(is_running(pid) for pid in pids), timeout=3)


@pytest.mark.parametrize("invoke", [False, True])
def test_timeout_cleans_up_children_and_grandchildren(tree, invoke, monkeypatch):
    make_process_tree(tree)
    tools = CodebaseTools(tree, timeout_seconds=5)
    if invoke:
        original_run = runner.run_pytest_process

        def longer_worker_timeout(*args, **kwargs):
            # The outer invoke deadline must stop the worker itself; do not let
            # the worker's independent deadline conceal a cancellation leak.
            kwargs["timeout"] = 60
            return original_run(*args, **kwargs)

        monkeypatch.setattr("app.tools.run_pytest_process", longer_worker_timeout)
    with pytest.raises(ToolError, match="timed out"):
        if invoke:
            asyncio.run(tools.invoke("run_pytest", {"test_path": "tests/test_tree.py"}))
        else:
            tools.run_pytest("tests/test_tree.py")
    assert_tree_stopped(tree)


def test_cancellation_waits_for_tree_cleanup_without_shutting_down_event_loop(tree, monkeypatch):
    make_process_tree(tree)
    tools = CodebaseTools(tree, timeout_seconds=30)
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original_run = runner.run_pytest_process

    def delayed_cleanup(*args, **kwargs):
        try:
            return original_run(*args, **kwargs)
        finally:
            cleanup_started.set()
            assert allow_cleanup.wait(5)

    monkeypatch.setattr("app.tools.run_pytest_process", delayed_cleanup)

    async def scenario():
        task = asyncio.create_task(tools.invoke("run_pytest", {"test_path": "tests/test_tree.py"}))
        try:
            deadline = time.monotonic() + 10
            while not (tree / "grandchild.pid").exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            task.cancel()
            while not cleanup_started.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            task.cancel()  # repeated cancellation must not abandon the worker
            await asyncio.sleep(0.05)
            assert not task.done()
            allow_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_tree_stopped(tree)  # before asyncio.run's executor shutdown
        finally:
            allow_cleanup.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_parent_exit_does_not_leave_children_holding_output_pipes(tree):
    make_process_tree(tree, parent_exits=True)
    result = CodebaseTools(tree, 10).run_pytest("tests/test_tree.py")
    assert result["status"] == "passed"
    assert_tree_stopped(tree)


def test_interrupt_during_output_collection_cleans_up_tree(tree, monkeypatch):
    make_process_tree(tree)
    original_read = runner.read_pipe

    def interrupted_read(*args):
        if (tree / "grandchild.pid").exists():
            raise KeyboardInterrupt
        return original_read(*args)

    monkeypatch.setattr(runner, "read_pipe", interrupted_read)
    with pytest.raises(KeyboardInterrupt):
        CodebaseTools(tree, 10).run_pytest("tests/test_tree.py")
    assert_tree_stopped(tree)


def test_cancelling_one_invocation_does_not_cancel_another(tree):
    make_process_tree(tree)
    (tree / "tests/test_other.py").write_text(
        "import time\nfrom pathlib import Path\n"
        "Path('other-started').touch()\n"
        "while not Path('other-release').exists(): time.sleep(0.01)\n"
        "def test_ok(): assert True\n",
        encoding="utf-8",
    )
    tools = CodebaseTools(tree, 15)

    async def scenario():
        first = asyncio.create_task(tools.invoke("run_pytest", {"test_path": "tests/test_tree.py"}))
        second = asyncio.create_task(tools.invoke("run_pytest", {"test_path": "tests/test_other.py"}))
        try:
            deadline = time.monotonic() + 10
            while not ((tree / "grandchild.pid").exists() and (tree / "other-started").exists()):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert_tree_stopped(tree)
            assert not second.done()
            (tree / "other-release").touch()
            result, _ = await second
            assert result["status"] == "passed"
        finally:
            (tree / "other-release").touch()
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(scenario())


def test_pre_cancelled_run_does_not_start_a_process(repo, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("process started"))
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_pytest_process([sys.executable, "-m", "pytest"], repo, 10, cancel)


@pytest.mark.skipif(os.name != "nt", reason="Windows job startup")
@pytest.mark.parametrize("failure", ["assign", "resume"])
def test_windows_startup_failure_never_executes_pytest(repo, monkeypatch, failure):
    (repo / "tests/test_start.py").write_text("from pathlib import Path\nPath('executed').touch()\n")
    processes = []
    original_popen = runner.subprocess.Popen

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail(*args):
        # Allow ample time for code to execute if startup wasn't suspended.
        time.sleep(0.2)
        raise OSError("injected startup failure")

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    if failure == "assign":
        monkeypatch.setattr(runner.WindowsJob, "assign", fail)
    else:
        monkeypatch.setattr(runner, "resume_process", fail)
    with pytest.raises(OSError, match="injected startup failure"):
        CodebaseTools(repo, 10).run_pytest("tests/test_start.py")
    assert not (repo / "executed").exists()
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
