import asyncio
import logging
from asyncio import subprocess
from collections.abc import AsyncGenerator
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


async def check_call_streaming(
    args: Sequence[str], timeout: float = 600
) -> AsyncGenerator[str, None]:
    """Run a command, yielding each output line as it arrives."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    output = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), deadline - loop.time()
                )
            except TimeoutError:
                raise Exception(
                    f"Running command failed, timeout after {timeout} seconds"
                )
            if len(line) == 0:
                break
            decoded_line = decode_text(line)
            output += decoded_line
            yield decoded_line
        await process.wait()
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
    if process.returncode != 0:
        raise Exception(f"Process returned status {process.returncode}:\n{output}")
