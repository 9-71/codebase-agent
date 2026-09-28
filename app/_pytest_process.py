"""Bounded pytest output collection and ownership of its process tree.

This is lifecycle management, not a sandbox: on POSIX a deliberately detached
process can escape the process group.
"""

import codecs
import io
import locale
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

if sys.platform == "win32":
    from ._windows_process import WindowsJob, read_pipe
else:
    def read_pipe(fd: int, size: int) -> bytes | None:
        try:
            return os.read(fd, size)
        except BlockingIOError:
            return None


OUTPUT_LIMIT = 4000
READ_SIZE = 8192


class _OutputTail:
    def __init__(self) -> None:
        encoding = "utf-8" if sys.flags.utf8_mode else locale.getpreferredencoding(False)
        self.decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder(encoding)(errors="replace"), translate=True,
        )
        self.text = ""

    def append(self, chunk: bytes, *, final: bool = False) -> None:
        self.text = (self.text + self.decoder.decode(chunk, final))[-OUTPUT_LIMIT:]


def run_pytest_process(command: list[str], cwd: Path, timeout: float,
                       cancelled: threading.Event) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    job = None
    process = None
    streams = []
    tails = [_OutputTail(), _OutputTail()]
    try:
        if cancelled.is_set():
            raise subprocess.TimeoutExpired(command, timeout)
        if sys.platform == "win32":
            job = WindowsJob()
            # The isolated stdlib launcher cannot start pytest until assignment
            # succeeds. This closes the spawn/assign race for test descendants.
            launcher = (
                "import subprocess,sys; "
                "sys.exit(subprocess.call(sys.argv[1:]) "
                "if sys.stdin.buffer.read(1) == b'1' else 1)"
            )
            args = [sys.executable, "-I", "-S", "-c", launcher, *command]
        else:
            args = command
        process = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if job is not None else None,
            bufsize=0, start_new_session=sys.platform != "win32",
        )
        streams = [process.stdout, process.stderr]
        if job is not None:
            job.assign(process.pid)
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            process.stdin.write(b"1")
            process.stdin.close()
        else:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)

        while True:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            received = False
            # One bounded read per stream per iteration: neither a noisy stream
            # nor an inherited pipe can starve cancellation/deadline checks.
            for stream, tail in zip(streams, tails):
                chunk = read_pipe(stream.fileno(), READ_SIZE)
                if chunk:
                    tail.append(chunk)
                    received = True
            if process.poll() is not None:
                break
            if not received:
                cancelled.wait(min(0.01, max(0, deadline - time.monotonic())))
    finally:
        # Also clean up on normal exit: descendants may still hold our pipes.
        try:
            if job is not None:
                job.close()
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if process is not None:
                try:
                    # Handles a launcher whose job assignment failed as well.
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    # Drain only bounded chunks, with a finite cleanup deadline
                    # even if an out-of-scope detached process holds a pipe open.
                    drain_deadline = time.monotonic() + 1
                    pending = list(zip(streams, tails))
                    while pending and time.monotonic() < drain_deadline:
                        received = False
                        for stream, tail in pending[:]:
                            chunk = read_pipe(stream.fileno(), READ_SIZE)
                            if chunk is not None:
                                tail.append(chunk, final=not chunk)
                                received = True
                                if not chunk:
                                    pending.remove((stream, tail))
                        if not received:
                            time.sleep(0.01)
                finally:
                    for stream in streams:
                        stream.close()
                    if process.stdin is not None:
                        process.stdin.close()
    return process.returncode, (tails[0].text + tails[1].text)[-OUTPUT_LIMIT:]
