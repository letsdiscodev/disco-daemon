"""dqlite cluster management utilities."""

import functools
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from disco.utils.subprocess import decode_text

log = logging.getLogger(__name__)

DQLITE_IMAGE_TAG = "0.1.0-patched-null"


@functools.cache
def get_current_node_disco_name() -> str:
    """Get the disco-name of the current node (cached)."""
    result = subprocess.run(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            '{{ index .Spec.Labels "disco-name" }}',
            "self",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@functools.cache
def get_local_dqlite_address() -> str:
    """Get the dqlite service address for this node (cached)."""
    disco_name = get_current_node_disco_name()
    return f"dqlite-{disco_name}:9001"


def disco_name_to_node_id(disco_name: str) -> int:
    """Convert disco-name to a positive 64-bit integer for dqlite NODE_ID."""
    return abs(hash(disco_name)) & 0x7FFFFFFFFFFFFFFF


def wait_for_dqlite_service(service_name: str, timeout_seconds: int = 120) -> None:
    """Wait for a dqlite service to become healthy."""
    log.info("Waiting for dqlite service %s to become healthy", service_name)
    timeout = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

    while datetime.now(timezone.utc) < timeout:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "service",
                    "ps",
                    service_name,
                    "--filter",
                    "desired-state=running",
                    "--format",
                    "{{ .CurrentState }}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            states = result.stdout.strip().split("\n")
            # Check if any task is running (healthy state will show "Running")
            if any("Running" in state for state in states if state):
                log.info("dqlite service %s is running", service_name)
                return
        except subprocess.CalledProcessError:
            pass

        import time

        time.sleep(2)

    raise Exception(
        f"Timeout waiting for dqlite service {service_name} to become healthy"
    )


def _run_cmd(args: list[str], timeout: int = 600) -> str:
    """Run a command and return output."""
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_dt = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    output = ""
    for line in process.stdout:
        decoded_line = decode_text(line)
        output += decoded_line
        print(decoded_line, end="", flush=True)
        if datetime.now(timezone.utc) > timeout_dt:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    print("", flush=True)
    return output


def start_first_dqlite_service(disco_name: str) -> None:
    """Start the bootstrap dqlite service on the first node."""
    service_name = f"dqlite-{disco_name}"
    node_id = disco_name_to_node_id(disco_name)

    log.info(
        "Starting bootstrap dqlite service %s with NODE_ID %d", service_name, node_id
    )
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            service_name,
            "--network",
            "disco-dqlite",
            "--network",
            "disco-main",
            "--constraint",
            f"node.labels.disco-name=={disco_name}",
            "--mount",
            f"source=dqlite-{disco_name},target=/data",
            "--env",
            f"NODE_ID={node_id}",
            "--env",
            "PORT=9001",
            "--env",
            "BOOTSTRAP=true",
            "--health-cmd",
            "/app/healthcheck.sh",
            "--health-interval",
            "5s",
            "--health-start-period",
            "10s",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            f"letsdiscodev/dqlite:{DQLITE_IMAGE_TAG}",
        ]
    )
