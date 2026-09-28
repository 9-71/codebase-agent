import asyncio
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from app import _pytest_process
from app.tools import CodebaseTools, ToolError


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tests").mkdir()
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    # Exercise the runner's pipes directly, not pytest's internal capture files.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-s")
    return tmp_path


def write_test(repo: Path, source: str, name: str = "test_resource.py") -> str:
    (repo / "tests" / name).write_text(source, encoding="utf-8")
    return f"tests/{name}"


def test_output_tail_is_bounded_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pytest_process.locale, "getpreferredencoding", lambda _: "utf-8")
    tail = _pytest_process._OutputTail()
    for _ in range(2000):
        tail.append(b"a" * _pytest_process.READ_SIZE)
        assert len(tail.text) == 4000
    tail.append(b"\xe2")
    tail.append(b"\x82\xac\r")
    tail.append(b"\nend\xff", final=True)
    assert tail.text.endswith("\u20ac\nend\ufffd")
    assert len(tail.text) == 4000


@pytest.mark.parametrize("exitcode", [0, 7])
def test_large_output_preserves_tail_and_result(repo: Path, exitcode: int,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # Run during collection and exit without pytest's own summary/footer.
    path = write_test(repo, f"""
import os
for _ in range(1024):
    os.write(1, b'A' * 8192)
    os.write(2, b'B' * 8192)
os.write(1, b'STDOUT-END')
os.write(2, b'STDERR-END')
os._exit({exitcode})
""")
    original_append = _pytest_process._OutputTail.append
    observed = [0, 0]

    def append(tail, chunk, *, final=False):
        assert len(chunk) <= _pytest_process.READ_SIZE
        original_append(tail, chunk, final=final)
        assert len(tail.text) <= 4000
        observed[0] += len(chunk)
        observed[1] += 1

    monkeypatch.setattr(_pytest_process._OutputTail, "append", append)
    result = CodebaseTools(repo, timeout_seconds=20).run_pytest(path)
    assert result == {
        "status": "passed" if exitcode == 0 else "failed",
        "returncode": exitcode,
        "output": "B" * (4000 - len("STDERR-END")) + "STDERR-END",
    }
    assert observed[0] == 2 * 1024 * 8192 + len("STDOUT-ENDSTDERR-END")
    assert observed[1] > 2000


def test_stdout_then_stderr_order_and_newlines(repo: Path) -> None:
    path = write_test(repo, "import os\nos.write(2, b'err\\r\\n')\nos.write(1, b'out\\r\\n')\nos._exit(0)\n")
    assert CodebaseTools(repo, timeout_seconds=10).run_pytest(path)["output"] == "out\nerr\n"


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            assert ctypes.get_last_error() == 87  # Process no longer exists.
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Orphans can briefly remain zombies until the system reaps them.
    stat = Path(f"/proc/{pid}/stat")
    try:
        return not (stat.exists() and stat.read_text().split(")", 1)[1].split()[0] == "Z")
    except FileNotFoundError:
        return False


@pytest.fixture
def tree(repo: Path):
    grandchild = (
        "import os,time; from pathlib import Path; "
        "Path('grandchild.pid').write_text(str(os.getpid())); time.sleep(60)"
    )
    child = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "Path('child.pid').write_text(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(60)"
    )
    source = f"""
import os, subprocess, sys, time
from pathlib import Path
Path('pytest.pid').write_text(str(os.getpid()))
subprocess.Popen([sys.executable, '-c', {child!r}])
while not Path('grandchild.pid').exists():
    time.sleep(0.01)
Path('ready').touch()
"""
    yield source
    # Keep a regression failure from leaving the test's 60-second sleepers alive.
    for marker in repo.glob("*.pid"):
        pid = int(marker.read_text())
        if process_alive(pid):
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                os.kill(pid, signal.SIGKILL)


def assert_tree_stopped(repo: Path) -> None:
    assert (repo / "ready").exists(), "timeout must happen after descendants start"
    markers = list(repo.glob("*.pid"))
    assert len(markers) == 3
    deadline = time.monotonic() + 2
    while any(process_alive(int(marker.read_text())) for marker in markers):
        assert time.monotonic() < deadline, "pytest or a descendant survived cleanup"
        time.sleep(0.01)


@pytest.mark.parametrize("invoke", [False, True])
def test_timeout_cleans_process_tree(repo: Path, tree: str, invoke: bool) -> None:
    path = write_test(repo, tree + "while True:\n    os.write(1, b'x' * 8192)\n    os.write(2, b'y' * 8192)\n")
    tools = CodebaseTools(repo, timeout_seconds=2)
    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        if invoke:
            asyncio.run(tools.invoke("run_pytest", {"test_path": path}))
        else:
            tools.run_pytest(path)
    assert time.monotonic() - started < 6
    assert_tree_stopped(repo)


