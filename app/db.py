"""SQLite connection management and schema initialization."""
from __future__ import annotations

import sqlite3
import logging
import os

logger = logging.getLogger(__name__)

_connection: sqlite3.Connection | None = None
_db_path: str = "mirriox.db"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS admins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username    TEXT,
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    added_by    INTEGER
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    channel_ref TEXT NOT NULL UNIQUE,
    title       TEXT,
    resolved_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS destinations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    channel_ref TEXT NOT NULL UNIQUE,
    title       TEXT,
    resolved_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blocked_words (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    word     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    added_by INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    source_id         INTEGER NOT NULL REFERENCES sources(id),
    destination_id    INTEGER NOT NULL REFERENCES destinations(id),
    mode              TEXT NOT NULL CHECK(mode IN ('all','date_range','id_range','single_id')),
    date_from         TEXT,
    date_to           TEXT,
    id_from           INTEGER,
    id_to             INTEGER,
    single_message_id INTEGER,
    use_blocked_words INTEGER NOT NULL DEFAULT 1,
    group_media       INTEGER NOT NULL DEFAULT 1,
    copy_text         INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK(status IN ('draft','pending','running','paused',
                                       'completed','cancelled','failed','waiting_retry')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    started_at        TEXT,
    completed_at      TEXT,
    last_updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    total_messages    INTEGER DEFAULT 0,
    copied_count      INTEGER DEFAULT 0,
    skipped_count     INTEGER DEFAULT 0,
    failed_count      INTEGER DEFAULT 0,
    last_processed_id INTEGER,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    max_retries       INTEGER NOT NULL DEFAULT 3,
    next_retry_at     TEXT,
    error_message     TEXT
);

-- One slice of a job's source ID range, so several userbot accounts can copy the
-- same job at once. Rows exist only for jobs that were actually sharded: with a
-- single eligible account the job stays one ordered pass and this table is empty
-- for it.
CREATE TABLE IF NOT EXISTS job_chunks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL,
    id_from             INTEGER NOT NULL,
    id_to               INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','done')),
    assigned_userbot_id INTEGER,
    last_processed_id   INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job_id, id_from)
);

-- Append-only log of every error a job hit, so the UI can show a dated history
-- instead of only the last one. jobs.error_message stays as the "current" error;
-- this table is the history behind it. No FK to jobs — see job_chunks above.
CREATE TABLE IF NOT EXISTS job_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL,
    userbot_id  INTEGER,
    error       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS copied_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    dest_message_id   INTEGER,
    status            TEXT NOT NULL CHECK(status IN ('copied','skipped','failed')),
    skip_reason       TEXT,
    -- Which account actually did the transfer. Recorded here (not derived from
    -- jobs.assigned_userbot_id, which is transient and cleared when a job ends)
    -- so per-account stats and per-account daily limits survive job completion.
    userbot_id        INTEGER,
    processed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job_id, source_message_id)
);

CREATE TABLE IF NOT EXISTS worker_state (
    id             INTEGER PRIMARY KEY CHECK(id = 1),
    status         TEXT NOT NULL DEFAULT 'idle'
                   CHECK(status IN ('idle','running','stopped','error')),
    current_job_id INTEGER REFERENCES jobs(id),
    last_heartbeat TEXT,
    error_message  TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS duplicate_scans (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_ref             TEXT NOT NULL DEFAULT '',
    channel_title           TEXT NOT NULL DEFAULT '',
    dest_id                 INTEGER REFERENCES destinations(id),
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','running','done','failed')),
    messages_scanned        INTEGER DEFAULT 0,
    total_messages          INTEGER DEFAULT 0,
    duplicate_groups        INTEGER DEFAULT 0,
    wasted_count            INTEGER DEFAULT 0,
    last_scanned_message_id INTEGER DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at            TEXT,
    report_url              TEXT,
    error_msg               TEXT
);

CREATE TABLE IF NOT EXISTS duplicate_scan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL REFERENCES duplicate_scans(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL,
    media_id    INTEGER NOT NULL,
    media_type  TEXT    NOT NULL CHECK(media_type IN ('document','photo')),
    file_size   INTEGER,
    mime_type   TEXT,
    msg_date    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS delete_scan_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id       INTEGER NOT NULL REFERENCES duplicate_scans(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','running','done','failed')),
    deleted_count INTEGER DEFAULT 0,
    error_msg     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT
);

-- Userbot accounts. Each row owns its own Telethon session file.
-- The account from .env is auto-registered on first run as is_default=1.
CREATE TABLE IF NOT EXISTS userbots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    session_name  TEXT NOT NULL UNIQUE,
    telegram_id   INTEGER,
    username      TEXT,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK(status IN ('active','inactive','unauthorized','error')),
    is_default    INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT,
    error_message TEXT
);

-- Per-account access to each source/destination channel.
-- Every active userbot probes every channel, so the UI can report exactly which
-- accounts can reach it. A missing row means "this account hasn't checked yet".
CREATE TABLE IF NOT EXISTS channel_access (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_kind TEXT NOT NULL CHECK(channel_kind IN ('source','destination')),
    channel_id   INTEGER NOT NULL,
    userbot_id   INTEGER NOT NULL,
    has_access   INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    checked_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel_kind, channel_id, userbot_id)
);

