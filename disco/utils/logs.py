import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

from disco.utils import docker, vectorconfig
from disco.utils.subprocess import check_call, decode_output

log = logging.getLogger(__name__)

# One collector service on every node, sending every container's lines to the
# daemon on this port of the disco-logging network; the daemon fans them out
# to the open disco logs sessions
LOGS_PORT = 10514
COLLECTOR_NAME = "disco-logs"
# Removed by the hourly cron once no session used it for this long
COLLECTOR_IDLE_SECONDS = 3600
COLLECTOR_CONNECT_TIMEOUT_SECONDS = 15
# Lines waiting for a session's SSE writer; the oldest are dropped when full
SESSION_QUEUE_MAX = 5000
# Docker records are at most 16 KB but Vector merges partial records
STREAM_LINE_LIMIT = 256 * 1024
HISTORY_LINES = 100
HISTORY_TIMEOUT_SECONDS = 5
HISTORY_PARALLEL_READS = 8

LogObject = dict[str, str | dict[str, str]]


class LogSession:
    def __init__(self, project_name: str | None, service_name: str | None) -> None:
        self.project_name = project_name
        self.service_name = service_name
        self.queue: asyncio.Queue[LogObject] = asyncio.Queue(maxsize=SESSION_QUEUE_MAX)
        self.dropped = 0
        self._notice_at = 0.0

    def offer(self, log_obj: LogObject) -> None:
        if not log_matches(log_obj, self.project_name, self.service_name):
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(log_obj)

    async def get(self) -> LogObject:
        # the drop count as a log line, at most one a second
        now = time.monotonic()
        if self.dropped > 0 and now - self._notice_at >= 1:
            dropped, self.dropped, self._notice_at = self.dropped, 0, now
            return {
                "container": "disco",
                "labels": {},
                "timestamp": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%S.%f000Z"
                ),
                "stream": "stderr",
                "message": f"[disco] {dropped} lines dropped, the client is too slow",
            }
        return await self.queue.get()


class LogListener:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.sessions: set[LogSession] = set()
        self._peers: dict[asyncio.StreamWriter, str] = {}
        self._changed = asyncio.Condition()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "0.0.0.0", LOGS_PORT, limit=STREAM_LINE_LIMIT
        )

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        for writer in list(self._peers):
            writer.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        address = peer[0] if peer else "?"
        log.info("Log collector connected from %s", address)
        self._peers[writer] = address
        async with self._changed:
            self._changed.notify_all()
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError as e:
                    log.warning(
                        "Skipping a log record over %d bytes from %s",
                        STREAM_LINE_LIMIT,
                        address,
                    )
                    try:
                        await _skip_record(reader, e)
                    except asyncio.IncompleteReadError:
                        break
                    continue
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) == 0:
                        break
                    line = e.partial
                if len(self.sessions) == 0:
                    continue
                log_obj = parse_stream_line(line.rstrip(b"\n"))
                if log_obj is None:
                    continue
                for session in self.sessions:
                    session.offer(log_obj)
        except ConnectionResetError:
            pass
        finally:
            del self._peers[writer]
            writer.close()
            log.info("Log collector from %s disconnected", address)

    def connected_nodes(self) -> int:
        return len(set(self._peers.values()))

    async def wait_for_nodes(self, count: int, timeout: float) -> bool:
        try:
            async with self._changed:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self.connected_nodes() >= count),
                    timeout=timeout,
                )
            return True
        except TimeoutError:
            return False

    def subscribe(self, session: LogSession) -> None:
        global _idle_since
        self.sessions.add(session)
        _idle_since = None

    def unsubscribe(self, session: LogSession) -> None:
        global _idle_since
        self.sessions.discard(session)
        if len(self.sessions) == 0:
            _idle_since = time.monotonic()


log_listener = LogListener()
# no session since boot, until one subscribes
_idle_since: float | None = time.monotonic()
_collector_lock = asyncio.Lock()


async def _skip_record(reader: asyncio.StreamReader, e: asyncio.LimitOverrunError):
    # the bytes up to the newline of the record that went over the limit
    while True:
        await reader.readexactly(e.consumed)
        try:
            await reader.readuntil(b"\n")
            return
        except asyncio.LimitOverrunError as again:
            e = again