def test_cancellation_cleans_tree_before_returning(repo: Path, tree: str) -> None:
    path = write_test(repo, tree + "time.sleep(60)\n")

    async def scenario():
        task = asyncio.create_task(CodebaseTools(repo, timeout_seconds=30).invoke("run_pytest", {"test_path": path}))
        deadline = time.monotonic() + 10
        try:
            while not (repo / "ready").exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - started < 3
            # Still inside the live event loop: asyncio.run shutdown must not be
            # responsible for waiting on the executor or cleaning the processes.
            assert_tree_stopped(repo)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(scenario())


def test_parent_exit_cleans_descendants_holding_pipes(repo: Path, tree: str) -> None:
    path = write_test(repo, tree + "os._exit(0)\n")
    started = time.monotonic()
    assert CodebaseTools(repo, timeout_seconds=10).run_pytest(path)["status"] == "passed"
    assert time.monotonic() - started < 5
    assert_tree_stopped(repo)


def test_interrupt_cleans_tree(repo: Path, tree: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_test(repo, tree + "time.sleep(60)\n")
    read_pipe = _pytest_process.read_pipe
    interrupted = False

    def interrupt(fd, size):
        nonlocal interrupted
        if (repo / "ready").exists() and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return read_pipe(fd, size)

    monkeypatch.setattr(_pytest_process, "read_pipe", interrupt)
    with pytest.raises(KeyboardInterrupt):
        CodebaseTools(repo, timeout_seconds=10).run_pytest(path)
    assert_tree_stopped(repo)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object failure path")
def test_job_assignment_failure_never_starts_pytest(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_test(repo, "from pathlib import Path\nPath('executed').touch()\n")
    pids = []

    def reject(job, pid):
        pids.append(pid)
        raise OSError("job assignment failed")

    monkeypatch.setattr(_pytest_process.WindowsJob, "assign", reject)
    with pytest.raises(OSError, match="job assignment failed"):
        CodebaseTools(repo).run_pytest(path)
    assert len(pids) == 1
    assert not process_alive(pids[0])
    assert not (repo / "executed").exists()


def test_spawn_failure_releases_resources(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_test(repo, "def test_ok(): pass\n")
    closed = []
    if sys.platform == "win32":
        close = _pytest_process.WindowsJob.close

        def record_close(job):
            close(job)
            closed.append(job.handle is None)

        monkeypatch.setattr(_pytest_process.WindowsJob, "close", record_close)

    def reject(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(_pytest_process.subprocess, "Popen", reject)
    with pytest.raises(OSError, match="spawn failed"):
        CodebaseTools(repo).run_pytest(path)
    if sys.platform == "win32":
        assert closed == [True]


def test_concurrent_invocations_have_separate_cancellation(repo: Path) -> None:
    slow = write_test(repo, "import time\nfrom pathlib import Path\nPath('slow-ready').touch()\ntime.sleep(60)\n")
    fast = write_test(repo, "import os,time\nfrom pathlib import Path\nPath('fast-ready').touch()\n"
                      "while not Path('finish-fast').exists(): time.sleep(0.01)\nos._exit(0)\n", "test_fast.py")
    tools = CodebaseTools(repo, timeout_seconds=15)

    async def scenario():
        slow_task = asyncio.create_task(tools.invoke("run_pytest", {"test_path": slow}))
        fast_task = asyncio.create_task(tools.invoke("run_pytest", {"test_path": fast}))
        try:
            deadline = time.monotonic() + 10
            while not all((repo / name).exists() for name in ("slow-ready", "fast-ready")):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            slow_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow_task
            assert not fast_task.done()
            (repo / "finish-fast").touch()
            result, duration = await fast_task
            assert result["status"] == "passed"
            assert duration > 0
        finally:
            for task in (slow_task, fast_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(slow_task, fast_task, return_exceptions=True)

    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_worker_cleanup(repo: Path) -> None:
    started = threading.Event()
    cleaning = threading.Event()
    finish = threading.Event()
    tools = CodebaseTools(repo, timeout_seconds=30)

    def handler(**kwargs):
        from app.tools import _pytest_cancellation
        cancelled = _pytest_cancellation.get()
        started.set()
        assert cancelled.wait(5)
        cleaning.set()
        assert finish.wait(5)
        raise ToolError("pytest timed out")

    tools._handlers["run_pytest"] = handler

    async def scenario():
        task = asyncio.create_task(tools.invoke("run_pytest", {}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            assert await asyncio.to_thread(cleaning.wait, 5)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
