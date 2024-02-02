import logging
import signal

from disco.utils.mq.consumer import Consumer

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    log.info("Starting worker")
    consumer = Consumer()

    def received_sigterm(signum, frame):
        nonlocal consumer
        log.info("Received SIGTERM signal")
        consumer.stop()

    signal.signal(signal.SIGTERM, received_sigterm)
    consumer.work()
    log.info("Closed gracefully")
