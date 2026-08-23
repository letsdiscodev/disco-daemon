"""Backup the live dqlite database to a portable SQLite file.

We can't `.backup` directly against dqlite's data dir — dqlite uses a
custom VFS and the on-disk layout isn't a plain SQLite file. So we
materialize a snapshot through the daemon's own SQL connection, stage
it in a stdlib in-memory SQLite, and use Python's online backup API
(sqlite3.Connection.backup) to write a clean .db file out.

The resulting file is a standard SQLite database. Operators can open it
with the `sqlite3` CLI (e.g. `sqlite3 latest.db .schema`) without any
disco-specific tooling.

Retention is age-based, applied during periodic pushes:
  - last 4 hours: keep every periodic backup
  - 4h-7d: keep one per day (the newest in each day)
  - 7d-13w: keep one per week
  - older: delete

Backups named with `pre-update-`, `post-update-`, or `pre-recovery-`
prefixes are exempt from thinning and kept indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.dialects import sqlite as sqlite_dialect_module
from sqlalchemy.schema import CreateIndex, CreateTable

from disco.models.db import AsyncSession
from disco.models.meta import base_metadata

log = logging.getLogger(__name__)

BACKUP_DIR = pathlib.Path("/disco/backups")
LATEST_FILENAME = "latest.db"
PERIODIC_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.db$")
SPECIAL_PREFIXES = ("pre-update-", "post-update-", "pre-recovery-")

# Retention.
KEEP_RECENT_HOURS = 4
KEEP_DAILY_DAYS = 7
KEEP_WEEKLY_WEEKS = 13

# Backup size monitoring thresholds (#3).
SIZE_WARN_BYTES = 50 * 1024 * 1024
SIZE_ERROR_BYTES = 500 * 1024 * 1024


def utc_iso_filename(when: datetime) -> str:
    """Filename-safe UTC ISO timestamp, e.g. 2026-05-23T14-30-00Z."""
    if when.tzinfo is None:
        raise ValueError("when must be timezone-aware")
    s = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return s


def periodic_filename(when: datetime) -> str:
    return f"{utc_iso_filename(when)}.db"


def special_filename(prefix: str, when: datetime) -> str:
    assert prefix in SPECIAL_PREFIXES
    return f"{prefix}{utc_iso_filename(when)}.db"


def _adapt_value(value):
    """Coerce SQLAlchemy row values to types stdlib sqlite3 will accept.

    Native-passthrough for the types sqlite3 already understands. Datetime
    and date are normalized to ISO 8601 strings. Anything else triggers a
    loud error rather than silently producing a broken backup — a future
    schema migration that adds Decimal/UUID/bytes/JSON columns should
    surface here so we update the adapter intentionally.
    """
    if value is None:
        return value
    if isinstance(value, (str, int, float, bool, bytes, bytearray)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(
        f"backup serializer has no adapter for {type(value).__name__}; "
        "update _adapt_value to handle this column type"
    )


async def make_local_backup(target: pathlib.Path) -> None:
    """Write a portable .db file at `target` containing a snapshot of the
    live dqlite database. Atomic via tmp+rename."""
    log.info("Creating dqlite backup at %s", target)

    mem = sqlite3.connect(":memory:")
    try:
        _build_schema_in_mem(mem)
        await _copy_data_into_mem(mem)
        mem.commit()

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        file_conn = sqlite3.connect(tmp)
        try:
            mem.backup(file_conn)
            file_conn.commit()
        finally:
            file_conn.close()
        tmp.replace(target)
    finally:
        mem.close()

    size = target.stat().st_size
    if size >= SIZE_ERROR_BYTES:
        log.error("dqlite backup is unexpectedly large: %d bytes", size)
    elif size >= SIZE_WARN_BYTES:
        log.warning("dqlite backup is large: %d bytes", size)
    else:
        log.info("dqlite backup size: %d bytes", size)


def _build_schema_in_mem(mem: sqlite3.Connection) -> None:
    """Mirror disco's schema into the in-memory SQLite using the sqlite dialect."""
    dialect = sqlite_dialect_module.dialect()
    for table in base_metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        if ddl:
            mem.execute(ddl)
        for index in table.indexes:
            idx_ddl = str(CreateIndex(index).compile(dialect=dialect)).strip()
            if idx_ddl:
                mem.execute(idx_ddl)


async def _copy_data_into_mem(mem: sqlite3.Connection) -> None:
    """Snapshot-isolated copy of every table into the in-memory SQLite.

    Acquires the dqlite write lock up-front so the snapshot is pinned for the
    whole copy (a deferred read transaction can lose its snapshot if promoted to
    write later or if other connections commit between our SELECTs; an IMMEDIATE
    transaction pins it, keeping cross-table reads consistent — important for FK
    validity on restore). The write engine runs with session_mode="immediate",
    so `session.begin()` already opens the transaction as BEGIN IMMEDIATE; an
    additional explicit BEGIN IMMEDIATE would nest and raise "cannot start a
    transaction within a transaction".
    """
    async with AsyncSession() as session:
        async with session.begin():
            for table in base_metadata.sorted_tables:
                result = await session.execute(table.select())
                rows = result.all()
                if not rows:
                    continue
                placeholders = ", ".join("?" * len(table.columns))
                cols = ", ".join(f'"{c.name}"' for c in table.columns)
                adapted = [tuple(_adapt_value(v) for v in row) for row in rows]
                mem.executemany(
                    f'INSERT INTO "{table.name}" ({cols}) VALUES ({placeholders})',
                    adapted,
                )


