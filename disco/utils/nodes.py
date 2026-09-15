import asyncio

from disco.utils import docker
from disco.utils.docker import NodeDetails
from disco.utils.randomname import generate_random_name

_naming_lock = asyncio.Lock()


async def name_unnamed_nodes() -> list[NodeDetails]:
    async with _naming_lock:
        node_ids = await docker.get_node_list()
        nodes = await docker.get_node_details(node_ids)
        unnamed = [node for node in nodes if "disco-name" not in node.labels]
        for node in unnamed:
            await docker.set_node_label(
                node_id=node.id, key="disco-name", value=await generate_random_name()
            )
        if len(unnamed) > 0:
            nodes = await docker.get_node_details(node_ids)
        return nodes
