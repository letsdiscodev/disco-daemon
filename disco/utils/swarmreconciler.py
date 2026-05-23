"""Reconcile per-node dqlite Swarm services with current Swarm node set.

Kubernetes-controller pattern: a periodic loop + an event-driven trigger
both call reconcile_dqlite_services(), which is idempotent. The function
asks Swarm "what nodes do you have?" and "what dqlite-* services do you
have?" and reshapes the latter to match the former:

  - Ready node without a disco-name → assign one (label).
  - Node with disco-name but no matching service → create the service.
  - Service with no matching node → remove it and evict the node's
    advertised address from the dqlite cluster.

Single-flight via an asyncio.Lock; concurrent triggers fold into one run.
"""

from __future__ import annotations

import logging

from disco.utils import docker
from disco.utils.cluster_locks import reconciler_lock, recovery_lock
from disco.utils.dqlite import (
    cluster_remove,
    disco_name_from_dqlite_service,
    dqlite_bind_address,
    list_dqlite_services,
    remove_dqlite_service,
    start_dqlite_service,
)
from disco.utils.randomname import generate_random_name

log = logging.getLogger(__name__)


async def reconcile_dqlite_services() -> None:
    """Single-flight reconcile pass. Safe to call from any number of callers."""
    if recovery_lock.locked():
        # Recovery is rewriting cluster state; standing back is correct.
        log.info("Recovery in progress; skipping reconcile")
        return
    if reconciler_lock.locked():
        # Another reconcile pass is already running; it'll pick up any
        # change we'd have noticed.
        log.debug("reconcile already in progress, skipping")
        return
    async with reconciler_lock:
        try:
            await _reconcile_once()
        except Exception:
            log.exception("reconcile_dqlite_services failed")


async def _reconcile_once() -> None:
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)

    # Ensure every node has a disco-name label.
    for node in nodes:
        if "disco-name" not in node.labels or not node.labels["disco-name"]:
            new_name = await generate_random_name()
            log.info("Assigning disco-name %s to node %s", new_name, node.id)
            await docker.set_node_label(
                node_id=node.id, key="disco-name", value=new_name
            )
            node.labels["disco-name"] = new_name

    # The set of disco-names that should have a dqlite service. We
    # intentionally include nodes that are currently Down — the cluster
    # is fine without them temporarily and we don't want to evict a node
    # just because its host bounced.
    desired_names = {
        node.labels["disco-name"] for node in nodes if node.labels.get("disco-name")
    }
    existing_services = await list_dqlite_services()
    existing_names = {
        n
        for n in (disco_name_from_dqlite_service(s) for s in existing_services)
        if n is not None
    }

    # Create services for nodes that don't have one yet. New nodes always
    # join via the union of already-existing services as PEERS; never set
    # BOOTSTRAP_ALLOWED here — bootstrap only happens once, in init.py.
    missing = desired_names - existing_names
    for name in missing:
        peers = sorted(dqlite_bind_address(n) for n in existing_names if n != name)
        if not peers:
            log.warning(
                "No existing peers for new node %s and BOOTSTRAP_ALLOWED is not "
                "set by reconciler; skipping (init.py creates the very first "
                "node, not the reconciler)",
                name,
            )
            continue
        try:
            await start_dqlite_service(
                disco_name=name, peers=peers, bootstrap_allowed=False
            )
            existing_names.add(name)
        except Exception:
            log.exception("Failed to create dqlite service for %s", name)

    # Remove services whose node is no longer in the swarm. We also evict
    # the dead node's advertised address from the dqlite cluster so role-
    # management doesn't keep a phantom voter.
    orphan = existing_names - desired_names
    for name in orphan:
        log.info("Removing orphan dqlite service for departed node %s", name)
        # Evict first while the service's address is still in the cluster's
        # raft view; once the service is gone there may be no peer at that
        # address for the leader to inspect on a stuck join elsewhere.
        evicted = await cluster_remove(dqlite_bind_address(name))
        if not evicted:
            log.info(
                "Cluster .remove for %s did not succeed; will retry on next reconcile",
                name,
            )
        try:
            await remove_dqlite_service(name)
        except Exception:
            log.exception("Failed to remove dqlite service for %s", name)
