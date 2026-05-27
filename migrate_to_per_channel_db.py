"""
Migration script: copy rows from shared automation.db into per-channel DBs.

Run once locally:
    python migrate_to_per_channel_db.py

For each channel found in the channels table, creates data/channel_N.db and copies
all related rows from channels, posted_videos, and runs.
"""

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
SRC_DB = PROJECT_ROOT / "data" / "automation.db"


def get_channel_ids(src_conn: sqlite3.Connection):
    rows = src_conn.execute("SELECT id FROM channels").fetchall()
    return [row[0] for row in rows]


def create_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id                   TEXT PRIMARY KEY,
            tiktok_username      TEXT NOT NULL,
            youtube_channel_name TEXT NOT NULL,
            enabled              INTEGER DEFAULT 1,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at           TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS posted_videos (
            channel_id          TEXT NOT NULL,
            tiktok_video_id     TEXT NOT NULL,
            tiktok_url          TEXT,
            tiktok_title        TEXT,
            tiktok_timestamp    INTEGER,
            youtube_video_id    TEXT,
            posted_at           TEXT,
            status              TEXT DEFAULT 'pending',
            retry_count         INTEGER DEFAULT 0,
            next_retry_date     TEXT,
            error_message       TEXT,
            created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (channel_id, tiktok_video_id)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id      TEXT NOT NULL,
            run_date        TEXT NOT NULL,
            slot            INTEGER NOT NULL,
            status          TEXT,
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


def migrate_channel(src_conn: sqlite3.Connection, channel_id: str):
    dest_path = PROJECT_ROOT / "data" / f"{channel_id}.db"
    print(f"Migrating {channel_id} -> {dest_path}")

    dest_conn = sqlite3.connect(str(dest_path))

    with dest_conn:
        create_schema(dest_conn)

        # channels
        row = src_conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if row:
            dest_conn.execute("""
                INSERT OR REPLACE INTO channels
                    (id, tiktok_username, youtube_channel_name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, tuple(row))

        # posted_videos
        rows = src_conn.execute(
            "SELECT * FROM posted_videos WHERE channel_id = ?", (channel_id,)
        ).fetchall()
        for r in rows:
            dest_conn.execute("""
                INSERT OR REPLACE INTO posted_videos
                    (channel_id, tiktok_video_id, tiktok_url, tiktok_title,
                     tiktok_timestamp, youtube_video_id, posted_at, status, retry_count,
                     next_retry_date, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(r))
        print(f"  Copied {len(rows)} posted_videos rows")

        # runs
        run_rows = src_conn.execute(
            "SELECT * FROM runs WHERE channel_id = ?", (channel_id,)
        ).fetchall()
        for r in run_rows:
            dest_conn.execute("""
                INSERT OR REPLACE INTO runs
                    (id, channel_id, run_date, slot, status, videos_uploaded,
                     error_message, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(r))
        print(f"  Copied {len(run_rows)} runs rows")

    dest_conn.close()


if __name__ == "__main__":
    if not SRC_DB.exists():
        print(f"Source DB not found: {SRC_DB}")
        raise SystemExit(1)

    src_conn = sqlite3.connect(str(SRC_DB))
    channel_ids = get_channel_ids(src_conn)
    print(f"Found channels: {channel_ids}")

    for channel_id in channel_ids:
        migrate_channel(src_conn, channel_id)

    src_conn.close()
    print("Migration complete.")