async def ensure_log_collector() -> None:
    config = vectorconfig.render_streaming_config(LOGS_PORT)
    config_hash = vectorconfig.config_hash(config)
    async with _collector_lock:
        if await docker.service_exists(COLLECTOR_NAME):
            labels = await docker.get_service_labels(COLLECTOR_NAME)
            if labels.get("disco.syslog.config") == config_hash:
                return
            log.info("Replacing the disco logs collector, its config changed")
            await docker.rm_service(COLLECTOR_NAME)
        log.info("Starting the disco logs collector")
        args = [
            "docker",
            "service",
            "create",
            "--name",
            COLLECTOR_NAME,
            "--detach",
            "--mode",
            "global",
            "--label",
            "disco.syslogs",
            "--label",
            f"disco.syslog.config={config_hash}",
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


async def remove_idle_log_collector() -> None:
    async with _collector_lock:
        if len(log_listener.sessions) > 0 or _idle_since is None:
            return
        if time.monotonic() - _idle_since < COLLECTOR_IDLE_SECONDS:
            return
        if await docker.service_exists(COLLECTOR_NAME):
            log.info("Removing the disco logs collector, no session for an hour")
            await docker.rm_service(COLLECTOR_NAME)


async def remove_all_log_collectors() -> None:
    # the 0.33 to 0.34 update: the per-session logspout collectors
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            "label=disco.syslogs",
            "--format",
            "{{ .Name }}",
        ]
    )
    for name in stdout:
        log.info("Removing the log collector %s", name)
        await docker.rm_service(name)


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


# <rfc 3339 ns timestamp> <task name>@<node>    | <message>
_SERVICE_LOG_LINE = re.compile(
    r"^(?P<ts>\S+) (?P<task>\S+)@(?P<node>\S+)\s+\| ?(?P<msg>.*)$"
)


def parse_service_log_line(
    line: str, labels: dict[str, str], stream: str
) -> LogObject | None:
    m = _SERVICE_LOG_LINE.match(line)
    if m is None:
        return None
    return {
        "container": m.group("task"),
        "labels": labels,
        "timestamp": _nanoseconds(m.group("ts")),
        "stream": stream,
        "message": m.group("msg"),
    }


def _nanoseconds(ts: str) -> str:
    # docker trims the trailing zeros of the fraction; the live lines have 9 digits
    if not ts.endswith("Z"):
        return ts
    head, _, frac = ts[:-1].partition(".")
    return f"{head}.{frac.ljust(9, '0')}Z"


async def read_service_history(
    service: docker.LabelledService, lines: int
) -> list[LogObject]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "service",
        "logs",
        "--timestamps",
        "--no-trunc",
        "--tail",
        str(lines),
        service.name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), HISTORY_TIMEOUT_SECONDS
        )
    except TimeoutError:
        # "docker service logs" sometimes hangs (see docker.get_log_for_service)
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        log.warning("Timed out reading the history of %s", service.name)
        return []
    if process.returncode != 0:
        log.warning("Could not read the history of %s", service.name)
        return []
    out = []
    for stream, output in (("stdout", stdout), ("stderr", stderr)):
        for line in decode_output(output):
            log_obj = parse_service_log_line(line, service.labels, stream)
            if log_obj is not None:
                out.append(log_obj)
    out.sort(key=lambda o: str(o["timestamp"]))
    return out[-lines:]


async def read_history(
    project_name: str | None, service_name: str | None, lines: int = HISTORY_LINES
) -> list[LogObject]:
    services = await docker.list_services_with_labels(project_name)
    if service_name is not None:
        services = [
            s for s in services if s.labels.get("disco.service.name") == service_name
        ]
    semaphore = asyncio.Semaphore(HISTORY_PARALLEL_READS)

    async def read(service: docker.LabelledService) -> list[LogObject]:
        async with semaphore:
            return await read_service_history(service, lines)

    history: list[LogObject] = []
    for objs in await asyncio.gather(*(read(service) for service in services)):
        history += objs
    history.sort(key=lambda o: str(o["timestamp"]))
    return history[-lines:]
