import asyncio

import friendlywords


def generate_random_name_sync() -> str:
    # sync because it reads from file system
    return friendlywords.generate("po", separator="-")


async def generate_random_name() -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, generate_random_name_sync)