-- Hyper backup: one config row per userbot account.
-- Hyper is per-account by design — a listener on this account's *outgoing*
-- messages, so it is pinned to the account whose traffic it mirrors and never
-- migrates to another (that would back up the wrong account's uploads).
-- Dedup is handled entirely by transferred_registry (content-based, cross-account),
-- so hyper never touches copied_messages — whose (job_id, source_message_id) key
-- would collide across the many different chats hyper captures from.
CREATE TABLE IF NOT EXISTS hyper_configs (
    userbot_id     INTEGER PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 0,
    destination_id INTEGER,
    copied_count   INTEGER NOT NULL DEFAULT 0,
    skipped_count  INTEGER NOT NULL DEFAULT 0,
    failed_count   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per successful hyper transfer, purely for the per-account daily cap.
-- Hyper can't use copied_messages (its (job_id, source_message_id) key collides
-- across the many chats hyper captures from), but its sends still consume the
-- account's Telegram quota, so they must count toward the same daily limit.
CREATE TABLE IF NOT EXISTS hyper_transfers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    userbot_id     INTEGER NOT NULL,
    transferred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Smart per-account, per-media-type filter rules for hyper backup.
-- A NULL bound means "not checked"; combine says whether the set bounds are
-- ANDed (all must hold) or ORed (any). All-NULL bounds = pass everything.
CREATE TABLE IF NOT EXISTS hyper_filters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    userbot_id   INTEGER NOT NULL,
    media_type   TEXT NOT NULL CHECK(media_type IN ('video','image','file','audio')),
    enabled      INTEGER NOT NULL DEFAULT 1,
    min_size     INTEGER,   -- bytes
    max_size     INTEGER,   -- bytes
    min_duration INTEGER,   -- seconds
    max_duration INTEGER,   -- seconds
    combine      TEXT NOT NULL DEFAULT 'and' CHECK(combine IN ('and','or')),
    UNIQUE(userbot_id, media_type)
);

-- Pending hyper backups waiting for the account to be able to send (daily cap
-- reached, FloodWait, or a transient failure). Only (chat, message) is stored —
-- the media stays on Telegram's servers, so the drain loop re-fetches it. This
-- is what makes hyper a reliable backup rather than best-effort: nothing an
-- account uploads is lost just because it was busy or capped when it arrived.
CREATE TABLE IF NOT EXISTS hyper_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    userbot_id   INTEGER NOT NULL,
    chat_id      INTEGER NOT NULL,   -- marked peer id of the source chat
    message_id   INTEGER NOT NULL,
    dest_id      INTEGER NOT NULL,   -- backup destination captured at enqueue time
    enqueued_at  TEXT NOT NULL DEFAULT (datetime('now')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(userbot_id, chat_id, message_id, dest_id)
);

