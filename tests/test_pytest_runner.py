import asyncio
import os
from pathlib import Path
import signal
import textwrap
import time
import tracemalloc

import pytest

from app import pytest_runner
from app.tools import CodebaseTools, ToolError


def write_test(repo: Path, source: str) -> None:
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_resource.py").write_text(textwrap.dedent(source), encoding="utf-8")


def is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            assert ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: no such PID
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        # An orphan may await reaping by PID 1 after it has already exited.
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def assert_processes_stopped(repo: Path) -> None:
    pids = [int(path.read_text()) for path in repo.glob("*.pid")]
    deadline = time.monotonic() + 3
    while any(is_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pids
    assert not [pid for pid in pids if is_running(pid)]


def cleanup_test_processes(repo: Path) -> None:
    """Keep a failing lifecycle regression from leaking its test processes."""
    for path in repo.glob("*.pid"):
        pid = int(path.read_text())
        if not is_running(pid):
            continue
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(1, False, pid)
            if handle:
                kernel.TerminateProcess(handle, 1)
                kernel.CloseHandle(handle)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def write_process_tree_test(repo: Path, *, parent_exits: bool = False) -> None:
    grandchild = "import os,time; from pathlib import Path; Path('grandchild.pid').write_text(str(os.getpid())); time.sleep(60)"
    child = (
        "import os,subprocess,sys,time; from pathlib import Path; "
        "Path('child.pid').write_text(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(60)"
    )
    write_test(repo, f"""
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time

        def test_tree():
            Path('parent.pid').write_text(str(os.getpid()))
            subprocess.Popen([sys.executable, '-c', {child!r}])
            deadline = time.monotonic() + 10
            while not Path('grandchild.pid').exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            {'return' if parent_exits else 'time.sleep(60)'}
    """)


def test_stdout_and_stderr_are_bounded_during_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_test(tmp_path, """
        import os

        def test_output():
            for _ in range(128):
                os.write(1, b'O' * 65536)
                os.write(2, b'E' * 65536)
            os.write(1, b'\\nSTDOUT_END\\n')
            os.write(2, b'\\nSTDERR_END\\n')
    """)
    sizes = [0, 0]
    buffers = []
    original = pytest_runner._append_tail

    def checked_append(tail, chunk):
        if not any(tail is item for item in buffers):
            buffers.append(tail)
        original(tail, chunk)
        index = next(i for i, item in enumerate(buffers) if item is tail)
        sizes[index] = max(sizes[index], len(tail))
        assert len(chunk) <= pytest_runner.READ_CHUNK_BYTES
        assert len(tail) <= pytest_runner.STREAM_LIMIT_BYTES

    monkeypatch.setattr(pytest_runner, "_append_tail", checked_append)
    tracemalloc.start()
    try:
        result = CodebaseTools(tmp_path, timeout_seconds=30).run_pytest("tests/test_resource.py")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["status"] == "passed", result
    assert len(result["output"]) <= 4000
    assert "STDOUT_END" in result["output"]
    assert "STDERR_END" in result["output"]
    assert sizes == [pytest_runner.STREAM_LIMIT_BYTES] * 2
    assert peak < 4 * 1024 * 1024  # 16 MiB emitted, without accumulating it in the parent


@pytest.mark.parametrize("via_invoke", [False, True])
def test_timeout_reaps_pytest_and_descendants(tmp_path: Path, via_invoke: bool) -> None:
    write_process_tree_test(tmp_path)
    tools = CodebaseTools(tmp_path, timeout_seconds=3)
    started = time.monotonic()
    try:
        with pytest.raises(ToolError, match="pytest timed out"):
            if via_invoke:
                asyncio.run(tools.invoke("run_pytest", {"test_path": "tests/test_resource.py"}))
            else:
                tools.run_pytest("tests/test_resource.py")
        assert time.monotonic() - started < 10
        assert {p.name for p in tmp_path.glob("*.pid")} == {"parent.pid", "child.pid", "grandchild.pid"}
        assert_processes_stopped(tmp_path)
    finally:
        cleanup_test_processes(tmp_path)


def test_success_also_cleans_up_background_descendants(tmp_path: Path) -> None:
    write_process_tree_test(tmp_path, parent_exits=True)
    try:
        result = CodebaseTools(tmp_path, timeout_seconds=10).run_pytest("tests/test_resource.py")
        assert result["status"] == "passed", result
        assert (tmp_path / "grandchild.pid").exists()
        assert_processes_stopped(tmp_path)
    finally:
        cleanup_test_processes(tmp_path)


def test_async_cancellation_waits_for_worker_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_process_tree_test(tmp_path)
    tools = CodebaseTools(tmp_path, timeout_seconds=30)
    finished = False
    original = tools._run_pytest

    def tracked_run(*args, **kwargs):
        nonlocal finished
        try:
            return original(*args, **kwargs)
        finally:
            finished = True

    monkeypatch.setattr(tools, "_run_pytest", tracked_run)

    async def exercise():
        task = asyncio.create_task(tools.invoke("run_pytest", {"test_path": "tests/test_resource.py"}))
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "grandchild.pid").exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Check inside the event loop: asyncio.run's executor shutdown must
            # not be the reason the worker has finished.
            assert finished
            assert_processes_stopped(tmp_path)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(exercise())
    finally:
        cleanup_test_processes(tmp_path)


def test_missing_path_never_executes_fixture_fallback(tmp_path: Path) -> None:
    fallback = tmp_path / "tests" / "fixture_repo" / "tests"
    fallback.mkdir(parents=True)
    (fallback / "test_resource.py").write_text(
        "from pathlib import Path\nPath('unexpected').write_text('executed')\ndef test_ok(): pass\n",
        encoding="utf-8",
    )
    with pytest.raises(ToolError, match="test file does not exist"):
        CodebaseTools(tmp_path).run_pytest("tests/test_resource.py")
    assert not (tmp_path / "unexpected").exists()


def test_failed_test_preserves_status_and_tail(tmp_path: Path) -> None:
    write_test(tmp_path, """
        def test_failure():
            assert False, 'failure-detail'
    """)
    result = CodebaseTools(tmp_path, timeout_seconds=10).run_pytest("tests/test_resource.py")
    assert result["status"] == "failed"
    assert result["returncode"] == 1
    assert "failure-detail" in result["output"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object startup gate")
def test_job_assignment_failure_never_starts_repository_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_test(tmp_path, "def test_ok(): pass\n")
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\nPath('unexpected').write_text('startup code executed')\n", encoding="utf-8",
    )
    processes = []
    popen = pytest_runner.subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def reject_assignment(self, pid):
        raise OSError("job assignment denied")

    monkeypatch.setattr(pytest_runner.subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(pytest_runner.WindowsJob, "assign", reject_assignment)
    with pytest.raises(ToolError, match="job assignment denied"):
        CodebaseTools(tmp_path).run_pytest("tests/test_resource.py")
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert not (tmp_path / "unexpected").exists()
