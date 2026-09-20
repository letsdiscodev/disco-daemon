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

# Lines waiting for the SSE writer. When full, the server stops reading and
# the collector buffers on disk.
STREAM_QUEUE_MAX = 500
# Docker records are at most 16 KB but Vector merges partial records
STREAM_LINE_LIMIT = 256 * 1024


LogObject = dict[str, str | dict[str, str]]


def parse_stream_line(line: bytes) -> LogObject | None:
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
# <rfc 3339 ns timestamp> <task name>@<node>    | <message>
_SERVICE_LOG_LINE = re.compile(
    r"^(?P<ts>\S+) (?P<task>\S+)@(?P<node>\S+)\s+\| ?(?P<msg>.*)$"
)


def parse_service_log_line(line: str, labels: dict[str, str]) -> LogObject | None:
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
    return (
        str(log_obj["container"]),
        str(log_obj.get("ts", log_obj["timestamp"])),
        str(log_obj["message"]),
    )


def for_client(log_obj: LogObject) -> LogObject:
    # "ts" (milliseconds) is only used to order and dedupe history against live
    return {k: v for k, v in log_obj.items() if k != "ts"}


class LogStreamServer:
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
            pass
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            log.info("Log collector from %s disconnected", peer)

    async def wait_for_connections(self, count: int, timeout: float) -> bool:
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
        if self.server is None:
            return
        self.server.close()
        # wait_closed() waits for the connections, and the collectors keep
        # theirs until they are removed
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


async def release_syslog(service_name: str) -> None:
    global _active_syslogs
    async with syslog_list_lock:
        _active_syslogs = [
            sl for sl in _active_syslogs if sl.service_name != service_name
        ]


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


async def start_log_collector(service_name: str, config: str) -> None:
    args = [
        "docker",
        "service",
        "create",
        "--name",
        service_name,
        "--detach",
        "--mode",
        "global",
        "--label",
        "disco.syslogs",
        "--label",
        f"disco.syslog.image={vectorconfig.VECTOR_IMAGE}",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--network",
        "disco-logging",
        "--env",
        f"{vectorconfig.CONFIG_ENV}={config}",
        "--env",
        vectorconfig.VECTOR_LOG_ENV,
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=20m",
        "--log-opt",
        "max-file=5",
        "--entrypoint",
        "sh",
        vectorconfig.VECTOR_IMAGE,
        "-c",
        vectorconfig.VECTOR_COMMAND,
    ]
    await check_call(args)


async def remove_log_collector(service_name: str) -> None:
    # A "docker service create" cancelled mid-way can still commit after this
    for _ in range(10):
        if await docker.service_exists(service_name):
            await docker.rm_service(service_name)
            break
        await asyncio.sleep(3)


async def _skip_line(reader: asyncio.StreamReader) -> None:
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk or b"\n" in chunk:
            return


async def remove_all_log_collectors() -> None:
    # At boot, no client is attached: every streaming collector is an orphan
    for name in await get_running_syslogs():
        log.info("Removing the streaming collector %s", name)
        try:
            await docker.rm_service(name)
        except Exception:
            log.exception("Could not remove %s", name)
