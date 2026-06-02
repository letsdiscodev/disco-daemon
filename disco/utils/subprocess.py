import asyncio
import logging
from asyncio import subprocess
from typing import Sequence

log = logging.getLogger(__name__)


async def call(
    args: Sequence[str],
    stdin: str | None = None,
    timeout: float | None = None,
) -> tuple[list[str], list[str], subprocess.Process]:
    """Run a subprocess and capture its output.

    When ``timeout`` is set (seconds), the process is killed and a
    ``TimeoutError`` is raised if it does not finish in time. This matters for
    docker/dqlite commands that can hang indefinitely (e.g. ``docker exec`` into
    a half-dead container during cluster recovery) and would otherwise block the
    caller — and any lock it holds — forever. Defaults to ``None`` (no timeout)
    to preserve existing behaviour for long-running commands like image pulls.
    """
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                input=stdin.encode("utf-8") if stdin is not None else None
            ),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        process.kill()
        # Reap the killed process so we don't leak a zombie / orphaned pipes.
        try:
            await process.wait()
        except Exception:
            pass
        raise TimeoutError(f"Command timed out after {timeout}s: {' '.join(args)}")
    await process.wait()
    return decode_output(stdout), decode_output(stderr), process


async def check_call(
    args: Sequence[str],
    stdin: str | None = None,
    timeout: float | None = None,
) -> tuple[list[str], list[str], subprocess.Process]:
    stdout, stderr, process = await call(args=args, stdin=stdin, timeout=timeout)
    if process.returncode != 0:
        for line in stdout:
            log.info("Stdout: %s", line)
        for line in stderr:
            log.info("Stderr: %s", line)
        raise Exception(f"Processs returned status {process.returncode}")
    return stdout, stderr, process


def decode_output(output: bytes) -> list[str]:
    lines = [decode_text(line) for line in output.split(b"\n")]
    if len(lines[-1].strip()) == 0:
        lines = lines[:-1]
    return lines


def decode_text(output_line: bytes) -> str:
    encodings = ["utf-8", "latin-1", "cp1252"]
    for encoding in encodings:
        try:
            return output_line.decode(encoding)
        except UnicodeDecodeError:
            pass
    return output_line.decode("utf-8", errors="replace")
