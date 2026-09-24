"""Bounded pytest output and process cleanup for trusted local repositories.

POSIX process groups cover ordinary descendants, not programs deliberately
escaping with setsid/setpgid. This runner is not an execution sandbox.
"""

import locale
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

if os.name == "nt":
    from ._windows_job import WindowsJob, read_available
else:
    def read_available(fd: int, size: int) -> bytes:
        try:
            return os.read(fd, size)
        except BlockingIOError:
            return b""


STREAM_LIMIT_BYTES = 64 * 1024
READ_CHUNK_BYTES = 16 * 1024
OUTPUT_LIMIT_CHARS = 4000
CLEANUP_TIMEOUT_SECONDS = 5.0

# Isolated startup prevents repository/site code from running before assignment
# to the Windows job. Only the launcher is isolated: pytest retains its usual
# interpreter, environment, cwd and import behavior, and inherits the job.
_WINDOWS_LAUNCHER = (
    "import sys; "
    "gate = sys.stdin.buffer.read(1); "
    "sys.exit(1) if gate != b'1' else None; "
    "import subprocess; "
    "sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))"
)


def _append_tail(tail: bytearray, chunk: bytes) -> None:
    excess = len(tail) + len(chunk) - STREAM_LIMIT_BYTES
    if excess > 0:
        del tail[:excess]
    tail.extend(chunk[-STREAM_LIMIT_BYTES:])


def _format_output(stdout: bytearray, stderr: bytearray) -> str:
    encoding = locale.getpreferredencoding(False)
    out = stdout.decode(encoding, errors="replace")
    err = stderr.decode(encoding, errors="replace")
    if not err:
        return out[-OUTPUT_LIMIT_CHARS:]
    if not out:
        return err[-OUTPUT_LIMIT_CHARS:]
    # Reserve room for both streams, so a noisy stderr cannot erase stdout.
    header = "[stdout]\n"
    separator = "\n[stderr]\n"
    budget = OUTPUT_LIMIT_CHARS - len(header) - len(separator)
    err_size = min(len(err), max(budget // 2, budget - len(out)))
    return header + out[-(budget - err_size):] + separator + err[-err_size:]


def run_pytest_process(repo_root: Path, command_path: str, timeout: float, cancelled: threading.Event) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    tails = (bytearray(), bytearray())
    # Let this runner capture output; pytest's default capture can itself spool
    # unbounded test output and materialize it when constructing a report.
    command = [sys.executable, "-m", "pytest", command_path, "-q", "-p", "no:cacheprovider", "--capture=no"]
    job = WindowsJob() if os.name == "nt" else None
    process = None
    streams = []
    pipes_ready = False
    try:
        if cancelled.is_set():
            raise InterruptedError("pytest cancelled")
        options: dict[str, Any] = {"start_new_session": True}
        if job is not None:
            command = [sys.executable, "-I", "-S", "-c", _WINDOWS_LAUNCHER, *command]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW}
        process = subprocess.Popen(
            command, cwd=repo_root, stdin=subprocess.PIPE if job else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, shell=False, **options,
        )
        streams = [process.stdout, process.stderr]
        if job is not None:
            # The gate stays closed on assignment failure: no pytest code runs.
            job.assign(process.pid)
            process.stdin.write(b"1")
            process.stdin.close()
        else:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
        pipes_ready = True

        while True:
            if cancelled.is_set():
                raise InterruptedError("pytest cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("pytest timed out")
            received = False
            for stream, tail in zip(streams, tails):
                chunk = read_available(stream.fileno(), READ_CHUNK_BYTES)
                _append_tail(tail, chunk)
                received |= bool(chunk)
            if process.poll() is not None:
                break
            if not received:
                cancelled.wait(min(0.01, max(0, deadline - time.monotonic())))
    finally:
        try:
            if process is not None:
                try:
                    if job is not None:
                        job.terminate(CLEANUP_TIMEOUT_SECONDS)
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
                # Drain bytes already in the pipes without waiting for EOF from
                # an escaped descendant. There are no blocking reader threads.
                drain_deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
                while pipes_ready and time.monotonic() < drain_deadline:
                    received = False
                    for stream, tail in zip(streams, tails):
                        chunk = read_available(stream.fileno(), READ_CHUNK_BYTES)
                        _append_tail(tail, chunk)
                        received |= bool(chunk)
                    if not received:
                        break
        finally:
            for stream in streams:
                stream.close()
            if process is not None and process.stdin is not None:
                process.stdin.close()
            if job is not None:
                job.close()
    return {
        "status": "passed" if process.returncode == 0 else "failed",
        "returncode": process.returncode,
        "output": _format_output(*tails),
    }
