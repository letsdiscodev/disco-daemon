import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from disco.utils import docker
from disco.utils.subprocess import check_call

log = logging.getLogger(__name__)


@dataclass
class ActiveSyslog:
    expires: datetime
    service_name: str


syslog_list_lock = asyncio.Lock()
_active_syslogs: list[ActiveSyslog] = []

def build_streaming_cmd(name: str, port: int) -> list[str]:
    """Build the docker service create command for per-client log streaming.

    Replaces the old LOGSPOUT_CMD. Vector reads docker logs, reformats each
    event into the JSON shape expected by JsonLogServer (container, labels,
    timestamp, message), and sends each as a UDP datagram to disco:{port}.
    """
    config = f"""sources:
  docker:
    type: docker_logs
    docker_host: unix:///var/run/docker.sock
    # Match logspout's BACKLOG=false: only stream new logs, not historical.
    # Vector defaults to ~now-ish, but be explicit so a future default change
    # doesn't dump a flood of old logs on every new client connection.
    since_seconds_ago: 0

transforms:
  reformat:
    type: remap
    inputs:
      - docker
    source: |
      ts = format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%SZ") ?? ""
      cn = to_string(.container_name) ?? ""
      msg = to_string(.message) ?? ""
      labels_obj = .label
      . = {{
        "container": cn,
        "labels": labels_obj,
        "timestamp": ts,
        "message": msg
      }}

sinks:
  out:
    type: socket
    inputs:
      - reformat
    address: "disco:{port}"
    mode: udp
    encoding:
      codec: json
"""
    return [
        "docker",
        "service",
        "create",
        "--name",
        name,
        "--mode",
        "global",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--network",
        "disco-logging",
        "--label",
        "disco.syslogs",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=20m",
        "--log-opt",
        "max-file=5",
        "--env",
        f"DISCO_VECTOR_CONFIG={config}",
        "--entrypoint",
        "sh",
        "timberio/vector:latest-alpine",
        "-c",
        'printf "%s" "$DISCO_VECTOR_CONFIG" > /tmp/vector.yaml && exec vector --config /tmp/vector.yaml',
    ]


class JsonLogServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        log_queue,
        project_name: str | None = None,
        service_name: str | None = None,
    ):
        self.log_queue = log_queue
        self.project_name = project_name
        self.service_name = service_name

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            json_str = data.decode("utf-8")
        except UnicodeDecodeError:
            log.error("Failed to UTF-8 decode log str: %s", data)
            return
        try:
            log_obj = json.loads(json_str)
        except json.decoder.JSONDecodeError:
            log.error("Failed to JSON decode log str: %s", json_str)
            return
        if self.project_name is not None:
            if log_obj["labels"].get("disco.project.name") != self.project_name:
                return
        if self.service_name is not None:
            if log_obj["labels"].get("disco.service.name") != self.service_name:
                return
        self.log_queue.put_nowait(log_obj)

    def connection_lost(self, exception):
        try:
            self.transport.close()
        except Exception:
            pass


async def monitor_syslog(service_name: str) -> None:
    global _active_syslogs
    log.info("Adding %s to the list of monitored syslogs", service_name)
    async with syslog_list_lock:
        _active_syslogs.append(
            ActiveSyslog(
                service_name=service_name,
                expires=datetime.now(timezone.utc) + timedelta(hours=24),
            )
        )


async def get_active_syslogs() -> list[str]:
    global _active_syslogs
    async with syslog_list_lock:
        _active_syslogs = [
            sl for sl in _active_syslogs if sl.expires > datetime.now(timezone.utc)
        ]
        return [sl.service_name for sl in _active_syslogs]


async def get_running_syslogs() -> list[str]:
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        "label=disco.syslogs",
        "--format",
        "{{ .Name }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout


async def clean_up_rogue_syslogs() -> None:
    active_syslogs = set(await get_active_syslogs())
    running_syslogs = await get_running_syslogs()
    for running_syslog in running_syslogs:
        if running_syslog not in active_syslogs:
            log.warning("Killing rogue syslog %s", running_syslog)
            await docker.rm_service(running_syslog)
