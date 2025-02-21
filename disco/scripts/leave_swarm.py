"""Script to leave Docker Swarm

It waits until it's the last container running,
then leaves the swarm.

Once the script is started, the swarm manager
will change the availability of the node to "drain",
so all services will be moved to other nodes.

"""

import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    wait_is_last_container()
    leave_swarm()


def ps_count() -> int:
    args = ["docker", "ps", "--format", "json"]
    process = subprocess.run(args, check=True, capture_output=True)
    return len(process.stdout.split(b"\n")) - 1


def wait_is_last_container():
    log.info("Waiting for all containers to be stopped")
    timeout = datetime.now(timezone.utc) + timedelta(days=1)
    while True:
        if datetime.now(timezone.utc) > timeout:
            log.error("Timed out")
            return
        count = ps_count()
        if count == 1:
            log.info("Done waiting")
            return
        log.info("There are %d other containers running, waiting...", count - 1)
        time.sleep(1)


def leave_swarm() -> None:
    log.info("Leaving swarm")
    args = ["docker", "swarm", "leave"]
    subprocess.run(args)
