from pathlib import Path
import asyncio
import codecs
import ctypes
import fnmatch
import io
import locale
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable


class ToolError(Exception):
    pass


def _windows_job(process: subprocess.Popen[bytes]) -> int:
    """Put pytest in a job so its descendants can be terminated together."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return job


def _stop_pytest_tree(process: subprocess.Popen[bytes], job: int | None) -> None:
    if os.name == "nt":
        if job is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            if not kernel32.TerminateJobObject(job, 1):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")
        else:
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

    def run_pytest(self, test_path: str) -> dict[str, Any]:
        return self._run_pytest(test_path)

    def _run_pytest(self, test_path: str, cancel_event: threading.Event | None = None) -> dict[str, Any]:
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
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", command_path, "-q", "-s", "-p", "no:cacheprovider"],
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        job: int | None = None
        readers: list[threading.Thread] = []
        tails = ["", ""]

        def drain(index: int, stream: Any) -> None:
            decoder = io.IncrementalNewlineDecoder(
                codecs.getincrementaldecoder(locale.getpreferredencoding(False))(errors="replace"),
                translate=True,
            )
            while chunk := stream.read1(16_384):
                tails[index] = (tails[index] + decoder.decode(chunk))[-4000:]
            tails[index] = (tails[index] + decoder.decode(b"", final=True))[-4000:]
            stream.close()

        try:
            if os.name == "nt":
                job = _windows_job(process)
            for index, stream in enumerate((process.stdout, process.stderr)):
                assert stream is not None
                reader = threading.Thread(target=drain, args=(index, stream), daemon=True)
                reader.start()
                readers.append(reader)
            deadline = time.monotonic() + self.timeout_seconds
            while process.poll() is None or any(reader.is_alive() for reader in readers):
                if cancel_event is not None and cancel_event.is_set():
                    raise ToolError("pytest cancelled")
                if time.monotonic() >= deadline:
                    raise ToolError("pytest timed out")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            output = (tails[0] + tails[1])[-4000:]
            return {"status": "passed" if process.returncode == 0 else "failed", "returncode": process.returncode, "output": output}
        except BaseException:
            _stop_pytest_tree(process, job)
            raise
        finally:
            process.wait()
            for reader in readers:
                reader.join()
            if job is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
                kernel32.CloseHandle(job)

    async def invoke(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"unknown tool: {name}")
        started = time.perf_counter()
        if name == "run_pytest":
            cancel_event = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(self._run_pytest, **arguments, cancel_event=cancel_event))
            try:
                result = await asyncio.wait_for(asyncio.shield(worker), timeout=self.timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                cancel_event.set()
                try:
                    await asyncio.shield(worker)
                except ToolError:
                    pass
                if isinstance(exc, asyncio.TimeoutError):
                    raise ToolError("tool timed out") from exc
                raise
        else:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(handler, **arguments), timeout=self.timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise ToolError("tool timed out") from exc
        return result, (time.perf_counter() - started) * 1000