def replace_latest(latest_path: pathlib.Path, source: pathlib.Path) -> None:
    """Atomically point `latest.db` at the content of `source`.

    Uses shutil.copyfile so we don't materialize the whole file in
    memory — backups can grow to hundreds of MB.
    """
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = latest_path.with_suffix(latest_path.suffix + ".tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(latest_path)


def thin(backup_dir: pathlib.Path, now: datetime) -> list[pathlib.Path]:
    """Apply retention policy to periodic backups. Returns deleted paths.

    Special-prefixed backups (pre-update-, post-update-, pre-recovery-)
    are never touched. `latest.db` is never touched.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    files: list[tuple[datetime, pathlib.Path]] = []
    for f in backup_dir.iterdir():
        if not f.is_file():
            continue
        if not PERIODIC_FILENAME_RE.match(f.name):
            continue
        try:
            t = datetime.strptime(f.stem, "%Y-%m-%dT%H-%M-%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        files.append((t, f))

    files.sort(key=lambda x: x[0])

    keep: set[pathlib.Path] = set()
    cutoff_recent = now - timedelta(hours=KEEP_RECENT_HOURS)
    cutoff_daily = now - timedelta(days=KEEP_DAILY_DAYS)
    cutoff_weekly = now - timedelta(weeks=KEEP_WEEKLY_WEEKS)
    seen_days: set[date] = set()
    seen_weeks: set[tuple[int, int]] = set()

    for t, f in reversed(files):
        if t >= cutoff_recent:
            keep.add(f)
        elif t >= cutoff_daily:
            day = t.date()
            if day not in seen_days:
                seen_days.add(day)
                keep.add(f)
        elif t >= cutoff_weekly:
            week = t.isocalendar()[:2]
            if week not in seen_weeks:
                seen_weeks.add(week)
                keep.add(f)
        # else: too old, drop

    deleted: list[pathlib.Path] = []
    for _, f in files:
        if f not in keep:
            try:
                f.unlink()
                deleted.append(f)
            except FileNotFoundError:
                # Concurrent pull_backup on another caller may have
                # already unlinked it; treat as a no-op.
                pass
    return deleted


def list_backups(backup_dir: pathlib.Path) -> Iterable[pathlib.Path]:
    """Iterate over .db files (excluding tmp files) in `backup_dir`."""
    if not backup_dir.exists():
        return
    for f in sorted(backup_dir.iterdir()):
        if f.is_file() and f.suffix == ".db":
            yield f


# Serialises concurrent calls from the hourly cron and the API-key event
# listener. Two simultaneous backups would race on latest.db, dispatch
# two global-jobs, and produce non-deterministic latest contents on each
# receiving manager. The lock makes the second caller wait; in practice
# both wake-up sources fire infrequently so the wait is short.
_periodic_backup_lock = asyncio.Lock()


async def make_and_push_periodic_backup() -> None:
    """Create a periodic backup on local disk and distribute it (dqlite mode).

    On every call: writes /disco/backups/<UTC-ISO>Z.db, refreshes
    latest.db. In dqlite mode, additionally dispatches the global-job to
    fan the file out to every manager; the receiving job also runs
    thinning on each node. In sqlite mode (single daemon node), thinning
    runs locally instead.
    """
    from disco import config
    from disco.utils.backup_distribution import push_backup_to_all_managers

    async with _periodic_backup_lock:
        now = datetime.now(timezone.utc)
        filename = periodic_filename(now)
        target = BACKUP_DIR / filename
        await make_local_backup(target)
        replace_latest(BACKUP_DIR / LATEST_FILENAME, target)
        if config.is_dqlite_mode():
            await push_backup_to_all_managers(filename, is_periodic=True)
        else:
            thin(BACKUP_DIR, now)


def make_local_backup_sync(target: pathlib.Path) -> None:
    """Sync wrapper for non-async callers (e.g. the update script).

    Disposes the module-global async engine before running. asyncio.run
    creates a fresh event loop each call, but the SQLAlchemy engine is
    process-global and its connection pool is bound to the first loop
    it saw. Calling this twice in one process (pre + post-update in
    disco_update) would otherwise reuse a destroyed loop's connections.
    """
    from disco.models.db import get_async_engine

    async def run() -> None:
        try:
            await make_local_backup(target)
        finally:
            await get_async_engine().dispose()

    asyncio.run(run())


# iterdump emits these around the actual statements; the caller's outer
# transaction handles commit semantics so we strip them.
_TRANSACTION_BOUNDARY_RE = re.compile(
    r"^(BEGIN(?:\s+TRANSACTION)?|COMMIT)\s*;?\s*$", re.I
)


async def replay_sqlite_file(path: pathlib.Path) -> None:
    """Stream a SQLite file's iterdump output into the live database.

    Replays schema DDL, all rows, and the alembic_version row in a single
    transaction through the module-global engine. Used by disco_restore
    (backup file -> fresh dqlite) and disco_migrate_to_dqlite (live sqlite
    file -> fresh dqlite). Statements stream straight from iterdump —
    only its outer BEGIN/COMMIT markers are skipped (we control commit) —
    so the whole dump is never buffered in memory; sources can be large
    (api_key_usages grows with every authenticated request).
    """
    from sqlalchemy import text as sql_text

    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        async with AsyncSession.begin() as session:
            for stmt in src.iterdump():
                if _TRANSACTION_BOUNDARY_RE.match(stmt.strip()):
                    continue
                await session.execute(sql_text(stmt))
    finally:
        src.close()
