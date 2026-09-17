"""vector configuration for log forwarding: pure functions, no docker, no db.

two kinds of collector run as swarm global services (one task per node), both reading
the local docker socket with vector's `docker_logs` source:

- syslog destinations (`render_syslog_config`): one service per configured url, rfc 5424
  frames over udp (`syslog://`) or tcp+tls (`syslog+tls://`, lf framed).
- `disco logs` streaming (`render_streaming_config`): one service per connected client,
  json lines over tcp to the daemon on the `disco-logging` overlay.

the hostname is part of the rendered config: a hostname change renders a new config,
whose hash is a new service name, so the reconciler creates the new collector before
removing the old one (an overlap, no gap: a global service cannot be restarted without
a gap, its node runs one task at a time and the docker source only reads from its own
start). SYSLOG_HOSTNAME in the environment is the fallback when the config has none.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

# pinned; multi-arch (linux/amd64, linux/arm64, arm/v7, arm/v6). the digest is the
# manifest list, checked on docker hub on 2026-09-17. bump both together.
VECTOR_IMAGE = "timberio/vector:0.58.0-alpine"
VECTOR_IMAGE_DIGEST = (
    "sha256:5dcf67db0ee378caa87f3395cb9484ebe3e97bb0334d119f2ac33116e00c5773"
)
VECTOR_CONFIG_PATH = "/etc/vector/vector.yaml"
VECTOR_DATA_DIR = "/var/lib/vector"
HOSTNAME_ENV = "SYSLOG_HOSTNAME"

# disk buffer sizes (bytes). keyvalues LOGGING_DESTINATION_BUFFER_BYTES and
# LOGGING_STREAM_BUFFER_BYTES override them (no cli option, see docs/logging).
DEFAULT_DESTINATION_BUFFER_BYTES = 512 * 1024 * 1024
DEFAULT_STREAM_BUFFER_BYTES = 100 * 1024 * 1024
# vector refuses disk buffers under this size
MIN_DISK_BUFFER_BYTES = 268_435_488

SyslogType = Literal["CORE", "GLOBAL"]

_SYSLOG_URL_RE = re.compile(
    r"^syslog(?P<tls>\+tls)?://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r":(?P<port>\d{1,5})\Z"
)


class InvalidSyslogUrl(ValueError):
    pass


@dataclass(frozen=True)
class SyslogDestination:
    host: str
    port: int
    tls: bool

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


def parse_syslog_url(url: str) -> SyslogDestination:
    """`syslog://host:port` (udp) or `syslog+tls://host:port` (tcp + tls), nothing else.

    no userinfo, path, query string or fragment: a credential does not belong in a
    url that ends up in labels and logs (see the prd, F9).
    """
    m = _SYSLOG_URL_RE.match(url)
    if m is None:
        raise InvalidSyslogUrl(
            f"invalid syslog url {url!r}: expected syslog://host:port "
            "or syslog+tls://host:port"
        )
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        raise InvalidSyslogUrl(f"invalid syslog url {url!r}: port out of range")
    return SyslogDestination(
        host=m.group("host"), port=port, tls=m.group("tls") is not None
    )


def destination_id(url: str, type: str) -> str:
    """stable short id for a destination, used in service, config and volume names."""
    return hashlib.sha256(f"{type} {url}".encode()).hexdigest()[:12]


def config_hash(config: str) -> str:
    return hashlib.sha256(config.encode()).hexdigest()[:12]


# vector's docker_logs source delivers the last record of a container that just
# exited twice (measured on 0.58.0: local proof and droplet). the same container, the
# same nanosecond timestamp and the same message is never a real second line.
# bump when the swarm service spec built around the config changes (mounts, update
# order, limits): the revision is part of the rendered config, so the config hash and
# with it the service name change and the reconciler replaces the collectors.
SERVICE_SPEC_REVISION = 3

# `{data_dir}` is filled in by the renderers: each collector gets its own directory
# inside the destination's buffer volume, so a replacement never shares buffer files
# with the collector it overlaps.
_DOCKER_SOURCE = f"""\
# disco collector, service spec revision {SERVICE_SPEC_REVISION}
data_dir: {{data_dir}}
sources:
  docker:
    type: docker_logs
    docker_host: unix:///var/run/docker.sock
transforms:
  dedupe:
    type: dedupe
    inputs: [docker]
    cache:
      num_events: 5000
    fields:
      match: [container_id, timestamp, message]
