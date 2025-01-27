import asyncio

import aiohttp


class AsyncDockerStats:
    def __init__(self, docker_socket="/var/run/docker.sock"):
        self.docker_socket = docker_socket

    async def get_container_stats(self, container_id):
        async with aiohttp.UnixConnector(path=self.docker_socket) as connector:
            async with aiohttp.ClientSession(connector=connector) as session:
                url = f"http://localhost/containers/{container_id}/stats?stream=false"
                async with session.get(url) as response:
                    return await response.json()

    async def get_all_container_stats(self):
        async with aiohttp.UnixConnector(path=self.docker_socket) as connector:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get("http://localhost/containers/json") as response:
                    containers = await response.json()
                tasks = [self.get_container_stats(c["Id"]) for c in containers]
                return [
                    stats for stats in await asyncio.gather(*tasks) if stats is not None
                ]