-- Global library of reusable Python snippets (like Linux scripts/aliases).
-- Any script may be run on any userbot account the admin selects.
CREATE TABLE IF NOT EXISTS scripts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    code       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ad-hoc code execution queue / history, one row per run, pinned to one account.
-- The bot enqueues a row; the target account's runner claims and runs it against
-- its live client (bot and worker share only the DB — same pattern as scans).
-- A run is bound to a single userbot, so there is never a cross-account race.
CREATE TABLE IF NOT EXISTS userbot_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    userbot_id  INTEGER NOT NULL,
    script_id   INTEGER,                 -- NULL = one-off quick run
    code        TEXT NOT NULL,           -- snapshot of the code at run time
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','done','error')),
    output      TEXT,                    -- stdout + return value + traceback
    chat_id     INTEGER,                 -- admin chat to deliver the result to
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);

-- Global registry of every transferred item, keyed by content.
-- Scope is per-destination: the same content may be sent to different channels.
CREATE TABLE IF NOT EXISTS transferred_registry (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key         TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK(kind IN ('media','text')),
    destination_id    INTEGER NOT NULL,
    source_id         INTEGER,
    source_message_id INTEGER,
    job_id            INTEGER,
    transferred_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(destination_id, dedup_key)
);

-- Durable per-channel-pair sync watermark. One row per (source, destination):
-- the highest source message id already synced to that destination, and when.
-- Deliberately keyed on the channel pair, not on a job — so it survives job
-- deletion and lets a future re-sync of the same pair resume from here instead
-- of scanning the whole history again. Only full-history ('all') single-destination
-- jobs write it, because only for those does "everything up to id X is delivered"
-- actually hold. See channel_sync_repo.
CREATE TABLE IF NOT EXISTS channel_sync_state (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL,
    destination_id INTEGER NOT NULL,
    last_synced_id INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    last_job_name  TEXT,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, destination_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status        ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_channel_sync_pair  ON channel_sync_state(source_id, destination_id);
CREATE INDEX IF NOT EXISTS idx_job_chunks_job     ON job_chunks(job_id, status);
CREATE INDEX IF NOT EXISTS idx_job_errors_job     ON job_errors(job_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_copied_msg_job     ON copied_messages(job_id);
CREATE INDEX IF NOT EXISTS idx_copied_src_id      ON copied_messages(job_id, source_message_id);
CREATE INDEX IF NOT EXISTS idx_scan_items_media   ON duplicate_scan_items(scan_id, media_id);
CREATE INDEX IF NOT EXISTS idx_transferred_key    ON transferred_registry(destination_id, dedup_key);
CREATE INDEX IF NOT EXISTS idx_hyper_filters_ub    ON hyper_filters(userbot_id);
CREATE INDEX IF NOT EXISTS idx_hyper_transfers_ub  ON hyper_transfers(userbot_id, transferred_at);
CREATE INDEX IF NOT EXISTS idx_hyper_queue_ub      ON hyper_queue(userbot_id, id);
CREATE INDEX IF NOT EXISTS idx_userbot_tasks_claim ON userbot_tasks(userbot_id, status, id);
CREATE INDEX IF NOT EXISTS idx_userbots_status    ON userbots(status);
CREATE INDEX IF NOT EXISTS idx_channel_access_ch  ON channel_access(channel_kind, channel_id);
-- NOTE: the index on copied_messages(userbot_id, ...) is created in _run_migrations,
-- not here. This script runs before migrations, so on an existing database the
-- column does not exist yet and CREATE INDEX would fail.

INSERT OR IGNORE INTO worker_state(id) VALUES(1);

INSERT OR IGNORE INTO app_settings(key,value) VALUES
    ('min_delay_ms',        '2000'),
    ('max_delay_ms',        '5000'),
    ('flood_buffer_min_s',  '5'),
    ('flood_buffer_max_s',  '10'),
    ('batch_size_min',      '50'),
    ('batch_size_max',      '100'),
    ('batch_pause_min_s',   '60'),
    ('batch_pause_max_s',   '120'),
    -- A FloodWait shorter than this is slept through in place instead of tearing
    -- the whole job pass down and spending one of its retries.
    ('flood_inline_max_s',  '60'),
    -- Minimum spacing per message into one destination channel, shared by every
    -- account writing to it. 0 disables the shared gate.
    ('dest_min_delay_ms',   '1000'),
    ('max_retries',         '5'),
    ('heartbeat_interval_s','30'),
    ('skip_duplicates',     '0'),
    ('main_chat_id',        ''),
    ('main_message_id',     '');
"""


def init(db_path: str) -> None:
    """Set the database path. Must be called before get_connection()."""
    global _db_path
    _db_path = db_path
    # Ensure parent directory exists
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Return (or create) the module-level SQLite connection."""
    global _connection
    if _connection is None:
        _connection = sqlite3.connect(_db_path, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA foreign_keys=ON")
        logger.debug("SQLite connection opened: %s", _db_path)
    return _connection


def init_schema() -> None:
    """Create all tables and seed default rows. Idempotent."""
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    _run_migrations(conn)
    conn.commit()
    logger.info("Database schema initialized")


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after initial schema. Safe to re-run."""
    # Must stay first: it rebuilds copied_messages with a positional `SELECT *`,
    # so any new column has to be added after the rebuild, never before it.
    _migrate_copied_messages_remove_fk(conn)
    # Per-account attribution for stats and per-account daily limits.
    # The index must be created here, after the column exists — SCHEMA_SQL runs first
    # and would fail on an existing database.
    _add_column_if_missing(conn, "copied_messages", "userbot_id",       "INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_copied_userbot "
        "ON copied_messages(userbot_id, status, processed_at)"
    )
    _add_column_if_missing(conn, "sources",       "validation_error",   "TEXT")
    _add_column_if_missing(conn, "destinations",  "validation_error",   "TEXT")
    _add_column_if_missing(conn, "jobs",          "content_types",      "TEXT DEFAULT 'file,image,text,video'")
    _migrate_content_types_add_file(conn)
    _add_column_if_missing(conn, "jobs",          "report_url",         "TEXT")
    _add_column_if_missing(conn, "jobs",          "group_media",        "INTEGER DEFAULT 1")
    _add_column_if_missing(conn, "jobs",          "copy_text",          "INTEGER DEFAULT 1")
    _add_column_if_missing(conn, "jobs",          "submitted_at",       "TEXT")
    _add_column_if_missing(conn, "jobs",          "created_by",         "INTEGER")
    # Continuous sync + multi-userbot assignment
    _add_column_if_missing(conn, "jobs",          "continuous",           "INTEGER DEFAULT 0")
    # A continuous job runs in two phases: copy the history its mode selects
    # (backfill_done=0), then listen for new messages (backfill_done=1).
    _add_column_if_missing(conn, "jobs",          "backfill_done",        "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "jobs",          "assigned_userbot_id",  "INTEGER")
    _add_column_if_missing(conn, "jobs",          "excluded_userbot_ids", "TEXT")
    # Optional positive allow-list: when set, only these accounts may run the job.
    # NULL/empty means "any active account" — the original, unrestricted behaviour.
    _add_column_if_missing(conn, "jobs",          "allowed_userbot_ids",  "TEXT")
    # Full destination list for random fan-out. NULL/empty means the job has a
    # single destination (the original behaviour); destination_id always holds
    # the primary (first) destination either way.
    _add_column_if_missing(conn, "jobs",          "destination_ids",      "TEXT")
    # Full hyper backup destination list for random fan-out. NULL/empty means the
    # account has a single backup channel (the original behaviour); destination_id
    # always holds the primary (first) destination either way.
    _add_column_if_missing(conn, "hyper_configs", "destination_ids",      "TEXT")
    # Channel extra-info columns
    for table in ("sources", "destinations"):
        _add_column_if_missing(conn, table, "username",           "TEXT")
        _add_column_if_missing(conn, table, "participants_count", "INTEGER")
        _add_column_if_missing(conn, table, "about",              "TEXT")
        _add_column_if_missing(conn, table, "verified",           "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, table, "channel_type",       "TEXT")
        _add_column_if_missing(conn, table, "total_messages",     "INTEGER")
        _add_column_if_missing(conn, table, "photos_count",       "INTEGER")
        _add_column_if_missing(conn, table, "videos_count",       "INTEGER")
        _add_column_if_missing(conn, table, "docs_count",         "INTEGER")
    # duplicate_scans: rebuild table if old source_id NOT NULL constraint exists
    _migrate_duplicate_scans(conn)
    # delete_scan_jobs: rebuild table if old source_id NOT NULL column exists
    _migrate_delete_scan_jobs(conn)
    # Add last_scanned_message_id column if missing
    _add_column_if_missing(conn, "duplicate_scans", "last_scanned_message_id", "INTEGER DEFAULT 0")
    # Backfill last_scanned_message_id from scan items for existing completed scans
    _backfill_last_scanned_message_id(conn)
    # Populate the per-channel-pair sync watermark from history that already exists.
    _backfill_channel_sync_state(conn)
    _seed_missing_settings(conn, {
        "flood_buffer_min_s": "5",
        "flood_buffer_max_s": "10",
        "batch_size_min":     "50",
        "batch_size_max":     "100",
        "batch_pause_min_s":  "60",
        "batch_pause_max_s":  "120",
        "flood_inline_max_s": "60",
        "dest_min_delay_ms":  "1000",
        "group_media":        "1",
        # Off by default — preserves the existing "always copy" behaviour
        "skip_duplicates":    "0",
        # Kill switch for the ad-hoc code execution feature. On by default; can be
        # turned off to disable running/enqueuing snippets entirely.
        "adhoc_enabled":      "1",
    })


def _migrate_duplicate_scans(conn: sqlite3.Connection) -> None:
    """
    Rebuild duplicate_scans if it still has source_id NOT NULL.
    Preserves existing rows by copying common columns.
    """
    try:
        # Check if duplicate_scans table exists at all
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='duplicate_scans'"
        ).fetchone()
        if not exists:
            return  # Will be created by SCHEMA_SQL with correct definition

        # Use PRAGMA table_info to check if source_id is NOT NULL (notnull=1)
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(duplicate_scans)")}
        source_col = cols.get("source_id")
        if source_col is None or source_col[3] == 0:
            # source_id doesn't exist or is already nullable — migration not needed
            return

        logger.info("Migration: rebuilding duplicate_scans to remove source_id NOT NULL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS duplicate_scans_v2")
        conn.execute("""
            CREATE TABLE duplicate_scans_v2 (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_ref      TEXT NOT NULL DEFAULT '',
                channel_title    TEXT NOT NULL DEFAULT '',
                dest_id          INTEGER REFERENCES destinations(id),
                status           TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','running','done','failed')),
                messages_scanned INTEGER DEFAULT 0,
                total_messages   INTEGER DEFAULT 0,
                duplicate_groups INTEGER DEFAULT 0,
                wasted_count     INTEGER DEFAULT 0,
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at     TEXT,
                report_url       TEXT,
                error_msg        TEXT
            )
        """)
        # Copy rows that exist; map source_id→channel_ref via sources table where possible
        conn.execute("""
            INSERT INTO duplicate_scans_v2
                (id, channel_ref, channel_title, status,
                 messages_scanned, total_messages, duplicate_groups, wasted_count,
                 created_at, completed_at, report_url, error_msg)
            SELECT
                ds.id,
                COALESCE(s.channel_ref, CAST(ds.source_id AS TEXT), '') AS channel_ref,
                COALESCE(s.title, s.name, '') AS channel_title,
                ds.status,
                ds.messages_scanned, ds.total_messages,
                ds.duplicate_groups, ds.wasted_count,
                ds.created_at, ds.completed_at, ds.report_url, ds.error_msg
            FROM duplicate_scans ds
            LEFT JOIN sources s ON s.id = ds.source_id
        """)
        conn.execute("DROP TABLE duplicate_scans")
        conn.execute("ALTER TABLE duplicate_scans_v2 RENAME TO duplicate_scans")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        logger.info("Migration: duplicate_scans rebuilt successfully")
    except Exception:
        logger.exception("Migration _migrate_duplicate_scans failed — skipping")
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_delete_scan_jobs(conn: sqlite3.Connection) -> None:
    """
    Rebuild delete_scan_jobs if it still has the old source_id NOT NULL column.
    That column was removed from the schema but never migrated in existing DBs.
    """
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(delete_scan_jobs)")}
        source_col = cols.get("source_id")
        if source_col is None or source_col[3] == 0:
            # source_id doesn't exist or is already nullable — no migration needed
            return

        logger.info("Migration: rebuilding delete_scan_jobs to remove source_id NOT NULL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS delete_scan_jobs_v2")
        conn.execute("""
            CREATE TABLE delete_scan_jobs_v2 (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id       INTEGER NOT NULL REFERENCES duplicate_scans(id),
                status        TEXT NOT NULL DEFAULT 'pending'
                              CHECK(status IN ('pending','running','done','failed')),
                deleted_count INTEGER DEFAULT 0,
                error_msg     TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at  TEXT
            )
        """)
        conn.execute("""
            INSERT INTO delete_scan_jobs_v2
                (id, scan_id, status, deleted_count, error_msg, created_at, completed_at)
            SELECT id, scan_id, status, deleted_count, error_msg, created_at, completed_at
            FROM delete_scan_jobs
        """)
        conn.execute("DROP TABLE delete_scan_jobs")
        conn.execute("ALTER TABLE delete_scan_jobs_v2 RENAME TO delete_scan_jobs")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        logger.info("Migration: delete_scan_jobs rebuilt successfully")
    except Exception:
        logger.exception("Migration _migrate_delete_scan_jobs failed — skipping")
        conn.execute("PRAGMA foreign_keys=ON")


def _backfill_last_scanned_message_id(conn: sqlite3.Connection) -> None:
    """
    For existing completed scans that have last_scanned_message_id=0 (or NULL),
    compute it from the actual scan items already stored in the DB.
    This is a one-time fix so old scans serve as a valid baseline for
    future incremental scans.
    """
    try:
        rows = conn.execute(
            """
            SELECT ds.id, MAX(dsi.message_id) AS max_id
            FROM duplicate_scans ds
            JOIN duplicate_scan_items dsi ON dsi.scan_id = ds.id
            WHERE ds.status = 'done'
              AND (ds.last_scanned_message_id IS NULL OR ds.last_scanned_message_id = 0)
            GROUP BY ds.id
            """
        ).fetchall()
        if rows:
            for row in rows:
                conn.execute(
                    "UPDATE duplicate_scans SET last_scanned_message_id=? WHERE id=?",
                    (row[1], row[0]),
                )
            conn.commit()
            logger.info(
                "Migration: backfilled last_scanned_message_id for %d completed scan(s)", len(rows)
            )
    except Exception:
        logger.exception("Migration _backfill_last_scanned_message_id failed — skipping")


def _backfill_channel_sync_state(conn: sqlite3.Connection) -> None:
    """
    Seed channel_sync_state from history that already lives in the database.

    copied_messages has no FK to jobs, so it outlives a deleted job — that is the
    record we mine here. For every full-history ('all'), single-destination job we
    still know about, the safe watermark is the highest source_message_id below the
    first message that *failed* to copy: only up to there was everything actually
    delivered. We take the MAX of those per-job safe points across the same pair,
    so the watermark never moves backwards, and never skips a message that still
    needs retrying.

    Restricted to mode='all' + single destination on purpose: an id_range or
    date_range job, or a random fan-out to several destinations, does not mean the
    whole 1..X range reached one specific channel, so its high id is not a safe
    resume point.

    Idempotent: re-running only ever raises a watermark to the same computed MAX.
    """
    try:
        rows = conn.execute(
            """
            -- Per job, stop the watermark just below its first failed message;
            -- then take the highest such safe point across all jobs on the pair.
            SELECT source_id, destination_id,
                   MAX(safe_id) AS watermark,
                   MAX(last_at) AS last_at
            FROM (
                SELECT j.source_id      AS source_id,
                       j.destination_id AS destination_id,
                       CASE
                         WHEN MIN(CASE WHEN cm.status = 'failed'
                                       THEN cm.source_message_id END) IS NOT NULL
                         THEN MIN(CASE WHEN cm.status = 'failed'
                                       THEN cm.source_message_id END) - 1
                         ELSE MAX(cm.source_message_id)
                       END              AS safe_id,
                       MAX(cm.processed_at) AS last_at
                FROM jobs j
                JOIN copied_messages cm ON cm.job_id = j.id
                WHERE j.mode = 'all'
                  AND (j.destination_ids IS NULL OR j.destination_ids = '')
                  -- Only history-complete jobs: a paused/failed sharded run can
                  -- have gaps below its MAX id, so its MAX is not a safe point.
                  AND (j.status = 'completed' OR COALESCE(j.backfill_done, 0) = 1)
                GROUP BY j.id, j.source_id, j.destination_id
            )
            WHERE safe_id > 0
            GROUP BY source_id, destination_id
            """
        ).fetchall()
        n = 0
        for row in rows:
            if not row["watermark"]:
                continue
            conn.execute(
                """
                INSERT INTO channel_sync_state
                    (source_id, destination_id, last_synced_id, last_synced_at, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_id, destination_id) DO UPDATE SET
                    last_synced_id = MAX(channel_sync_state.last_synced_id, excluded.last_synced_id),
                    last_synced_at = COALESCE(excluded.last_synced_at, channel_sync_state.last_synced_at),
                    updated_at     = datetime('now')
                """,
                (row["source_id"], row["destination_id"], row["watermark"], row["last_at"]),
            )
            n += 1
        if n:
            conn.commit()
            logger.info("Migration: backfilled channel_sync_state for %d channel pair(s)", n)
    except Exception:
        logger.exception("Migration _backfill_channel_sync_state failed — skipping")


def _migrate_copied_messages_remove_fk(conn: sqlite3.Connection) -> None:
    """Remove FK constraint from copied_messages so deleting a job keeps its stats."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='copied_messages'"
        ).fetchone()
        if not row or "REFERENCES jobs(id)" not in (row[0] or ""):
            return  # Already migrated or table doesn't exist yet

        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS copied_messages_v2")
        conn.execute("""
            CREATE TABLE copied_messages_v2 (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id            INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                dest_message_id   INTEGER,
                status            TEXT NOT NULL CHECK(status IN ('copied','skipped','failed')),
                skip_reason       TEXT,
                processed_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, source_message_id)
            )
        """)
        conn.execute("INSERT INTO copied_messages_v2 SELECT * FROM copied_messages")
        conn.execute("DROP TABLE copied_messages")
        conn.execute("ALTER TABLE copied_messages_v2 RENAME TO copied_messages")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_copied_msg_job ON copied_messages(job_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_copied_src_id ON copied_messages(job_id, source_message_id)")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        logger.info("Migration: removed FK from copied_messages")
    except Exception:
        logger.exception("Migration _migrate_copied_messages_remove_fk failed — skipping")
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_content_types_add_file(conn: sqlite3.Connection) -> None:
    """
    Introduce the 'file' content type without changing what existing jobs copy.

    Before this type existed, selecting all types meant "copy everything" — the
    content filter was skipped entirely, so documents came through. Now that
    'file' is selectable, those same rows would read as a strict subset and
    start dropping documents silently. Widen them to keep their old meaning.

    Guarded by user_version because it must run exactly once: after the upgrade
    a user may deliberately clear the 'file' checkbox, and re-running this would
    force it back on at every startup.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    # Both orderings exist in the wild: the wizard stores sorted(), while rows
    # backfilled by the old column default kept the literal 'text,image,video'.
    cur = conn.execute(
        "UPDATE jobs SET content_types = 'file,image,text,video' "
        "WHERE content_types IN ('text,image,video', 'image,text,video')"
    )
    conn.execute("PRAGMA user_version = 1")
    logger.info(
        "Migration: added 'file' content type to %d existing job(s)", cur.rowcount
    )


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    existing = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        logger.debug("Migration: added column %s.%s", table, column)


def _seed_missing_settings(conn: sqlite3.Connection, defaults: dict[str, str]) -> None:
    """Insert app_settings rows that don't exist yet (idempotent)."""
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)",
            (key, value),
        )
        logger.debug("Migration: seeded setting %s=%s (if missing)", key, value)


def close() -> None:
    """Close the database connection on graceful shutdown."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
        logger.debug("SQLite connection closed")
