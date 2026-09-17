"""pure builders in docker.py / logs.py and the reconciler with docker stubbed out."""

import asyncio

import pytest

from disco.utils import vectorconfig as vc


def _docker():
    from disco.utils import docker

    return docker


def test_syslog_service_name_is_deterministic_and_config_sensitive():
    docker = _docker()
    cfg_a = vc.render_syslog_config("syslog://h:1", "GLOBAL")
    cfg_b = vc.render_syslog_config("syslog://h:1", "GLOBAL", buffer_bytes=2**30)
    a1 = docker.syslog_service_name("syslog://h:1", "GLOBAL", cfg_a)
    a2 = docker.syslog_service_name("syslog://h:1", "GLOBAL", cfg_a)
    b = docker.syslog_service_name("syslog://h:1", "GLOBAL", cfg_b)
    c = docker.syslog_service_name(
        "syslog://h:1", "CORE", vc.render_syslog_config("syslog://h:1", "CORE")
    )
    assert a1 == a2
    assert a1 != b  # a buffer size change is a config change: new service
    assert a1 != c  # same url as CORE and GLOBAL: two services
    assert a1.startswith("disco-syslog-")


def test_build_syslog_service_args():
    docker = _docker()
    cfg = vc.render_syslog_config(
        "syslog+tls://logs4.papertrailapp.com:27257", "GLOBAL"
    )
    args = docker.build_syslog_service_args(
        "my.host", "syslog+tls://logs4.papertrailapp.com:27257", "GLOBAL", cfg
    )
    joined = " ".join(args)
    assert args[:3] == ["docker", "service", "create"]
    assert "--label disco.syslog " in joined
    assert (
        "--label disco.syslog.url=syslog+tls://logs4.papertrailapp.com:27257" in joined
    )
    assert "--label disco.syslog.type=GLOBAL" in joined
    assert "--label disco.syslog.impl=vector" in joined
    assert f"--label disco.syslog.image={vc.VECTOR_IMAGE}" in joined
    cfg_name = docker.syslog_config_name(cfg)
    assert f"--label disco.syslog.config={cfg_name}" in joined
    assert f"--config source={cfg_name},target={vc.VECTOR_CONFIG_PATH}" in joined
    assert "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock" in joined
    assert (
        f"type=volume,source={docker.syslog_buffer_volume_name('syslog+tls://logs4.papertrailapp.com:27257', 'GLOBAL')},target={vc.VECTOR_DATA_DIR}"
        in joined
    )
    assert f"--env {vc.HOSTNAME_ENV}=my.host" in joined
    assert "--mode global" in joined
    assert "--update-order start-first" in joined
    assert "--limit-memory" in joined
    assert args[-3:] == [vc.VECTOR_IMAGE, "--config", vc.VECTOR_CONFIG_PATH]
    # no logspout leftovers
    assert "logspout" not in joined and "EXCLUDE_LABELS" not in joined


def test_build_streaming_service_args():
    from disco.utils import logs

    args = logs.build_streaming_service_args(
        "disco-syslog-12345", "disco-stream-cfg-abc"
    )
    joined = " ".join(args)
    assert "--name disco-syslog-12345" in joined
    assert "--label disco.syslogs " in joined
    assert "--label disco.syslog.config=disco-stream-cfg-abc" in joined
    assert "--network disco-logging" in joined
    assert "--mode global" in joined
    assert "type=volume" not in joined  # stream buffers die with the task
    assert args[-3:] == [vc.VECTOR_IMAGE, "--config", vc.VECTOR_CONFIG_PATH]


def test_parse_stream_line_and_match():
    from disco.utils import logs

    good = b'{"container":"p-1-web.1","labels":{"disco.project.name":"p","disco.service.name":"web"},"timestamp":"2026-09-17T15:00:00.123Z","message":"hi"}'
    obj = logs.parse_stream_line(good)
    assert obj is not None and obj["message"] == "hi"
    assert logs.log_matches(obj, None, None)
    assert logs.log_matches(obj, "p", None)
    assert logs.log_matches(obj, "p", "web")
    assert not logs.log_matches(obj, "other", None)
    assert not logs.log_matches(obj, "p", "worker")
    assert not logs.log_matches(obj, None, "worker")
    assert logs.parse_stream_line(b"not json") is None
    assert logs.parse_stream_line(b"\xff\xfe") is None
    assert (
        logs.parse_stream_line(b'{"container":"x","labels":"nope","message":""}')
        is None
    )
    assert logs.parse_stream_line(b"[1,2]") is None


class FakeDocker:
    """records calls; services are SyslogService-like objects."""

    def __init__(self, existing):
        from disco.utils.docker import SyslogService

        self.SyslogService = SyslogService
        self.existing = list(existing)
        self.started: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.order: list[str] = []
        self.pruned = 0

    async def list_syslog_services(self):
        return list(self.existing)

    async def start_syslog_service(self, disco_host, url, type, buffer_bytes):
        from disco.utils import docker

        cfg = vc.render_syslog_config(url, type, buffer_bytes, disco_host)
        name = docker.syslog_service_name(url, type, cfg)
        self.started.append((url, type))
        self.order.append(f"start {name}")
        return name

    async def rm_syslog_service(self, service):
        self.removed.append(service.name)
        self.order.append(f"rm {service.name}")

    async def prune_logging_configs(self):
        self.pruned += 1


