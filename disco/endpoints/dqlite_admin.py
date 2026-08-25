from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
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
    # Both probes must fail; a single transient error must not unlock recovery.
    is_locked_1, err_1 = await _single_probe()
    if not is_locked_1:
        return False, None
    await asyncio.sleep(1)
    is_locked_2, err_2 = await _single_probe()
    if not is_locked_2:
        return False, None
    return True, err_2 or err_1


async def _single_probe() -> tuple[bool, str | None]:
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
                disco_name_for_member = _disco_name_for_address(m["address"])
                if disco_name_for_member:
                    sn = next(
                        (n for n in swarm_nodes if n["discoName"] == disco_name_for_member),
                        None,
                    )
                    if sn is not None and sn["state"] == "ready":
                        voters_alive += 1
    voters_needed = (voters_total // 2) + 1 if voters_total else 0

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

    needed = is_locked
    recommendation = None
    if needed:
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
    if address.startswith("tasks.dqlite-"):
        rest = address[len("tasks."):]
        if ":" in rest:
            rest = rest.split(":", 1)[0]
        name = disco_name_from_dqlite_service(rest)
        return name if name else None
    return None


class RecoverQuorumRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    keep_node: str = Field(..., alias="keepNode")
    remove_nodes: list[str] = Field(..., alias="removeNodes")


@router.post("/api/disco/swarm/dqlite/recover-quorum")
async def recover_quorum(
    body: RecoverQuorumRequest,
    _: Annotated[str, Depends(get_api_key_emergency_capable)],
):
    started = time.monotonic()
    self_name = await get_current_node_disco_name()

    if body.keep_node != self_name:
        raise HTTPException(409, {
            "error": "keep_node_mismatch",
            "message": (
                "keepNode must equal the daemon's local Swarm node. "
                f"This daemon is on '{self_name}'."
            ),
            "daemonNode": self_name,
        })

    if body.keep_node in body.remove_nodes:
        raise HTTPException(400, "keepNode cannot also be in removeNodes")

    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    swarm_node_names = {
        name for n in nodes if (name := n.labels.get("disco-name"))
    }

    for rn in body.remove_nodes:
        if rn not in swarm_node_names:
            raise HTTPException(400, f"unknown node in removeNodes: {rn}")
    if body.keep_node not in swarm_node_names:
        raise HTTPException(409, f"keepNode '{body.keep_node}' is not in the Swarm")

    # A non-ready node cannot be wiped (the wipe job is constrained to it and
    # would hang), so the operator must put it in removeNodes.
    ready_names = {
        name
        for n in nodes
        if (name := n.labels.get("disco-name")) and n.state == "ready"
    }
    rejoin_nodes = sorted(
        ready_names - {body.keep_node} - set(body.remove_nodes)
    )
    undecided = sorted(
        (swarm_node_names - ready_names - {body.keep_node}) - set(body.remove_nodes)
    )
    if undecided:
        raise HTTPException(400, {
            "error": "undecided_nodes",
            "message": (
                f"Nodes {undecided} are not ready. Add them to removeNodes "
                f"or fix their state before retrying."
            ),
            "undecidedNodes": undecided,
        })

    # wait_for makes the check-and-acquire atomic; a locked() check would race.
    try:
        await asyncio.wait_for(recovery_lock.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(409, "another recovery is already in progress")

    try:
        # Re-probe under the lock; the cluster may have recovered since the
        # first probe.
        is_locked_now, _probe_err = await _probe_dqlite_locked()
        if not is_locked_now:
            raise HTTPException(409, {
                "error": "cluster_healthy",
                "message": (
                    "dqlite cluster is serving requests; recovery refused. "
                    "Use DELETE /api/disco/swarm/nodes/<name> to remove nodes."
                ),
            })

        async with reconciler_lock:
            existing_services = await list_dqlite_services()
            for svc in existing_services:
                log.info("Scaling %s to 0", svc)
                await docker.scale({svc: 0})
            # docker.scale is --detach; the reconfigure container must not mount
            # a volume a task still holds.
            for svc in existing_services:
                try:
                    await wait_for_service_tasks_stopped(svc, timeout_seconds=30)
                except Exception:
                    log.exception(
                        "Service %s did not stop within 30s; continuing",
                        svc,
                    )

            await reconfigure_node_as_single_member(body.keep_node)

            await scale_dqlite_service(body.keep_node, 1)
            await wait_for_dqlite_service_healthy(dqlite_service_name(body.keep_node))

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

            node_by_name = {n.labels.get("disco-name"): n for n in nodes}
            remove_outcomes = []
            for rn in body.remove_nodes:
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

    # Outside the locks so the reconciler can run.
    from disco.utils.swarmreconciler import reconcile_dqlite_services
    await reconcile_dqlite_services()

    return {
        "status": "ok",
        "keptNode": body.keep_node,
        "rejoinedNodes": rejoin_outcomes,
        "removedNodes": remove_outcomes,
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }
