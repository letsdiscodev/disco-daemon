"""Copy Caddy's certmagic file storage from the disco-caddy-data volume into the
caddy_data table so the dqlite-backed Caddy serves existing certificates.

    docker run --rm \\
      --mount source=disco-caddy-data,target=/old-caddy-data,readonly \\
      --network disco-dqlite \\
      <disco-image> disco_migrate_caddy_storage

The Caddy image sets XDG_DATA_HOME=/data, so the certmagic root is
<volume>/caddy; override with CADDY_DATA_SRC.
"""

import logging
import os
from pathlib import Path

from sqlalchemy import text

from disco.models.db import get_engine

log = logging.getLogger(__name__)

DEFAULT_SRC = "/old-caddy-data/caddy"


def migrate(src: str) -> int:
    root = Path(src)
    if not root.is_dir():
        print(f"No certmagic data at {root}; nothing to migrate")
        return 0

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS caddy_data ("
                "key TEXT PRIMARY KEY, value BLOB NOT NULL, modified INTEGER NOT NULL)"
            )
        )

    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        value = path.read_bytes()
        modified = int(path.stat().st_mtime * 1_000_000_000)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO caddy_data(key, value, modified) "
                    "VALUES (:k, :v, :m) "
                    "ON CONFLICT(key) DO UPDATE SET value = :v, modified = :m"
                ),
                {"k": key, "v": value, "m": modified},
            )
        count += 1
        print(f"  migrated {key} ({len(value)} bytes)")

    print(f"Migrated {count} file(s) into caddy_data")
    return count


def main() -> None:
    # Runs in a one-shot container without disco-data, so pin the mode.
    from disco import config

    config.pin_mode("dqlite")
    logging.basicConfig(level=logging.INFO)
    src = os.environ.get("CADDY_DATA_SRC", DEFAULT_SRC)
    print(f"Migrating Caddy storage from {src} into dqlite")
    migrate(src)
