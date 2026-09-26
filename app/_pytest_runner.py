"""Bounded output collection and lifetime management for a pytest invocation.

POSIX descendants are owned via a process group; Windows descendants via a job.
This is process cleanup, not a sandbox for code that deliberately escapes it.
"""

import codecs
import io
import locale
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

if os.name == "nt":
    from ._windows_process import WindowsJob, read_pipe, resume_process
else:
    def read_pipe(fd: int, size: int) -> bytes | None:
        try:
            return os.read(fd, size)
        except BlockingIOError:
            return None


OUTPUT_LIMIT = 4000
READ_SIZE = 8192
POLL_INTERVAL = 0.01


class _OutputTail:
    def __init__(self) -> None:
        decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))()
        self.decoder = io.IncrementalNewlineDecoder(decoder, translate=True)
        self.text = ""

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        self.text = (self.text + self.decoder.decode(chunk, final=final))[-OUTPUT_LIMIT:]


def run_pytest_process(
    command: list[str], cwd: Path, timeout: float, cancel: threading.Event,
) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    process = None
    job = None
    tails = [_OutputTail(), _OutputTail()]
    tree_stopped = False

    def stop_tree() -> None:
        nonlocal tree_stopped
        if tree_stopped or process is None:
            return
        if job is not None:
            job.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        tree_stopped = True

    try:
        if cancel.is_set():
            raise subprocess.TimeoutExpired(command, timeout)
        if os.name == "nt":
            job = WindowsJob()
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # Assign before any code (including a venv launcher) can spawn a
            # child. A process group alone does not own descendants on Windows.
            creationflags=0x00000004 if job is not None else 0,  # CREATE_SUSPENDED
            bufsize=0, start_new_session=os.name != "nt",
        )
        if job is not None:
            job.assign(process.pid)
            if cancel.is_set() or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            resume_process(process.pid)
        else:
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
        streams = {process.stdout: tails[0], process.stderr: tails[1]}
        while True:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            returncode = process.poll()
            if returncode is not None:
                # Also clean up on ordinary exit: descendants may hold pipes open
                # even after their parent has gone away.
                stop_tree()
            progress = False
            for stream, tail in list(streams.items()):
                chunk = read_pipe(stream.fileno(), READ_SIZE)
                if chunk is not None:
                    progress = True
                    tail.feed(chunk, final=not chunk)
                    if not chunk:
                        del streams[stream]
            if returncode is not None and not streams:
                return returncode, (tails[0].text + tails[1].text)[-OUTPUT_LIMIT:]
            if not progress:
                cancel.wait(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
    finally:
        try:
            stop_tree()
        finally:
            try:
                if process is not None:
                    # Also covers Windows job-assignment/resume failure. Never
                    # leave a suspended process behind.
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            finally:
                if process is not None:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                if job is not None:
                    job.close()
