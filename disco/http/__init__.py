import random
import asyncio
import json

from asgiref.wsgi import WsgiToAsgi
from pyramid.config import Configurator
from pyramid.paster import get_appsettings, setup_logging

import logging

log = logging.getLogger(__name__)

class ExtendedWsgiToAsgi(WsgiToAsgi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.protocol_router = {"http": {}, "websocket": {}}

    async def __call__(self, scope, *args, **kwargs):
        protocol = scope["type"]
        path = scope["path"]
        try:
            consumer = self.protocol_router[protocol][path]
        except KeyError:
            consumer = None
        if consumer is not None:
            await consumer(scope, *args, **kwargs)
        else:
            try:
                await super().__call__(scope, *args, **kwargs)
            except ValueError:
                log.exception("Exception handling request %s", scope)
            except Exception as e:
                raise e

    def route(self, rule, *args, **kwargs):
        try:
            protocol = kwargs["protocol"]
        except KeyError:
            raise Exception("You must define a protocol type for an ASGI handler")

        def _route(func):
            self.protocol_router[protocol][rule] = func

        return _route


LOGSPOUT_CMD = [
        "docker",
        "run",
        "--env",
        "BACKLOG=false",
        "--env",
        'RAW_FORMAT={ "container" : "{{ .Container.Name }}", '
        '"labels": {{ toJSON .Container.Config.Labels }}, '
        '"timestamp": "{{ .Time.Format "2006-01-02T15:04:05Z07:00" }}", '
        '"message": {{ toJSON .Data }} }',
        "--env",
        "EXCLUDE_LABEL=disco",
        "--volume",
        "/var/run/docker.sock:/var/run/docker.sock",
        "--network",
        "disco-network",
        "--env",
        "ALLOW_TTY=true",
        "gliderlabs/logspout",
        "raw://disco-daemon:{port}",
    ]


class JsonLogServer:
    def __init__(self, log_queue):
        self.log_queue = log_queue

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

    @asgi_app.route("/logs", protocol="websocket")
    async def logs_websocket(scope, receive, send):
        port = random.randint(10000, 65535)
        logspout_cmd = LOGSPOUT_CMD.copy()
        logspout_cmd[-1] = logspout_cmd[-1].format(port=port)
        logspout_process = None
        transport = None
        log_queue = asyncio.Queue()
        logspout_process = await asyncio.create_subprocess_exec(*logspout_cmd)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: JsonLogServer(log_queue), local_addr=("0.0.0.0", port)
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
                    if logspout_process is not None:
                        try:
                            logspout_process.terminate()
                            await logspout_process.wait()
                        except Exception:
                            log.exception("Exception terminating logspout")
                    if transport is not None:
                        try:
                            transport.close()
                        except Exception:
                            log.exception("Exception closing transport")
                    break
            tasks = pending_tasks

    return asgi_app


def create_app():
    config_uri = "/code/production.ini"
    setup_logging(config_uri)
    settings = get_appsettings(config_uri)
    asgi_app = main(dict(), **settings)
    return asgi_app
