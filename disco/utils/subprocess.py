import asyncio
import logging
from asyncio import subprocess
from typing import Sequence

log = logging.getLogger(__name__)


async def call(
    args: Sequence[str], stdin: str | None = None
) -> tuple[list[str], list[str], subprocess.Process]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(
        input=stdin.encode("utf-8") if stdin is not None else None
    )
    await process.wait()
    return decode_output(stdout), decode_output(stderr), process


async def check_call(
    args: Sequence[str], stdin: str | None = None
) -> tuple[list[str], list[str], subprocess.Process]:
    stdout, stderr, process = await call(args=args, stdin=stdin)
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
