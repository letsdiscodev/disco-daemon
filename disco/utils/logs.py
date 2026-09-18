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

# the daemon holds at most STREAM_QUEUE_MAX lines of at most STREAM_LINE_LIMIT bytes per
# client (128 MB worst case); the rest waits in the collector's buffer (backpressure)
STREAM_QUEUE_MAX = 500
# every open `disco logs` is one more collector task per node reading the docker
# socket, plus a queue in the daemon (prd 2.2)
MAX_STREAMS = 10
# a docker record is at most 16 KB; vector merges partial records, so one line without
# a newline can be longer. above this the connection is dropped (the collector
# reconnects and resends from its buffer, the line is logged and skipped).
STREAM_LINE_LIMIT = 256 * 1024
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
    r"^(?P<ts>\S+) (?P<task>\S+)@(?P<node>\S+)\s+\| ?(?P<msg>.*)$"
)


def parse_service_log_line(line: str, labels: dict[str, str]) -> LogObject | None:
    """one line of `docker service logs --timestamps --no-trunc` ->
    {"container","labels","timestamp","message"}. the line is
    `<rfc 3339 ns timestamp> <task name>@<node>    | <message>`; the task name is the
    container name (`<service>.<slot>.<task id>`); the timestamp is cut to
    milliseconds like the live stream."""
    m = _SERVICE_LOG_LINE.match(line)
    if m is None:
        return None
    ts = m.group("ts")
    ms = ts[:23] + "Z" if len(ts) > 24 and ts.endswith("Z") else ts
    return {
        "container": m.group("task"),
        "labels": labels,
        "timestamp": ms[:19] + "Z" if len(ms) >= 20 else ms,
        "ts": ms,
        "message": m.group("msg"),
    }


async def read_service_history(service_name: str, lines: int) -> list[LogObject]:
    """the last `lines` lines docker retained for a service (every node)."""
    # stdout and stderr of the containers come back on the matching streams
    stdout, stderr, process = await call(
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
    for line in stdout + stderr:
        log_obj = parse_service_log_line(line, labels)
        if log_obj is not None:
            out.append(log_obj)
    out.sort(key=lambda o: str(o.get("ts", o["timestamp"])))
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
    history.sort(key=lambda o: str(o.get("ts", o["timestamp"])))
    return history[-lines:]


def history_key(log_obj: LogObject) -> tuple[str, str, str]:
    """container + millisecond timestamp + message: the same line in history and live."""
    return (
        str(log_obj["container"]),
        str(log_obj.get("ts", log_obj["timestamp"])),
        str(log_obj["message"]),
    )


def for_client(log_obj: LogObject) -> LogObject:
    """what the cli and dashboard get: the four keys logspout sent, seconds timestamp."""
    return {k: v for k, v in log_obj.items() if k != "ts"}


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
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task] = set()
        # one collector task per node connects; the source of each is running by then
        self.connections = 0
        self.connected = asyncio.Condition()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port, limit=STREAM_LINE_LIMIT
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Log collector connected from %s on port %d", peer, self.port)
        self._writers.add(writer)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        async with self.connected:
            self.connections += 1
            self.connected.notify_all()
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    # one record over the limit: skip it, keep the connection
                    await _skip_line(reader)
                    log.warning(
                        "Skipped a log record over %d bytes from %s",
                        STREAM_LINE_LIMIT,
                        peer,
                    )
                    continue
                except asyncio.IncompleteReadError as e:
                    line = e.partial
                    if len(line) == 0:
                        break
                log_obj = parse_stream_line(line.rstrip(b"\n"))
                if log_obj is None:
                    continue
                if not log_matches(log_obj, self.project_name, self.service_name):
                    continue
                await self.log_queue.put(log_obj)
        except ConnectionResetError:
            pass
        except asyncio.CancelledError:
            # close(): the client is gone, a put() blocked on a full queue must not
            # keep this handler and its lines alive
            pass
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            log.info("Log collector from %s disconnected", peer)

    async def wait_for_connections(self, count: int, timeout: float) -> bool:
        """true once `count` collector tasks have connected (one per node)."""
        try:
            async with self.connected:
                await asyncio.wait_for(
                    self.connected.wait_for(lambda: self.connections >= count),
                    timeout=timeout,
                )
            return True
        except TimeoutError:
            return False

    async def close(self) -> None:
        """stop listening and drop the collector connections. `wait_closed` waits for
        every open connection since python 3.12, and the collector keeps its
        connection until it is removed, so the connections are closed here first
        and the wait is bounded."""
        if self.server is None:
            return
        self.server.close()
        for writer in list(self._writers):
            writer.close()
        for handler in list(self._handlers):
            handler.cancel()
        try:
            await asyncio.wait_for(self.server.wait_closed(), timeout=5)
        except TimeoutError:
            log.warning("Log stream server on port %d did not close in time", self.port)


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
    """remove the collector; a `docker service create` cancelled mid-way can still
    commit after this runs, so a missing service is looked for again for a while."""
    try:
        for _ in range(10):
            if await docker.service_exists(service_name):
                await docker.rm_service(service_name)
                break
            await asyncio.sleep(3)
    finally:
        docker.cleanup_in_background(
            f"config {config_name}", lambda: docker._config_removed(config_name)
        )


async def _skip_line(reader: asyncio.StreamReader) -> None:
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk or b"\n" in chunk:
            return


async def remove_all_log_collectors() -> None:
    """at boot: no client can be attached to a daemon that just started, every
    streaming collector left by the previous process is an orphan."""
    for name in await get_running_syslogs():
        log.info(
            "Removing the streaming collector %s left by the previous daemon", name
        )
        try:
            await docker.rm_service(name)
        except Exception:
            log.exception("Could not remove %s", name)
    await docker.prune_logging_configs()