"""

# logspout matched EXCLUDE_LABELS by value "true", case-insensitively; same here.
_EXCLUDE_CONDITION = (
    'downcase(to_string(.label."disco.log.exclude") ?? "") != "true"'
    ' && downcase(to_string(.label."disco.run") ?? "") != "true"'
)
_CORE_CONDITION = 'downcase(to_string(.label."disco.log.core") ?? "") == "true"'

# `disco run` sessions carry disco.run=true and a tty: logspout never forwarded tty
# containers to destinations, so they stay out (they still show in `disco logs`).
# rfc 5424: <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG
# PRI = facility user (1) * 8 + severity: stderr -> err (3), stdout -> info (6).
# APP-NAME = container name, at most 48 chars. PROCID, MSGID, SD = "-".
# a docker record with embedded newlines stays one frame with the newlines escaped.
_SYSLOG_VRL = f"""\
      hostname = "{{hostname}}"
      if hostname == "" {{ hostname = get_env_var("{HOSTNAME_ENV}") ?? "-" }}
      if hostname == "" {{ hostname = "-" }}
      severity = if .stream == "stderr" {{ 3 }} else {{ 6 }}
      pri = 8 + severity
      ts = format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%SZ") ?? "-"
      app = to_string(.container_name) ?? "-"
      app = truncate(app, 48)
      if app == "" {{ app = "-" }}
      msg = to_string(.message) ?? ""
      msg = replace(msg, "\\n", "\\\\n")
      . = {{ "message": "<" + to_string(pri) + ">1 " + ts + " " + hostname + " " + app + " - - - " + msg }}
"""


def _vrl_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_syslog_config(
    url: str,
    type: SyslogType,
    buffer_bytes: int = DEFAULT_DESTINATION_BUFFER_BYTES,
    disco_host: str = "",
) -> str:
    """vector yaml for one syslog destination. raises InvalidSyslogUrl."""
    dest = parse_syslog_url(url)
    vrl = _SYSLOG_VRL.replace("{hostname}", _vrl_string(disco_host))
    if type == "CORE":
        condition = f"{_CORE_CONDITION} && {_EXCLUDE_CONDITION}"
    elif type == "GLOBAL":
        condition = _EXCLUDE_CONDITION
    else:
        raise ValueError(f"unknown syslog type {type!r}")
    if dest.tls:
        transport = f"""\
    mode: tcp
    address: {dest.address}
    tls:
      enabled: true
      verify_certificate: true
      verify_hostname: true
    framing:
      method: newline_delimited
    buffer:
      type: disk
      max_size: {max(buffer_bytes, MIN_DISK_BUFFER_BYTES)}
      when_full: block
"""
    else:
        transport = f"""\
    mode: udp
    address: {dest.address}
    framing:
      method: bytes
    buffer:
      type: disk
      max_size: {max(buffer_bytes, MIN_DISK_BUFFER_BYTES)}
      when_full: drop_newest
"""
    body = f"""\
  keep:
    type: filter
    inputs: [dedupe]
    condition: '{condition}'
  frame:
    type: remap
    inputs: [keep]
    source: |
{vrl}\
sinks:
  syslog:
    type: socket
    inputs: [frame]
{transport}\
    encoding:
      codec: raw_message
"""
    return _with_data_dir(_DOCKER_SOURCE + body)


def _with_data_dir(config: str) -> str:
    """the data dir is derived from the config's own hash (without it), so it is
    unique per rendered config and stable across renders."""
    marker = "{data_dir}"
    subdir = config_hash(config)
    return config.replace(marker, f"{VECTOR_DATA_DIR}/{subdir}")


_STREAM_VRL = """\
      . = {
        "container": to_string(.container_name) ?? "",
        "labels": object(.label) ?? {},
        "timestamp": format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%SZ") ?? "",
        "ts": format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%S%.3fZ") ?? "",
        "message": to_string(.message) ?? ""
      }
"""


def render_streaming_config(
    port: int, buffer_bytes: int = DEFAULT_STREAM_BUFFER_BYTES
) -> str:
    """vector yaml for one `disco logs` client: json lines over tcp to disco:<port>.

    the json shape is what disco.utils.logs.JsonLogServer reads:
    {"container", "labels", "timestamp", "message"}.
    """
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    body = f"""\
  json:
    type: remap
    inputs: [dedupe]
    source: |
{_STREAM_VRL}\
sinks:
  disco:
    type: socket
    inputs: [json]
    mode: tcp
    address: disco:{port}
    framing:
      method: newline_delimited
    buffer:
      type: disk
      max_size: {max(buffer_bytes, MIN_DISK_BUFFER_BYTES)}
      when_full: block
    encoding:
      codec: json
"""
    return _with_data_dir(_DOCKER_SOURCE + body)
