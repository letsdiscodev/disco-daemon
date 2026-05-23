"""Admin endpoints for the dqlite cluster: status + recovery.

These are emergency-capable: they authenticate via the local backup
file when dqlite itself is locked, so an operator with a valid API key
can still diagnose and repair the cluster during quorum loss.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from disco.auth import get_api_key_emergency_capable
from disco.models.db import AsyncSession
from disco.utils import docker
from disco.utils.cluster_locks import reconciler_lock, recovery_lock
from disco.utils.dqlite import (
    disco_name_from_dqlite_service,
    dqlite_service_name,
    get_current_node_disco_name,
    list_dqlite_services,
    query_cluster_members,
    reconfigure_node_as_single_member,
    scale_dqlite_service,
    wait_for_dqlite_service_healthy,
    wait_for_service_tasks_stopped,
    wipe_dqlite_volume_via_job,
)

log = logging.getLogger(__name__)

router = APIRouter()

LOCK_PROBE_TIMEOUT_S = 3.0


async def _probe_dqlite_locked() -> tuple[bool, str | None]:
    """Returns (is_locked, last_error_message)."""
    try:
        async with asyncio.timeout(LOCK_PROBE_TIMEOUT_S):
            async with AsyncSession.begin() as dbsession:
                await dbsession.execute(text("SELECT 1"))
        return False, None
    except asyncio.TimeoutError:
        return True, f"SELECT 1 timed out after {LOCK_PROBE_TIMEOUT_S}s"
    except OperationalError as e:
        return True, f"OperationalError: {e}"
    except Exception as e:  # noqa: BLE001
        return True, f"{type(e).__name__}: {e}"


@router.get("/api/disco/swarm/dqlite/status")
async def dqlite_status(
    _: Annotated[str, Depends(get_api_key_emergency_capable)],
):
    """Diagnostic snapshot of the dqlite cluster's health.

    All data sources work even when dqlite itself is locked: Swarm
    state comes from the manager's local raft (`docker node ls`),
    cluster lock is a short-timeout probe, dqlite membership comes
    from the local container's view via `.cluster`.
    """
    self_name = await get_current_node_disco_name()
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)

    swarm_nodes = []
    daemon_node_info = None
    for node in nodes:
        info = {
            "discoName": node.labels.get("disco-name") or "",
            "state": node.state,
            "availability": node.availability,
            "role": node.role,
            "address": node.address,
        }
        swarm_nodes.append(info)
        if info["discoName"] == self_name:
            daemon_node_info = {
                **info,
                "isManager": node.role == "manager",
            }

    if daemon_node_info is None:
        raise HTTPException(500, "could not identify daemon's local Swarm node")

    is_locked, last_probe_error = await _probe_dqlite_locked()
    members = await query_cluster_members()

    voters_alive = 0
    voters_total = 0
    if members is not None:
        for m in members:
            if m["role"] == "voter":
                voters_total += 1
                # Reachable iff the node's swarm state is "ready". Not
                # perfect (a ready Swarm node might still have a dead
                # dqlite container) but the most useful proxy from here.
                disco_name_for_member = _disco_name_for_address(m["address"])
                if disco_name_for_member:
                    sn = next(
                        (n for n in swarm_nodes if n["discoName"] == disco_name_for_member),
                        None,
                    )
                    if sn is not None and sn["state"] == "ready":
                        voters_alive += 1
    voters_needed = (voters_total // 2) + 1 if voters_total else 0

    # Decorate each member with reachable + isLocalDaemonNode.
    decorated_members = []
    if members is not None:
        for m in members:
            disco_name_for_member = _disco_name_for_address(m["address"])
            sn = next(
                (n for n in swarm_nodes if n["discoName"] == disco_name_for_member),
                None,
            )
            decorated_members.append({
                "id": m["id"],
                "address": m["address"],
                "role": m["role"],
                "discoName": disco_name_for_member or "",
                "reachable": sn is not None and sn["state"] == "ready",
                "isLocalDaemonNode": disco_name_for_member == self_name,
            })

    # Recovery guidance.
    needed = is_locked
    recommendation = None
    if needed:
        # All Swarm nodes that aren't this daemon's node and are not
        # currently in "ready" state. The operator can edit; we just
        # suggest the most likely-correct set.
        recommended_remove = sorted(
            n["discoName"]
            for n in swarm_nodes
            if n["discoName"] != self_name and n["state"] != "ready"
        )
        recommendation = {
            "keepNode": self_name,
            "removeNodes": recommended_remove,
        }

    return {
        "daemonNode": daemon_node_info,
        "cluster": {
            "isLocked": is_locked,
            "lastProbeError": last_probe_error,
            "voters": {
                "alive": voters_alive,
                "needed": voters_needed,
                "total": voters_total,
            },
            "members": decorated_members if members is not None else None,
        },
        "swarmNodes": swarm_nodes,
        "recoveryGuidance": {
            "needed": needed,
            "recommendation": recommendation,
        },
    }


def _disco_name_for_address(address: str) -> str | None:
    """Reverse-lookup: tasks.dqlite-<disco-name>:9001 -> <disco-name>."""
    if address.startswith("tasks.dqlite-"):
        rest = address[len("tasks."):]
        if ":" in rest:
            rest = rest.split(":", 1)[0]
        return disco_name_from_dqlite_service(rest)
    # Older or alternate naming — best effort.
    return None


class RecoverQuorumRequest(BaseModel):
    keepNode: str
    removeNodes: list[str]


@router.post("/api/disco/swarm/dqlite/recover-quorum")
async def recover_quorum(
    body: RecoverQuorumRequest,
    _: Annotated[str, Depends(get_api_key_emergency_capable)],
):
    """Reconfigure the dqlite cluster after a quorum loss.

    Preconditions (all enforced; 409 on violation):
      - keepNode must equal the daemon's local Swarm node.
      - removeNodes must be a subset of the current Swarm membership,
        not containing keepNode.
      - The cluster must be currently locked (refuse on healthy cluster).

    Implicit rejoin: any alive Swarm node that's neither keepNode nor in
    removeNodes is treated as a survivor whose dqlite data is wiped and
    re-joined to the new cluster as a fresh member.
    """
    started = time.monotonic()
    self_name = await get_current_node_disco_name()

    if body.keepNode != self_name:
        raise HTTPException(409, {
            "error": "keep_node_mismatch",
            "message": (
                "keepNode must equal the daemon's local Swarm node. "
                f"This daemon is on '{self_name}'."
            ),
            "daemonNode": self_name,
        })

    if body.keepNode in body.removeNodes:
        raise HTTPException(400, "keepNode cannot also be in removeNodes")

    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    swarm_node_names = {
        n.labels.get("disco-name") for n in nodes if n.labels.get("disco-name")
    }

    for rn in body.removeNodes:
        if rn not in swarm_node_names:
            raise HTTPException(400, f"unknown node in removeNodes: {rn}")
    if body.keepNode not in swarm_node_names:
        raise HTTPException(409, f"keepNode '{body.keepNode}' is not in the Swarm")

    # Categorize survivors. Anything not in removeNodes and not the keepNode
    # is implicitly a rejoin candidate — but a non-ready node can't be wiped
    # via Swarm task (the job is constrained to that node and will hang).
    # Force the operator to be explicit: either include it in removeNodes
    # or get its state back to ready before retrying.
    ready_names = {
        n.labels.get("disco-name")
        for n in nodes
        if n.labels.get("disco-name") and n.state == "ready"
    }
    rejoin_nodes = sorted(
        ready_names - {body.keepNode} - set(body.removeNodes)
    )
    undecided = sorted(
        (swarm_node_names - ready_names - {body.keepNode}) - set(body.removeNodes)
    )
    if undecided:
        raise HTTPException(400, {
            "error": "undecided_nodes",
            "message": (
                f"Nodes {undecided} are not ready. Either add them to "
                f"removeNodes (to force-remove from Swarm), or fix their "
                f"state before retrying."
            ),
            "undecidedNodes": undecided,
        })

    # Acquire the recovery lock with a tiny timeout — refuses immediately
    # rather than queueing if another recovery is in flight. (Plain
    # `if recovery_lock.locked(): raise` would race between check and
    # acquire below; using wait_for makes the check-and-acquire atomic.)
    try:
        await asyncio.wait_for(recovery_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(409, "another recovery is already in progress")

    try:
        # Re-probe inside the lock — the cluster may have recovered between
        # the request-time probe and now (e.g. another concurrent recovery
        # just finished).
        is_locked_now, _ = await _probe_dqlite_locked()
        if not is_locked_now:
            raise HTTPException(409, {
                "error": "cluster_healthy",
                "message": (
                    "dqlite cluster is currently serving requests. Recovery "
                    "would deliberately break it. Use DELETE /api/disco/swarm/"
                    "nodes/<name> to gracefully remove specific nodes instead."
                ),
            })

        async with reconciler_lock:
            # Phase 1: stop every existing dqlite service so volumes are free.
            existing_services = await list_dqlite_services()
            for svc in existing_services:
                log.info("Scaling %s to 0", svc)
                await docker.scale({svc: 0})
            # Wait for tasks to actually exit and release their volumes —
            # docker.scale is --detach. A blind sleep would race with the
            # reconfigure_node_as_single_member container mounting the
            # same volume the dqlite-demo task hasn't released yet.
            for svc in existing_services:
                try:
                    await wait_for_service_tasks_stopped(svc, timeout_seconds=30)
                except Exception:
                    log.exception(
                        "Service %s did not stop cleanly within 30s; "
                        "continuing — reconfigure may fail if the volume "
                        "is still held.",
                        svc,
                    )

            # Phase 2: rewrite keepNode's raft state as a single-node cluster.
            await reconfigure_node_as_single_member(body.keepNode)

            # Phase 3: bring keepNode's service back online.
            await scale_dqlite_service(body.keepNode, 1)
            await wait_for_dqlite_service_healthy(dqlite_service_name(body.keepNode))

            # Phase 4: rejoin each ready survivor by wiping its data and
            # restarting; it'll JOIN the new cluster via PEERS.
            rejoin_outcomes = []
            for rn in rejoin_nodes:
                try:
                    await wipe_dqlite_volume_via_job(rn)
                    await scale_dqlite_service(rn, 1)
                    await wait_for_dqlite_service_healthy(dqlite_service_name(rn))
                    rejoin_outcomes.append({"name": rn, "status": "rejoined"})
                except Exception as exc:
                    log.exception("Failed to rejoin %s", rn)
                    rejoin_outcomes.append(
                        {"name": rn, "status": "failed", "error": str(exc)}
                    )

            # Phase 5: force-remove the gone nodes from Swarm. Reconciler
            # will then notice their service-orphans and tear them down.
            node_by_name = {n.labels.get("disco-name"): n for n in nodes}
            remove_outcomes = []
            for rn in body.removeNodes:
                node = node_by_name.get(rn)
                if node is None:
                    remove_outcomes.append({"name": rn, "status": "not_found"})
                    continue
                try:
                    await docker.remove_node(node_id=node.id, force=True)
                    remove_outcomes.append({"name": rn, "status": "removed"})
                except Exception as exc:
                    log.exception("Failed to docker node rm %s", rn)
                    remove_outcomes.append(
                        {"name": rn, "status": "failed", "error": str(exc)}
                    )
    finally:
        recovery_lock.release()

    # Phase 6 (outside the locks so reconciler can run): final sweep.
    from disco.utils.swarmreconciler import reconcile_dqlite_services
    await reconcile_dqlite_services()

    return {
        "status": "ok",
        "keptNode": body.keepNode,
        "rejoinedNodes": rejoin_outcomes,
        "removedNodes": remove_outcomes,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }
