"""
SQLite wrapper. One database, multi-channel schema.
All state tracking lives here — nothing is derived from filenames or folders.
"""

import sqlite3
import logging
from pathlib import Path
from datetime import date, datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

import os
import glob as _glob

PROJECT_ROOT = Path(__file__).parent.parent

def _get_db_path() -> Path:
    page_id = os.environ.get("DB_PAGE_ID")
    if page_id:
        return PROJECT_ROOT / "data" / f"{page_id}.db"
    return PROJECT_ROOT / "data" / "automation.db"

DB_PATH = _get_db_path()


def get_connection() -> sqlite3.Connection:
    db_path = _get_db_path()
    db_path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call on every startup."""
    conn = get_connection()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id                  TEXT PRIMARY KEY,
                tiktok_username     TEXT NOT NULL,
                youtube_channel_name TEXT NOT NULL,
                enabled             INTEGER DEFAULT 1,
                created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS posted_videos (
                channel_id          TEXT NOT NULL,
                tiktok_video_id     TEXT NOT NULL,
                tiktok_url          TEXT,
                tiktok_title        TEXT,
                tiktok_timestamp    INTEGER,   -- Unix epoch of TikTok post date
                youtube_video_id    TEXT,
                posted_at           TEXT,
                status              TEXT DEFAULT 'pending',
                    -- pending | uploaded | pending_retry | failed_permanent | skipped
                retry_count         INTEGER DEFAULT 0,
                next_retry_date     TEXT,      -- ISO date, NULL if not in retry
                error_message       TEXT,
                created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id, tiktok_video_id)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id      TEXT NOT NULL,
                run_date        TEXT NOT NULL,   -- ISO date YYYY-MM-DD
                slot            INTEGER NOT NULL, -- 1 or 2
                status          TEXT,            -- success | failed | skipped | no_content
                videos_uploaded INTEGER DEFAULT 0,
                error_message   TEXT,
                started_at      TEXT,
                completed_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_posted_videos_channel_status
                ON posted_videos (channel_id, status);

            CREATE INDEX IF NOT EXISTS idx_runs_channel_date
                ON runs (channel_id, run_date);
        """)
    conn.close()
    logger.debug("Database initialised at %s", _get_db_path())


# ── Channel registry ──────────────────────────────────────────────────────────

