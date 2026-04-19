"""Database engines for the Trip Planner.

Three engines:

* **trips_engine** — read/write, owns the trip-planner + yearly planner
  tables. Postgres in production (via `DATABASE_URL`), SQLite locally
  (via `TRIPS_DB_PATH`). Schema managed by Alembic (`backend/alembic/`).
* **golf_engine** — read/write, SQLite only. Owns the golf library
  (resorts, courses, entity_images). Bundled at `backend/data/golf.db`;
  override with `GOLF_DB_PATH` (e.g. a mounted Railway volume) to make
  runtime UI edits persistent across deploys. Schema is maintained via
  plain `create_all` — no Alembic, no migrations, evolves with the model.
* **vacationmap_engine** — read-only companion SQLite. Bundled at
  `backend/data/vacation.db`; override with `VACATIONMAP_DB_PATH`.

Inter-engine references (e.g. a shortlisted destination's `resort_id`
pointing at a `golf_resorts.id`) are plain integer columns with no FK
constraint. Joins across engines are not possible; fetch IDs from the
trips engine and then look up the golf record in a separate session.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_BUNDLED_VACATIONMAP = _BACKEND_DIR / "data" / "vacation.db"
_BUNDLED_TRIPS_SEED = _BACKEND_DIR / "data" / "trips.seed.db"
_BUNDLED_GOLF_DB = _BACKEND_DIR / "data" / "golf.db"


# -----------------------------------------------------------------------------
# Trips engine — Postgres in prod, SQLite in dev
# -----------------------------------------------------------------------------


def _resolve_trips_url() -> str:
    """Pick the right SQLAlchemy URL for the trips DB.

    1. If `DATABASE_URL` is set (Railway convention), use it. Normalize the
       `postgres://` scheme some hosts still emit to `postgresql://`.
    2. Else fall back to SQLite at `TRIPS_DB_PATH` (default `./trips.db`).
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        return url
    path = os.environ.get("TRIPS_DB_PATH", "./trips.db")
    return f"sqlite:///{path}"


TRIPS_DATABASE_URL = _resolve_trips_url()
# Kept for backwards compatibility with tests / tooling that referenced it.
TRIPS_DB_PATH = (
    TRIPS_DATABASE_URL[len("sqlite:///") :]
    if TRIPS_DATABASE_URL.startswith("sqlite:///")
    else ""
)


def _engine_kwargs(url: str) -> dict:
    """Dialect-aware engine kwargs."""
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    # Postgres: sane pool settings for Railway's small free tier.
    return {"pool_pre_ping": True, "pool_recycle": 300}


trips_engine: Engine = create_engine(
    TRIPS_DATABASE_URL, **_engine_kwargs(TRIPS_DATABASE_URL)
)
TripsSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=trips_engine)
TripsBase = declarative_base()


# -----------------------------------------------------------------------------
# Golf engine — always SQLite, always bundled, (mostly) static reference
# -----------------------------------------------------------------------------


def _resolve_golf_path() -> str:
    """Return the filesystem path the golf SQLite should live at.

    If the user points `GOLF_DB_PATH` elsewhere (e.g. a mounted volume so
    UI edits survive redeploys), we copy the bundled snapshot there on
    first boot in `init_golf_db()`.
    """
    return os.environ.get("GOLF_DB_PATH") or str(_BUNDLED_GOLF_DB)


GOLF_DB_PATH = _resolve_golf_path()
golf_engine = create_engine(
    f"sqlite:///{GOLF_DB_PATH}",
    connect_args={"check_same_thread": False},
)
GolfSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=golf_engine)
GolfBase = declarative_base()


# -----------------------------------------------------------------------------
# VacationMap engine — always SQLite, always read-only in practice
# -----------------------------------------------------------------------------


VACATIONMAP_DB_PATH = os.environ.get("VACATIONMAP_DB_PATH") or (
    str(_BUNDLED_VACATIONMAP)
    if _BUNDLED_VACATIONMAP.is_file()
    else os.path.expanduser("~/Documents/VacationMap/backend/vacation.db")
)
vacationmap_engine = create_engine(
    f"sqlite:///{VACATIONMAP_DB_PATH}",
    connect_args={"check_same_thread": False},
)
VacationMapSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=vacationmap_engine
)


