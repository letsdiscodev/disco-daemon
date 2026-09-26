import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import TypedDict

from disco.utils import docker, vectorconfig
from disco.utils.subprocess import check_call

log = logging.getLogger(__name__)

LOGS_PORT = 10514
COLLECTOR_NAME = "disco-logs"
COLLECTOR_CONNECT_TIMEOUT_SECONDS = 15
STREAM_LINE_LIMIT = 64 * 1024


class LogLine(TypedDict):
    container: str
    labels: dict[str, str]
    timestamp: str  # docker's, rfc 3339 with nanoseconds
    stream: str  # "stdout", "stderr" or "console" (tty)
    message: str


def log_matches(
    log_line: LogLine, project_name: str | None, service_name: str | None
) -> bool:
    labels = log_line["labels"]
    if project_name is not None and labels.get("disco.project.name") != project_name:
        return False
    if service_name is not None and labels.get("disco.service.name") != service_name:
        return False
    return True


class LogSession:
    def __init__(self, project_name: str | None, service_name: str | None) -> None:
        self.project_name = project_name
        self.service_name = service_name
        self.queue: asyncio.Queue[LogLine] = asyncio.Queue(maxsize=5000)
        self.dropped_lines = 0
        self._notice_at = 0.0

    def offer(self, log_line: LogLine) -> None:
        if not log_matches(log_line, self.project_name, self.service_name):
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped_lines += 1
        self.queue.put_nowait(log_line)

    async def get(self) -> LogLine:
        now = time.monotonic()
        if self.dropped_lines > 0 and now - self._notice_at >= 1:
            dropped_lines = self.dropped_lines
            self.dropped_lines = 0
            self._notice_at = now
            now_utc = datetime.now(timezone.utc)
            return {
                "container": "disco",
                "labels": {},
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
                "stream": "stdout",
                "message": f"[disco] {dropped_lines} lines dropped",
            }
        return await self.queue.get()


class LogListener:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.sessions: set[LogSession] = set()
        self._collector_connections: dict[asyncio.StreamWriter, str] = {}
        self._changed = asyncio.Condition()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "0.0.0.0", LOGS_PORT, limit=STREAM_LINE_LIMIT
        )

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
        for writer in list(self._collector_connections):
            writer.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        address = peer[0] if peer else "?"
        log.info("Log collector connected from %s", address)
        self._collector_connections[writer] = address
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
                log_line = parse_stream_line(line.rstrip(b"\n"))
                if log_line is None:
                    continue
                for session in self.sessions:
                    session.offer(log_line)
        except ConnectionResetError:
            pass
        finally:
            del self._collector_connections[writer]
            writer.close()
            log.info("Log collector from %s disconnected", address)

    def connected_nodes(self) -> int:
        return len(set(self._collector_connections.values()))

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


def parse_stream_line(line: bytes) -> LogLine | None:
    try:
        json_str = line.decode("utf-8")
    except UnicodeDecodeError:
        log.error("Failed to UTF-8 decode log line: %r", line[:200])
        return None
    try:
        parsed = json.loads(json_str)
    except json.decoder.JSONDecodeError:
        log.error("Failed to JSON decode log line: %s", json_str[:200])
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("labels"), dict):
        log.error("Unexpected log line shape: %s", json_str[:200])
        return None
    return LogLine(
        container=str(parsed.get("container", "")),
        labels=parsed["labels"],
        timestamp=str(parsed.get("timestamp", "")),
        stream=str(parsed.get("stream", "")),
        message=str(parsed.get("message", "")),
    )


async def ensure_log_collector() -> None:
    config = vectorconfig.render_streaming_config(LOGS_PORT)
    config_hash = vectorconfig.config_hash(config)
    async with _collector_lock:
        if await docker.service_exists(COLLECTOR_NAME):
            labels = await docker.get_service_labels(COLLECTOR_NAME)
            if labels.get("disco.logs.config") == config_hash:
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
            "disco.logs",
            "--label",
            f"disco.logs.config={config_hash}",
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
        if time.monotonic() - _idle_since < 3600:
            return
        if await docker.service_exists(COLLECTOR_NAME):
            log.info("Removing the disco logs collector, no session for an hour")
            await docker.rm_service(COLLECTOR_NAME)