@pytest.fixture
def fake(monkeypatch):
    from disco.utils import syslog

    def install(existing):
        fd = FakeDocker(existing)
        for attr in (
            "list_syslog_services",
            "start_syslog_service",
            "rm_syslog_service",
            "prune_logging_configs",
        ):
            monkeypatch.setattr(syslog.docker, attr, getattr(fd, attr))
        return fd

    return install


def _vector_service(
    url, type, buffer_bytes=vc.DEFAULT_DESTINATION_BUFFER_BYTES, host="host"
):
    from disco.utils import docker

    cfg = vc.render_syslog_config(url, type, buffer_bytes, host)
    return docker.SyslogService(
        name=docker.syslog_service_name(url, type, cfg),
        type=type,
        url=url,
        impl="vector",
        image=vc.VECTOR_IMAGE,
        config=docker.syslog_config_name(cfg),
    )


def _logspout_service(url, type):
    from disco.utils import docker

    return docker.SyslogService(
        name="disco-syslog-0123456789abcdef", type=type, url=url
    )


def test_reconcile_creates_missing_and_is_idempotent(fake):
    from disco.utils.syslog import set_syslog_services

    fd = fake([])
    urls = [
        {"url": "syslog://h:1", "type": "GLOBAL"},
        {"url": "syslog+tls://h:2", "type": "CORE"},
    ]
    asyncio.run(set_syslog_services("host", urls))
    assert sorted(fd.started) == [
        ("syslog+tls://h:2", "CORE"),
        ("syslog://h:1", "GLOBAL"),
    ]
    assert fd.removed == []
    # second run with the services now existing: nothing happens
    fd2 = fake(
        [
            _vector_service("syslog://h:1", "GLOBAL"),
            _vector_service("syslog+tls://h:2", "CORE"),
        ]
    )
    asyncio.run(set_syslog_services("host", urls))
    assert fd2.started == [] and fd2.removed == []
    assert fd2.pruned == 1


def test_reconcile_replaces_logspout_create_before_remove(fake):
    from disco.utils.syslog import set_syslog_services

    old = _logspout_service("syslog://h:1", "GLOBAL")
    fd = fake([old])
    asyncio.run(
        set_syslog_services("host", [{"url": "syslog://h:1", "type": "GLOBAL"}])
    )
    assert fd.started == [("syslog://h:1", "GLOBAL")]
    assert fd.removed == [old.name]
    assert fd.order[0].startswith("start ") and fd.order[1].startswith("rm ")


def test_reconcile_removes_extra_and_stale_config(fake):
    from disco.utils.syslog import set_syslog_services

    keep = _vector_service("syslog://h:1", "GLOBAL")
    extra = _vector_service("syslog://gone:9", "GLOBAL")
    stale = _vector_service(
        "syslog://h:3", "GLOBAL", buffer_bytes=2**30
    )  # rendered with another size
    fd = fake([keep, extra, stale])
    asyncio.run(
        set_syslog_services(
            "host",
            [
                {"url": "syslog://h:1", "type": "GLOBAL"},
                {"url": "syslog://h:3", "type": "GLOBAL"},
            ],
        )
    )
    assert fd.started == [("syslog://h:3", "GLOBAL")]
    assert sorted(fd.removed) == sorted([extra.name, stale.name])


def test_reconcile_same_url_core_and_global_are_two_services(fake):
    from disco.utils.syslog import set_syslog_services

    fd = fake([])
    asyncio.run(
        set_syslog_services(
            "host",
            [
                {"url": "syslog://h:1", "type": "GLOBAL"},
                {"url": "syslog://h:1", "type": "CORE"},
            ],
        )
    )
    assert sorted(fd.started) == [("syslog://h:1", "CORE"), ("syslog://h:1", "GLOBAL")]


def test_reconcile_replaces_on_hostname_change(fake):
    from disco.utils.syslog import set_syslog_services

    old = _vector_service("syslog://h:1", "GLOBAL", host="old.host")
    fd = fake([old])
    asyncio.run(
        set_syslog_services("new.host", [{"url": "syslog://h:1", "type": "GLOBAL"}])
    )
    assert fd.started == [("syslog://h:1", "GLOBAL")] and fd.removed == [old.name]
    assert fd.order[0].startswith("start ")


def test_reconcile_with_zero_destinations_removes_everything(fake):
    from disco.utils.syslog import set_syslog_services

    fd = fake(
        [
            _logspout_service("syslog://h:1", "GLOBAL"),
            _vector_service("syslog://h:2", "CORE"),
        ]
    )
    asyncio.run(set_syslog_services("host", []))
    assert fd.started == [] and len(fd.removed) == 2


def test_parse_service_log_line():
    from disco.utils import logs

    labels = {"disco.project.name": "p", "disco.service.name": "web"}
    line = "2026-09-17T15:25:41.123456789Z p-3-web.1.k2j3h4g5f6d7s8a9@node-a    | hello world"
    obj = logs.parse_service_log_line(line, labels)
    assert obj == {
        "container": "p-3-web.1.k2j3h4g5f6d7s8a9",
        "labels": labels,
        "timestamp": "2026-09-17T15:25:41.123Z",
        "message": "hello world",
    }
    # an empty message line
    obj = logs.parse_service_log_line(
        "2026-09-17T15:25:41.000000000Z p-3-web.1.abc@n | ", labels
    )
    assert obj is not None and obj["message"] == ""
    assert logs.parse_service_log_line("garbage", labels) is None
    assert logs.history_key(obj) == ("p-3-web.1.abc", "2026-09-17T15:25:41.000Z", "")
