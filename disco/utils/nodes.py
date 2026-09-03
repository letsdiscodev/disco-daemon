from disco.utils import docker
from disco.utils.docker import NodeDetails
from disco.utils.randomname import generate_random_name


async def name_unnamed_nodes() -> list[NodeDetails]:
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    for node in nodes:
        if "disco-name" not in node.labels:
            node.labels["disco-name"] = await generate_random_name()
            await docker.set_node_label(
                node_id=node.id, key="disco-name", value=node.labels["disco-name"]
            )
    return nodes
