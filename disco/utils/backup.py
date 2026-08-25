from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.dialects import sqlite as sqlite_dialect_module
from sqlalchemy.schema import CreateIndex, CreateTable

from disco.models.db import AsyncSession
from disco.models.meta import base_metadata

log = logging.getLogger(__name__)

BACKUP_DIR = pathlib.Path("/disco/backups")
LATEST_FILENAME = "latest.db"
PERIODIC_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.db$")
SPECIAL_PREFIXES = ("pre-update-", "post-update-", "pre-recovery-")

# Retention for periodic backups: keep everything from the last 4 hours,
# one per day up to 7 days, one per week up to 13 weeks, then delete.
# Special-prefixed backups are never thinned.
KEEP_RECENT_HOURS = 4
KEEP_DAILY_DAYS = 7
KEEP_WEEKLY_WEEKS = 13

SIZE_WARN_BYTES = 50 * 1024 * 1024
SIZE_ERROR_BYTES = 500 * 1024 * 1024


def utc_iso_filename(when: datetime) -> str:
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
    # The write engine's session.begin() is already BEGIN IMMEDIATE, which pins
    # the snapshot across tables; an explicit BEGIN IMMEDIATE here would nest.
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
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = latest_path.with_suffix(latest_path.suffix + ".tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(latest_path)


def thin(backup_dir: pathlib.Path, now: datetime) -> list[pathlib.Path]:
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

    deleted: list[pathlib.Path] = []
    for _, f in files:
        if f not in keep:
            try:
                f.unlink()
                deleted.append(f)
            except FileNotFoundError:
                pass
    return deleted


# The hourly cron and the API-key listener would otherwise race on latest.db.
_periodic_backup_lock = asyncio.Lock()


async def make_and_push_periodic_backup() -> None:
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
    # The engine pool is bound to the first event loop it saw; dispose it so
    # a second asyncio.run in the same process (disco_update) doesn't reuse it.
    from disco.models.db import get_async_engine

    async def run() -> None:
        try:
            await make_local_backup(target)
        finally:
            await get_async_engine().dispose()

    asyncio.run(run())


def read_keyvalue_from_sqlite_file(path: pathlib.Path, key: str) -> str | None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM key_values WHERE key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# iterdump's own BEGIN/COMMIT; the session transaction handles commit.
_TRANSACTION_BOUNDARY_RE = re.compile(
    r"^(BEGIN(?:\s+TRANSACTION)?|COMMIT)\s*;?\s*$", re.I
)


async def replay_sqlite_file(path: pathlib.Path) -> None:
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
