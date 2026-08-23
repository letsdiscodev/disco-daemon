import os

__version__ = "0.32.0"


def daemon_image() -> str:
    return os.environ.get("DISCO_IMAGE") or f"letsdiscodev/daemon:{__version__}"
