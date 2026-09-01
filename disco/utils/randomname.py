import asyncio

import friendlywords


def _generate() -> str:
    # reads word lists from the file system
    return friendlywords.generate("po", separator="-")  # type: ignore[attr-defined]


async def generate_random_name() -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _generate)
