from pathlib import Path
import asyncio
import fnmatch
import locale
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable


class ToolError(Exception):
    pass


class _OutputTail:
    """Drain a pipe while retaining only a bounded suffix."""

    LIMIT = 16_000

    def __init__(self, pipe: Any) -> None:
        self.pipe = pipe
        self.data = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            while chunk := self.pipe.read(8192):
                self.data.extend(chunk)
                if len(self.data) > self.LIMIT:
                    del self.data[:-self.LIMIT]
        finally:
            self.pipe.close()

    def result(self) -> str:
        self.thread.join()
        return self.data.decode(locale.getpreferredencoding(False), errors="replace")


def _windows_job(process: subprocess.Popen[bytes]) -> int:
    """Put pytest and its descendants in a job killed when its handle closes."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, int(process._handle)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    return job


def _stop_pytest_tree(process: subprocess.Popen[bytes], job: int | None) -> None:
    if sys.platform == "win32":
        if job is not None:
            import ctypes
            from ctypes import wintypes
            close_handle = ctypes.WinDLL("kernel32").CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle(job)
        else:
            # Assignment can fail (for example under a restrictive parent job).
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if process.poll() is None:
                process.kill()
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class CodebaseTools:
    """Read-only tools with path, size and command-scope restrictions."""

    SEARCH_EXCLUDED_DIRS = frozenset(
        {
            "__pycache__",
            ".git",
            ".pytest_cache",
            ".venv",
            "venv",
            "node_modules",
        }
    )
    SEARCH_EXCLUDED_SUFFIXES = frozenset(
        {".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe"}
    )

    def __init__(self, repo_root: Path, timeout_seconds: float = 3) -> None:
        self.repo_root = repo_root.resolve()
        self.timeout_seconds = timeout_seconds
        self._handlers: dict[str, Callable[..., Any]] = {
            "search_code": self.search_code,
            "read_file": self.read_file,
            "run_pytest": self.run_pytest,
        }

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": "search_code", "description": "Find text in source files.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "read_file", "description": "Read a bounded source file range.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "run_pytest", "description": "Run pytest on an allowed test path.", "parameters": {"type": "object", "properties": {"test_path": {"type": "string"}}, "required": ["test_path"]}}},
        ]

    def _safe_path(self, path: str | Path, *, allowed_root: Path | None = None) -> Path:
        try:
            candidate = (self.repo_root / path).resolve()
        except (OSError, RuntimeError) as exc:
            raise ToolError("Access denied: path could not be resolved") from exc
        if not candidate.is_relative_to(self.repo_root):
            raise ToolError("Access denied: path outside working directory (path escapes repository root)")
        if allowed_root is not None and not candidate.is_relative_to(allowed_root):
            raise ToolError("Access denied: path escapes allowed tests directory")
        return candidate

    def _is_search_excluded(self, path: Path) -> bool:
        relative_parts = {part.lower() for part in path.relative_to(self.repo_root).parts}
        return bool(relative_parts & self.SEARCH_EXCLUDED_DIRS) or path.suffix.lower() in self.SEARCH_EXCLUDED_SUFFIXES

    def search_code(self, query: str) -> dict[str, Any]:
        if not query or len(query) > 200:
            raise ToolError("query must contain 1-200 characters")
        matches: list[str] = []
        for path in self.repo_root.rglob("*"):
            if self._is_search_excluded(path):
                continue
            try:
                target = self._safe_path(path)
            except ToolError:
                continue
            try:
                if not target.is_file() or target.stat().st_size > 512_000:
                    continue
                for line_no, line in enumerate(target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if query.lower() in line.lower():
                        matches.append(f"{path.relative_to(self.repo_root)}:{line_no}: {line[:240]}")
                        if len(matches) >= 20:
                            return {"matches": matches}
            except OSError:
                continue
        return {"matches": matches}

    def read_file(self, path: str, start_line: int = 1, end_line: int = 80) -> dict[str, Any]:
        if start_line < 1 or end_line < start_line or end_line - start_line > 120:
            raise ToolError("line range must be between 1 and 120 lines")
        target = self._safe_path(path)
        if not target.is_file() or target.stat().st_size > 512_000:
            raise ToolError("file is missing or too large")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"path": path, "start_line": start_line, "end_line": min(end_line, len(lines)), "content": "\n".join(lines[start_line - 1:end_line])}

    def run_pytest(self, test_path: str, *, _cancel_event: threading.Event | None = None) -> dict[str, Any]:
        requested = Path(test_path)
        if (
            requested.is_absolute()
            or ".." in requested.parts
            or not requested.parts
            or requested.parts[0].lower() != "tests"
            or not fnmatch.fnmatch(requested.name, "test_*.py")
        ):
            raise ToolError("pytest is limited to tests/test_*.py")
        allowed_tests = self.repo_root / "tests"
        target = self._safe_path(test_path, allowed_root=allowed_tests)
        if not target.is_file():
            if (self.repo_root / requested).is_symlink():
                raise ToolError("test file does not exist")
            fallback = self.repo_root / "tests" / "fixture_repo" / requested
            target = self._safe_path(fallback, allowed_root=allowed_tests)
            if not target.is_file():
                raise ToolError("test file does not exist")
        command_path = target.relative_to(self.repo_root).as_posix()
        if _cancel_event is not None and _cancel_event.is_set():
            raise ToolError("pytest cancelled")
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", command_path, "-q", "-p", "no:cacheprovider"],
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
        job: int | None = None
        try:
            if sys.platform == "win32":
                job = _windows_job(process)
            assert process.stdout is not None and process.stderr is not None
            stdout = _OutputTail(process.stdout)
            stderr = _OutputTail(process.stderr)
            deadline = time.monotonic() + self.timeout_seconds
            while process.poll() is None:
                if _cancel_event is not None and _cancel_event.is_set():
                    raise ToolError("pytest cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolError("pytest timed out")
                try:
                    process.wait(timeout=min(remaining, 0.05))
                except subprocess.TimeoutExpired:
                    continue
        finally:
            _stop_pytest_tree(process, job)
            process.wait()
        output = (stdout.result() + stderr.result())[-4000:]
        return {"status": "passed" if process.returncode == 0 else "failed", "returncode": process.returncode, "output": output}

    async def invoke(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"unknown tool: {name}")
        started = time.perf_counter()
        if name == "run_pytest":
            cancel_event = threading.Event()
            task = asyncio.create_task(asyncio.to_thread(self.run_pytest, **arguments, _cancel_event=cancel_event))
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                cancel_event.set()
                try:
                    await asyncio.shield(task)
                except ToolError:
                    pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ToolError("tool timed out") from exc
        else:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(handler, **arguments), timeout=self.timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise ToolError("tool timed out") from exc
        return result, (time.perf_counter() - started) * 1000