# -----------------------------------------------------------------------------
# Startup: schema bootstrap + seed
# -----------------------------------------------------------------------------


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _run_alembic_upgrade():
    """Apply all pending Alembic migrations against the active trips engine.

    If the target DB already has our schema but no `alembic_version` table
    (e.g. a local SQLite created before Alembic was introduced, or an
    imported Postgres snapshot), we stamp it at `head` first so Alembic
    knows where to start from.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect as sqla_inspect

    config_path = _BACKEND_DIR / "alembic.ini"
    cfg = Config(str(config_path))
    # Point Alembic at the runtime engine URL (bypasses alembic.ini's value).
    cfg.set_main_option("sqlalchemy.url", TRIPS_DATABASE_URL)

    inspector = sqla_inspect(trips_engine)
    existing = set(inspector.get_table_names())
    has_alembic = "alembic_version" in existing
    has_schema = "trip_plans" in existing  # any real table works as a probe
    if has_schema and not has_alembic:
        # Pre-Alembic DB with our schema — baseline it so we don't try to
        # re-create tables.
        command.stamp(cfg, "head")
    command.upgrade(cfg, "head")


def _seed_from_bundled_sqlite_if_empty():
    """If the trips DB has no golf resorts yet and a bundled seed snapshot
    exists, copy its rows row-by-row into the active engine.

    Works for both SQLite and Postgres targets (the seed file is always
    SQLite). No-op if the target already has data or the seed is missing.
    """
    import sqlite3
    from datetime import date, datetime

    from sqlalchemy import Date, DateTime, MetaData, Table
    from sqlalchemy import inspect as sqla_inspect
    from sqlalchemy import text

    if os.environ.get("DISABLE_TRIPS_SEED") == "1":
        return
    if not _BUNDLED_TRIPS_SEED.is_file():
        return

    inspector = sqla_inspect(trips_engine)
    if "trip_plans" not in inspector.get_table_names():
        return  # Alembic hasn't built the schema yet — unexpected, bail out.
    with trips_engine.connect() as conn:
        (count,) = conn.execute(text("SELECT COUNT(*) FROM trip_plans")).one()
    if count > 0:
        return  # Already populated (existing deploy) — don't overwrite.

    def _coerce_value(value, col_type):
        """SQLite stores datetimes as ISO strings; SQLAlchemy's DateTime
        column for Postgres expects real datetime objects. Coerce on the way
        in so both engines are happy."""
        if value is None:
            return None
        if isinstance(col_type, DateTime) and isinstance(value, str):
            return datetime.fromisoformat(value)
        if isinstance(col_type, Date) and isinstance(value, str):
            return date.fromisoformat(value)
        return value

    # Pull every row from every table in the seed SQLite, match against our
    # declared ORM tables, and bulk-insert through SQLAlchemy so Postgres gets
    # proper type coercion.
    metadata = MetaData()
    metadata.reflect(bind=trips_engine)
    seed_conn = sqlite3.connect(_BUNDLED_TRIPS_SEED)
    seed_conn.row_factory = sqlite3.Row
    try:
        with trips_engine.begin() as conn:
            for table_name in _seed_table_order():
                if table_name not in metadata.tables:
                    continue
                target_tbl: Table = metadata.tables[table_name]
                try:
                    cursor = seed_conn.execute(f"SELECT * FROM {table_name}")
                except sqlite3.OperationalError:
                    continue  # table missing in seed — skip
                rows = [dict(r) for r in cursor.fetchall()]
                if not rows:
                    continue
                col_types = {c.name: c.type for c in target_tbl.columns}
                filtered = [
                    {
                        k: _coerce_value(v, col_types[k])
                        for k, v in r.items()
                        if k in col_types
                    }
                    for r in rows
                ]
                conn.execute(target_tbl.insert(), filtered)
    finally:
        seed_conn.close()

    # On Postgres, reset sequences so subsequent inserts get IDs > the highest
    # seeded ID. (SQLite uses rowid/autoincrement, no sequence to reset.)
    if trips_engine.dialect.name == "postgresql":
        with trips_engine.begin() as conn:
            for table_name in _seed_table_order():
                if table_name not in metadata.tables:
                    continue
                if "id" not in {c.name for c in metadata.tables[table_name].columns}:
                    continue
                conn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table_name}), 1))"
                    )
                )


def _seed_table_order() -> list[str]:
    """Insertion order that respects FK dependencies across the trips DB
    (golf tables live in a separate engine and are seeded by the bundled
    `backend/data/golf.db` directly)."""
    return [
        "trip_plans",
        "conversations",
        "conversation_messages",
        "suggested_destinations",
        "shortlisted_destinations",
        "excluded_destinations",
        "year_plans",
        "year_options",
        "slots",
    ]


def init_trips_db():
    """Run on FastAPI startup (trips + yearly tables).

    1. Register all ORM models so Alembic autogenerate sees the metadata.
    2. Ensure schema via Alembic (`upgrade head`; baseline-stamp first if
       the DB was created before Alembic).
    3. If the DB is fresh and a bundled seed snapshot ships with the deploy,
       load it row-by-row through SQLAlchemy.
    """
    # Registration imports — must run before Alembic sees the metadata in
    # autogenerate mode and before create_all (if used as a fallback).
    from .trips import models as _trip_models  # noqa: F401
    from .yearly import models as _yearly_models  # noqa: F401

    _run_alembic_upgrade()
    _seed_from_bundled_sqlite_if_empty()


def init_golf_db():
    """Run on FastAPI startup (golf library — separate SQLite).

    1. If `GOLF_DB_PATH` points somewhere that doesn't exist yet AND the
       bundled snapshot does, copy the bundle → target. Lets users mount a
       Railway volume and get the curated library pre-populated.
    2. Run `create_all` so any new golf model lands in the SQLite without
       needing a migration framework.
    """
    from .golf import models as _golf_models  # noqa: F401

    target = Path(GOLF_DB_PATH)
    if (
        not target.exists()
        and _BUNDLED_GOLF_DB.is_file()
        and target != _BUNDLED_GOLF_DB
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_BUNDLED_GOLF_DB, target)

    GolfBase.metadata.create_all(bind=golf_engine)


# -----------------------------------------------------------------------------
# FastAPI dependencies
# -----------------------------------------------------------------------------


def get_trips_db():
    db = TripsSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_golf_db():
    db = GolfSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_vacationmap_db():
    db = VacationMapSessionLocal()
    try:
        yield db
    finally:
        db.close()
