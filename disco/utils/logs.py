import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from disco.utils import docker, vectorconfig
from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)


@dataclass
class ActiveSyslog:
    expires: datetime
    service_name: str


syslog_list_lock = asyncio.Lock()
_active_syslogs: list[ActiveSyslog] = []

STREAM_QUEUE_MAX = 1000
# a docker record is at most 16 KB, but vector merges partial records: one line without
# a newline can be as long as the writer made it. above this the connection is dropped
# (the collector reconnects and resends from its buffer, the line is logged and skipped).
STREAM_LINE_LIMIT = 64 * 1024 * 1024
STREAM_TASK_MEMORY_LIMIT = "256m"


def build_streaming_service_args(name: str, config_name: str) -> list[str]:
    """pure: the `docker service create` argv for one `disco logs` collector.

    no buffer volume: the disk buffer lives in the task's own filesystem and goes away
    with the service when the client disconnects.
    """
    return [
        "docker",
        "service",
        "create",
        "--name",
        name,
        "--detach",
        "--mode",
        "global",
        "--label",
        "disco.syslogs",
        "--label",
        f"disco.syslog.config={config_name}",
        "--label",
        f"disco.syslog.image={vectorconfig.VECTOR_IMAGE}",
        "--config",
        f"source={config_name},target={vectorconfig.VECTOR_CONFIG_PATH}",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--network",
        "disco-logging",
        "--limit-memory",
        STREAM_TASK_MEMORY_LIMIT,
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=20m",
        "--log-opt",
        "max-file=5",
        vectorconfig.VECTOR_IMAGE,
        "--config",
        vectorconfig.VECTOR_CONFIG_PATH,
    ]


LogObject = dict[str, str | dict[str, str]]


def parse_stream_line(line: bytes) -> LogObject | None:
    """one json line from the collector -> {"container","labels","timestamp","message"}."""
    try:
        json_str = line.decode("utf-8")
    except UnicodeDecodeError:
        log.error("Failed to UTF-8 decode log line: %r", line[:200])
        return None
    try:
        log_obj = json.loads(json_str)
    except json.decoder.JSONDecodeError:
        log.error("Failed to JSON decode log line: %s", json_str[:200])
        return None
    if not isinstance(log_obj, dict) or not isinstance(log_obj.get("labels"), dict):
        log.error("Unexpected log line shape: %s", json_str[:200])
        return None
    return log_obj


def log_matches(
    log_obj: LogObject, project_name: str | None, service_name: str | None
) -> bool:
    labels = log_obj["labels"]
    assert isinstance(labels, dict)
    if project_name is not None and labels.get("disco.project.name") != project_name:
        return False
    if service_name is not None and labels.get("disco.service.name") != service_name:
        return False
    return True


HISTORY_LINES = 100
_SERVICE_LOG_LINE = re.compile(
    r"^(?P<task>\S+)@(?P<node>\S+)\s+\| (?P<ts>\S+) ?(?P<msg>.*)$"
)


def parse_service_log_line(line: str, labels: dict[str, str]) -> LogObject | None:
    """one line of `docker service logs --timestamps --no-trunc` ->
    {"container","labels","timestamp","message"}. the task name is the container name
    (`<service>.<slot>.<task id>`); the timestamp is docker's (rfc 3339 nanoseconds),
    cut to milliseconds like the live stream."""
    m = _SERVICE_LOG_LINE.match(line)
    if m is None:
        return None
    ts = m.group("ts")
    if len(ts) > 24 and ts.endswith("Z"):
        ts = ts[:23] + "Z"
    return {
        "container": m.group("task"),
        "labels": labels,
        "timestamp": ts,
        "message": m.group("msg"),
    }


async def read_service_history(service_name: str, lines: int) -> list[LogObject]:
    """the last `lines` lines docker retained for a service (every node)."""
    stdout, _, process = await call(
        [
            "docker",
            "service",
            "logs",
            "--timestamps",
            "--no-trunc",
            "--tail",
            str(lines),
            service_name,
        ]
    )
    if process.returncode != 0:
        log.warning("Could not read the history of %s", service_name)
        return []
    labels = await docker.get_service_labels(service_name)
    out = []
    for line in stdout:
        log_obj = parse_service_log_line(line, labels)
        if log_obj is not None:
            out.append(log_obj)
    out.sort(key=lambda o: str(o["timestamp"]))
    return out[-lines:]


async def read_history(
    project_name: str | None, service_name: str | None, lines: int = HISTORY_LINES
) -> list[LogObject]:
    """the last `lines` lines of the selected services, oldest first."""
    services = await docker.list_project_services_with_labels(project_name)
    if service_name is not None:
        services = [
            s for s in services if s.labels.get("disco.service.name") == service_name
        ]
    history: list[LogObject] = []
    for service in services:
        history += await read_service_history(service.name, lines)
    history.sort(key=lambda o: str(o["timestamp"]))
    return history[-lines:]


def history_key(log_obj: LogObject) -> tuple[str, str, str]:
    return (
        str(log_obj["container"]),
        str(log_obj["timestamp"]),
        str(log_obj["message"]),
    )


class LogStreamServer:
    """tcp listener for one `disco logs` client: the collector on every node connects
    and sends json lines; matching lines go into a bounded queue that the sse writer
    drains. when the client is slow the queue fills, this server stops reading, the
    collector's sink blocks and its buffer holds the backlog (backpressure, no drops)."""

    def __init__(
        self,
        port: int,
        log_queue: "asyncio.Queue[LogObject]",
        project_name: str | None,
        service_name: str | None,
    ) -> None:
        self.port = port
        self.log_queue = log_queue
        self.project_name = project_name
        self.service_name = service_name
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port, limit=STREAM_LINE_LIMIT
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Log collector connected from %s on port %d", peer, self.port)
        try:
            while True:
                line = await reader.readline()
                if len(line) == 0:
                    break
                log_obj = parse_stream_line(line.rstrip(b"\n"))
                if log_obj is None:
                    continue
                if not log_matches(log_obj, self.project_name, self.service_name):
                    continue
                await self.log_queue.put(log_obj)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except ValueError:
            log.exception(
                "Log line over %d bytes from %s, dropping the connection",
                STREAM_LINE_LIMIT,
                peer,
            )
        finally:
            writer.close()
            log.info("Log collector from %s disconnected", peer)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


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
    await docker.prune_logging_configs()


async def start_log_collector(service_name: str, config: str) -> str:
    """create the config object and the global collector service; returns the config name."""
    config_name = docker.stream_config_name(config)
    await docker.create_config(config_name, config)
    await check_call(build_streaming_service_args(service_name, config_name))
    return config_name


async def remove_log_collector(service_name: str, config_name: str) -> None:
    try:
        await docker.rm_service(service_name)
    finally:
        await docker.rm_config_if_unused(config_name)
