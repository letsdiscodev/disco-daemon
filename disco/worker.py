import logging
import os
import signal
import sys

from pyramid.paster import get_appsettings, setup_logging
from pyramid.scripts.common import parse_vars

from disco.models import (
    get_engine,
    get_session_factory,
)
from disco.utils.mq.consumer import Consumer

log = logging.getLogger(__name__)


def usage(argv):
    cmd = os.path.basename(argv[0])
    print(
        "usage: %s <config_uri> [var=value]\n"
        '(example: "%s development.ini")' % (cmd, cmd)
    )
    sys.exit(1)


def main(argv=sys.argv):
    if len(argv) < 2:
        usage(argv)
    config_uri = argv[1]
    options = parse_vars(argv[2:])
    setup_logging(config_uri)
    settings = get_appsettings(config_uri, options=options)
    session_factory = get_session_factory(get_engine(settings))
    log.info("Starting worker")
    consumer = Consumer(session_factory)

    def received_sigterm(signum, frame):
        nonlocal consumer
        log.info("Received SIGTERM signal")
        consumer.stop()

    signal.signal(signal.SIGTERM, received_sigterm)
    consumer.work()
    log.info("Closed gracefully")
