import asyncio
import json
import logging
import random
import re

from asgiref.wsgi import WsgiToAsgi
from pyramid.config import Configurator
from pyramid.paster import get_appsettings, setup_logging

log = logging.getLogger(__name__)


class ExtendedWsgiToAsgi(WsgiToAsgi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.protocol_router = {"http": {}, "websocket": {}}

    async def __call__(self, scope, *args, **kwargs):
        if scope["type"] == "websocket":
            handler = self.get_ws_handler(scope)
            if handler is not None:
                await handler(scope, *args, **kwargs)
                return
        elif scope["type"] == "http":
            try:
                await super().__call__(scope, *args, **kwargs)
            except ValueError:
                log.exception("Exception handling request %s", scope)
            except Exception as e:
                raise e

    def get_ws_handler(self, scope):
        if re.match(r"^/logs(/([^/]+))?(/([^/]+))?$", scope["path"]):
            return logs_websocket


async def logs_websocket(scope, receive, send):
    m = re.match(r"^/logs(/([^/]+))?(/([^/]+))?$", scope["path"])
    assert m is not None
    project_name = m.group(2)
    service_name = m.group(4)
    port = random.randint(10000, 65535)
    logspout_cmd = LOGSPOUT_CMD.copy()
    assert logspout_cmd[4] == "{name}"
    syslog_service = f"disco-syslog-{port}"
    logspout_cmd[4] = syslog_service
    logspout_cmd[-1] = logspout_cmd[-1].format(port=port)
    transport = None
    log_queue = asyncio.Queue()
    start_logspout_process = await asyncio.create_subprocess_exec(*logspout_cmd)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: JsonLogServer(
            log_queue=log_queue, project_name=project_name, service_name=service_name
        ),
        local_addr=("0.0.0.0", port),
    )
    receive_websocket_task = asyncio.create_task(receive())
    receive_logs_task = asyncio.create_task(log_queue.get())
    tasks = [receive_websocket_task, receive_logs_task]
    websocket_connected = False
    while True:
        done_tasks, pending_tasks = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        done_task = done_tasks.pop()  # only one task because FIRST_COMPLETED
        if done_task == receive_logs_task:
            if websocket_connected:
                log_obj = receive_logs_task.result()
                await send({"type": "websocket.send", "text": json.dumps(log_obj)})
            receive_logs_task = asyncio.create_task(log_queue.get())
            pending_tasks.add(receive_logs_task)
        elif done_task == receive_websocket_task:
            message = receive_websocket_task.result()
            receive_websocket_task = asyncio.create_task(receive())
            pending_tasks.add(receive_websocket_task)
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.accept"})
                websocket_connected = True
            elif message["type"] == "websocket.receive":
                pass  # no op, shouldn't happen
            elif message["type"] == "websocket.disconnect":
                websocket_connected = False
                try:
                    await start_logspout_process.wait()
                    rm_logspout_process = await asyncio.create_subprocess_exec(
                        "docker", "service", "rm", syslog_service
                    )
                    await rm_logspout_process.wait()
                except Exception:
                    log.exception("Exception terminating logspout")
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        log.exception("Exception closing transport")
                break
        tasks = pending_tasks


LOGSPOUT_CMD = [
    "docker",
    "service",
    "create",
    "--name",
    "{name}",
    "--mode",
    "global",
    "--env",
    "BACKLOG=false",
    "--env",
    'RAW_FORMAT={ "container" : "{{`{{ .Container.Name }}`}}", '
    '"labels": {{`{{ toJSON .Container.Config.Labels }}`}}, '
    '"timestamp": "{{`{{ .Time.Format "2006-01-02T15:04:05Z07:00" }}`}}", '
    '"message": {{`{{ toJSON .Data }}`}} }',
    "--mount",
    "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
    "--network",
    "disco-network",
    "--env",
    "ALLOW_TTY=true",
    "gliderlabs/logspout",
    "raw://disco-daemon:{port}",
]


class JsonLogServer:
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

    # TODO def error?


def main(global_config, **settings):
    with Configurator(settings=settings) as config:
        config.include("disco.models")
        config.include("disco.http.auth")
        config.scan("disco.http.endpoints")
    wsgi_app = config.make_wsgi_app()

    asgi_app = ExtendedWsgiToAsgi(wsgi_app)
    return asgi_app


def create_app():
    config_uri = "/code/production.ini"
    setup_logging(config_uri)
    settings = get_appsettings(config_uri)
    asgi_app = main(dict(), **settings)
    return asgi_app