def upsert_channel(channel_cfg: Dict[str, Any]) -> None:
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO channels (id, tiktok_username, youtube_channel_name, enabled, updated_at)
            VALUES (:id, :tiktok_username, :youtube_channel_name, :enabled, :now)
            ON CONFLICT(id) DO UPDATE SET
                tiktok_username      = excluded.tiktok_username,
                youtube_channel_name = excluded.youtube_channel_name,
                enabled              = excluded.enabled,
                updated_at           = excluded.updated_at
        """, {
            "id": channel_cfg["id"],
            "tiktok_username": channel_cfg["tiktok_username"],
            "youtube_channel_name": channel_cfg["youtube_channel_name"],
            "enabled": 1 if channel_cfg.get("enabled", True) else 0,
            "now": datetime.utcnow().isoformat(),
        })
    conn.close()


# ── Video state ───────────────────────────────────────────────────────────────

def get_posted_video_ids(channel_id: str) -> set:
    """Return set of tiktok_video_id values that are already uploaded or permanently failed."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT tiktok_video_id FROM posted_videos
        WHERE channel_id = ? AND status IN ('uploaded', 'failed_permanent', 'skipped')
    """, (channel_id,)).fetchall()
    conn.close()
    return {row["tiktok_video_id"] for row in rows}


def get_videos_for_retry(channel_id: str, today: date) -> List[Dict]:
    """Return videos in pending_retry status whose next_retry_date is today or past."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM posted_videos
        WHERE channel_id = ?
          AND status = 'pending_retry'
          AND (next_retry_date IS NULL OR next_retry_date <= ?)
        ORDER BY tiktok_timestamp DESC
    """, (channel_id, today.isoformat())).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def record_video_seen(channel_id: str, video: Dict[str, Any]) -> None:
    """Insert a new video into the DB with status=pending if not already tracked."""
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT OR IGNORE INTO posted_videos
                (channel_id, tiktok_video_id, tiktok_url, tiktok_title, tiktok_timestamp, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (
            channel_id,
            video["id"],
            video.get("url"),
            video.get("title"),
            video.get("timestamp"),
        ))
    conn.close()


def mark_uploaded(channel_id: str, tiktok_video_id: str, youtube_video_id: str) -> None:
    conn = get_connection()
    with conn:
        conn.execute("""
            UPDATE posted_videos
            SET status = 'uploaded', youtube_video_id = ?, posted_at = ?,
                retry_count = 0, next_retry_date = NULL, error_message = NULL,
                updated_at = ?
            WHERE channel_id = ? AND tiktok_video_id = ?
        """, (
            youtube_video_id,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
            channel_id,
            tiktok_video_id,
        ))
    conn.close()


def mark_retry(channel_id: str, tiktok_video_id: str,
               error_message: str, next_retry_date: date, max_retries: int) -> None:
    """Increment retry counter. If max exceeded, mark as failed_permanent."""
    conn = get_connection()
    row = conn.execute("""
        SELECT retry_count FROM posted_videos
        WHERE channel_id = ? AND tiktok_video_id = ?
    """, (channel_id, tiktok_video_id)).fetchone()

    current_count = (row["retry_count"] if row else 0) + 1
    now = datetime.utcnow().isoformat()

    with conn:
        if current_count > max_retries:
            conn.execute("""
                UPDATE posted_videos
                SET status = 'failed_permanent', retry_count = ?,
                    error_message = ?, updated_at = ?
                WHERE channel_id = ? AND tiktok_video_id = ?
            """, (current_count, error_message, now, channel_id, tiktok_video_id))
            logger.warning(
                "Video %s on channel %s permanently failed after %d retries",
                tiktok_video_id, channel_id, current_count
            )
        else:
            conn.execute("""
                UPDATE posted_videos
                SET status = 'pending_retry', retry_count = ?,
                    next_retry_date = ?, error_message = ?, updated_at = ?
                WHERE channel_id = ? AND tiktok_video_id = ?
            """, (current_count, next_retry_date.isoformat(), error_message, now,
                  channel_id, tiktok_video_id))
    conn.close()


# ── Run tracking ──────────────────────────────────────────────────────────────

def start_run(channel_id: str, slot: int) -> int:
    """Insert a run record, return its ID."""
    conn = get_connection()
    with conn:
        cursor = conn.execute("""
            INSERT INTO runs (channel_id, run_date, slot, status, started_at)
            VALUES (?, ?, ?, 'running', ?)
        """, (channel_id, date.today().isoformat(), slot, datetime.utcnow().isoformat()))
        run_id = cursor.lastrowid
    conn.close()
    return run_id


def finish_run(run_id: int, status: str,
               videos_uploaded: int = 0, error_message: Optional[str] = None) -> None:
    conn = get_connection()
    with conn:
        conn.execute("""
            UPDATE runs
            SET status = ?, videos_uploaded = ?, error_message = ?, completed_at = ?
            WHERE id = ?
        """, (status, videos_uploaded, error_message, datetime.utcnow().isoformat(), run_id))
    conn.close()


def count_uploads_today(channel_id: str) -> int:
    """How many videos have been successfully uploaded for this channel today."""
    conn = get_connection()
    row = conn.execute("""
        SELECT COALESCE(SUM(videos_uploaded), 0) AS total
        FROM runs
        WHERE channel_id = ? AND run_date = ? AND status = 'success'
    """, (channel_id, date.today().isoformat())).fetchone()
    conn.close()
    return row["total"] if row else 0


def get_todays_run_summary() -> List[Dict]:
    """
    Return exactly one row per channel per slot for today's runs.

    Two bugs fixed vs. the naive query:
      1. Deduplication — if a slot ran more than once today (e.g. a local run
         followed by the scheduled GitHub Actions run), only the most recent
         run (highest id) is returned.
      2. Per-slot video URL — the subquery now matches posted_at to the
         specific run's started_at/completed_at window so slot 1 and slot 2
         never share the same video URL in the summary.

    In summary mode (DB_PAGE_ID not set), globs all channel_*.db files and
    combines results from each.
    """
    page_id = os.environ.get("DB_PAGE_ID")
    if page_id:
        db_paths = [_get_db_path()]
    else:
        db_paths = [Path(p) for p in _glob.glob(str(PROJECT_ROOT / "data" / "channel_*.db"))]
        if not db_paths:
            db_paths = [PROJECT_ROOT / "data" / "automation.db"]

    query = """
        SELECT r.channel_id, r.slot, r.status, r.videos_uploaded, r.error_message,
               (
                   SELECT p.youtube_video_id
                   FROM posted_videos p
                   WHERE p.channel_id = r.channel_id
                     AND p.status = 'uploaded'
                     AND p.youtube_video_id NOT LIKE '%DRY_RUN%'
                     AND p.posted_at >= r.started_at
                     AND p.posted_at <= COALESCE(r.completed_at, datetime('now'))
                   ORDER BY p.posted_at DESC
                   LIMIT 1
               ) AS youtube_video_id,
               (
                   SELECT p.tiktok_title
                   FROM posted_videos p
                   WHERE p.channel_id = r.channel_id
                     AND p.status = 'uploaded'
                     AND p.youtube_video_id NOT LIKE '%DRY_RUN%'
                     AND p.posted_at >= r.started_at
                     AND p.posted_at <= COALESCE(r.completed_at, datetime('now'))
                   ORDER BY p.posted_at DESC
                   LIMIT 1
               ) AS tiktok_title
        FROM runs r
        WHERE r.run_date = date('now')
          AND r.status != 'running'
          AND r.id = (
              SELECT COALESCE(
                  (SELECT MAX(r2.id) FROM runs r2
                   WHERE r2.channel_id = r.channel_id AND r2.slot = r.slot
                     AND r2.run_date = r.run_date AND r2.status = 'success'),
                  (SELECT MAX(r2.id) FROM runs r2
                   WHERE r2.channel_id = r.channel_id AND r2.slot = r.slot
                     AND r2.run_date = r.run_date AND r2.status != 'running')
              )
          )
        ORDER BY r.channel_id, r.slot
    """

    all_rows: List[Dict] = []
    for db_path in db_paths:
        if not db_path.exists():
            continue
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()
        conn.close()
        all_rows.extend([dict(row) for row in rows])
    return all_rows


def slot_already_ran(channel_id: str, slot: int) -> bool:
    """True if this slot already completed successfully today (prevents double-runs)."""
    conn = get_connection()
    row = conn.execute("""
        SELECT 1 FROM runs
        WHERE channel_id = ? AND run_date = ? AND slot = ? AND status = 'success'
        LIMIT 1
    """, (channel_id, date.today().isoformat(), slot)).fetchone()
    conn.close()
    return row is not None
