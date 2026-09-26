from pathlib import Path
import asyncio
import fnmatch
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from ._pytest_runner import run_pytest_process


class ToolError(Exception):
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
        return self._run_pytest(test_path, threading.Event())

    def _run_pytest(self, test_path: str, cancel: threading.Event) -> dict[str, Any]:
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
        try:
            returncode, output = run_pytest_process(
                [sys.executable, "-m", "pytest", command_path, "-q", "-p", "no:cacheprovider"],
                cwd=self.repo_root,
                timeout=self.timeout_seconds,
                cancel=cancel,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError("pytest timed out") from exc
        return {"status": "passed" if returncode == 0 else "failed", "returncode": returncode, "output": output}

    async def invoke(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"unknown tool: {name}")
        started = time.perf_counter()
        if name == "run_pytest":
            cancel = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(self._run_pytest, **arguments, cancel=cancel))
            try:
                done, _ = await asyncio.wait({worker}, timeout=self.timeout_seconds)
                if not done:
                    raise asyncio.TimeoutError
                result = worker.result()
            except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
                cancel.set()
                # Cancelling to_thread alone does not stop its thread. Wait for
                # cleanup before exposing cancellation/timeout, even if cancelled
                # repeatedly while that cleanup is underway.
                while not worker.done():
                    try:
                        await asyncio.wait({worker})
                    except asyncio.CancelledError as cancellation:
                        exc = cancellation
                        continue
                if not worker.cancelled():
                    worker.exception()
                if isinstance(exc, asyncio.TimeoutError):
                    raise ToolError("tool timed out") from exc
                raise exc
            return result, (time.perf_counter() - started) * 1000
        try:
            result = await asyncio.wait_for(asyncio.to_thread(handler, **arguments), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ToolError("tool timed out") from exc
        return result, (time.perf_counter() - started) * 1000
