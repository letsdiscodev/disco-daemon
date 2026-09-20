"""Vector configs for the log collectors (syslog destinations and `disco logs`)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

VECTOR_IMAGE = "timberio/vector:0.58.0-alpine"
VECTOR_CONFIG_PATH = "/etc/vector/vector.yaml"
HOSTNAME_ENV = "SYSLOG_HOSTNAME"
# Bump when spec changes to update services
SERVICE_SPEC_REVISION = 4

VECTOR_LOG_ENV = "VECTOR_LOG=warn"

SyslogType = Literal["CORE", "GLOBAL"]

_SYSLOG_URL_RE = re.compile(
    r"^syslog(?P<tls>\+tls)?://"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?)"
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
    return hashlib.sha256(f"{type} {url}".encode()).hexdigest()[:12]


def config_hash(config: str) -> str:
    return hashlib.sha256(config.encode()).hexdigest()[:12]


# The dedupe transform is for docker_logs delivering the last record of an
# exiting container twice (Vector 0.58.0).
_DOCKER_SOURCE = f"""\
# disco collector, service spec revision {SERVICE_SPEC_REVISION}, image {VECTOR_IMAGE}
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

# Same as logspout: labels matched by value "true", case-insensitively.
_EXCLUDE_CONDITION = 'downcase(to_string(.label."disco.log.exclude") ?? "") != "true"'
_CORE_CONDITION = 'downcase(to_string(.label."disco.log.core") ?? "") == "true"'

# RFC 5424: <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG
# PRI = facility user (1) * 8 + severity, stderr -> err (3), stdout -> info (6)
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

_STREAM_VRL = """\
      . = {
        "container": to_string(.container_name) ?? "",
        "labels": object(.label) ?? {},
        "timestamp": format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%SZ") ?? "",
        "ts": format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%S%.3fZ") ?? "",
        "message": to_string(.message) ?? ""
      }
"""


def _vrl_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_syslog_config(url: str, type: SyslogType, disco_host: str = "") -> str:
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
    address: "{dest.address}"
    tls:
      enabled: true
      verify_certificate: true
      verify_hostname: true
    framing:
      method: newline_delimited
    buffer:
      when_full: block
"""
    else:
        transport = f"""\
    mode: udp
    address: "{dest.address}"
    framing:
      method: bytes
    buffer:
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
    return _DOCKER_SOURCE + body


def render_streaming_config(port: int) -> str:
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
    address: "disco:{port}"
    framing:
      method: newline_delimited
    buffer:
      when_full: block
    encoding:
      codec: json
"""
    return _DOCKER_SOURCE + body
