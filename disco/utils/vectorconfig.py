"""Vector configs for the syslog destination collectors."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

VECTOR_IMAGE = "timberio/vector:0.58.0-alpine"
CONFIG_ENV = "DISCO_VECTOR_CONFIG"
VECTOR_COMMAND = (
    f'printf "%s\\n" "${CONFIG_ENV}" > /tmp/vector.yaml'
    " && exec vector --config /tmp/vector.yaml"
)
SERVICE_SPEC_REVISION = 4  # Bump when spec changes to update services
VECTOR_LOG_ENV = "VECTOR_LOG=warn"

SyslogType = Literal["CORE", "GLOBAL"]


def destination_id(url: str, type: str) -> str:
    return hashlib.sha256(f"{type} {url}".encode()).hexdigest()[:12]


def config_hash(config: str) -> str:
    return hashlib.sha256(config.encode()).hexdigest()[:12]


# The dedupe transform is for docker_logs delivering the last record of an
# exiting container twice.
# Keep comment "# disco collector..." because it's used to generate the config hash.
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

_EXCLUDE_CONDITION = 'downcase(to_string(.label."disco.log.exclude") ?? "") != "true"'
_CORE_CONDITION = 'downcase(to_string(.label."disco.log.core") ?? "") == "true"'

# RFC 5424: <PRI>1 TIMESTAMP HOSTNAME APP-NAME PROCID MSGID SD MSG
# PRI = facility user (1) * 8 + severity, stderr -> err (3), stdout -> info (6)
_SYSLOG_VRL = """\
      hostname = "{hostname}"
      if hostname == "" { hostname = "-" }
      severity = if .stream == "stderr" { 3 } else { 6 }
      pri = 8 + severity
      ts = format_timestamp(.timestamp, "%Y-%m-%dT%H:%M:%SZ") ?? "-"
      app = to_string(.container_name) ?? "-"
      app = truncate(app, 48)
      if app == "" { app = "-" }
      msg = to_string(.message) ?? ""
      msg = replace(msg, "\\n", "\\\\n")
      . = { "message": "<" + to_string(pri) + ">1 " + ts + " " + hostname + " " + app + " - - - " + msg }
"""


def _vrl_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_syslog_config(url: str, type: SyslogType, disco_host: str = "") -> str:
    tls = url.startswith("syslog+tls://")
    address = json.dumps(url.split("://", 1)[1])
    vrl = _SYSLOG_VRL.replace("{hostname}", _vrl_string(disco_host))
    if type == "CORE":
        condition = f"{_CORE_CONDITION} && {_EXCLUDE_CONDITION}"
    elif type == "GLOBAL":
        condition = _EXCLUDE_CONDITION
    else:
        raise ValueError(f"unknown syslog type {type!r}")
    if tls:
        transport = f"""\
    mode: tcp
    address: {address}
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
    address: {address}
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
