"""
Runs the full TikTok→YouTube pipeline for a single channel.
Called by orchestrator.py — never runs all channels directly.
Returns a result dict so orchestrator can aggregate and notify.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from googleapiclient.errors import HttpError

from . import db
from .tiktok_downloader import (
    get_profile_videos, download_video, is_watermarked,
    is_short_video, cleanup_download, cleanup_stale_downloads,
)
from .youtube_uploader import get_authenticated_client, upload_video

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"


class TikTokUnreachableError(Exception):
    """Raised when the TikTok profile fetch fails after all retries."""


def run_channel(channel: Dict[str, Any], slot: int, dry_run: bool = False) -> Dict[str, Any]:
    """
    Full pipeline for one channel, one slot.
    Returns: {channel_id, slot, status, video_uploaded, youtube_url, error}
    Never raises — all exceptions are caught and returned in the result dict.
    """
    channel_id = channel["id"]
    result = {
        "channel_id": channel_id,
        "slot": slot,
        "status": "skipped",
        "video_uploaded": None,
        "youtube_url": None,
        "error": None,
    }

    run_id = db.start_run(channel_id, slot)

    try:
        # Guard: don't double-run a slot that already succeeded today
        if db.slot_already_ran(channel_id, slot):
            logger.info("[%s] Slot %d already ran successfully today — skipping", channel_id, slot)
            db.finish_run(run_id, "skipped")
            result["status"] = "skipped"
            return result

        # Sync channel to DB registry
        db.upsert_channel(channel)

        # Clean up any stale files from previous failed runs
        cleanup_stale_downloads(DOWNLOADS_DIR, max_age_days=7)

        # Pick one video to upload this slot
        video = _pick_next_video(channel, slot)
        if video is None:
            logger.info("[%s] No unposted videos available for slot %d", channel_id, slot)
            db.finish_run(run_id, "no_content")
            result["status"] = "no_content"
            return result

        logger.info("[%s] Selected video: %s | '%s'", channel_id, video["id"], video.get("title", ""))

        # Download
        local_file = _download_with_retry(channel, video, dry_run)
        if local_file is None:
            _handle_download_failure(channel, video, "Download failed after retries")
            db.finish_run(run_id, "failed", error_message="Download failed")
            result["status"] = "failed"
            result["error"] = "Download failed"
            return result

        # Determine Short vs regular
        short = is_short_video(
            duration=video.get("duration"),
            width=video.get("width"),
            height=video.get("height"),
            max_seconds=channel.get("shorts_max_seconds", 180),
        )

        # Upload
        youtube_id = _upload_video(channel, video, local_file, short, slot, dry_run)

        if youtube_id:
            if not dry_run:
                db.mark_uploaded(channel_id, video["id"], youtube_id)
                db.finish_run(run_id, "success", videos_uploaded=1)
            else:
                # Dry run: do NOT write to DB so real runs aren't blocked
                db.finish_run(run_id, "dry_run", videos_uploaded=0)
                logger.info("[%s] [DRY RUN] Would have uploaded: https://www.youtube.com/watch?v=%s", channel_id, youtube_id)
            cleanup_download(local_file)
            result["status"] = "success"
            result["video_uploaded"] = video.get("title", video["id"])
            result["youtube_url"] = f"https://www.youtube.com/watch?v={youtube_id}"
            if not dry_run:
                logger.info("[%s] ✓ Uploaded: %s", channel_id, result["youtube_url"])
        else:
            _handle_upload_failure(channel, video, "Upload returned no video ID")
            db.finish_run(run_id, "failed", error_message="Upload failed")
            result["status"] = "failed"
            result["error"] = "Upload returned no video ID"

    except TikTokUnreachableError as exc:
        error_msg = str(exc)
        logger.error("[%s] %s", channel_id, error_msg)
        db.finish_run(run_id, "failed", error_message=error_msg)
        result["status"] = "failed"
        result["error"] = error_msg

    except HttpError as exc:
        error_msg = f"YouTube API error: {exc.reason}"
        logger.error("[%s] %s", channel_id, error_msg)
        db.finish_run(run_id, "failed", error_message=error_msg)
        result["status"] = "failed"
        result["error"] = error_msg

    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"
        logger.exception("[%s] %s", channel_id, error_msg)
        db.finish_run(run_id, "failed", error_message=error_msg)
        result["status"] = "failed"
        result["error"] = error_msg

    return result


# ── Video selection ───────────────────────────────────────────────────────────

def _pick_next_video(channel: Dict[str, Any], slot: int) -> Optional[Dict[str, Any]]:
    """
    Priority order:
      1. Videos in pending_retry state that are due today (retries take priority)
      2. New unposted videos, sorted newest-first

    This ensures new content always floats to the top while failed videos
    get their retry window without blocking the queue indefinitely.

    Supports two optional channel config keys:
      min_upload_date       YYYY-MM-DD — ignore TikTok videos older than this date.
      min_backlog_for_slot1 int — slot 1 is skipped unless at least this many
                            unuploaded eligible videos exist. When the backlog drops
                            below this threshold the channel automatically falls back
                            to 1 upload/day (slot 2 only).
    """
    channel_id = channel["id"]
    today = date.today()

    # Resolve optional date filter → Unix timestamp
    min_ts = _parse_min_upload_date(channel.get("min_upload_date"))

    # Check for pending retries first (apply date filter if configured)
    retries = db.get_videos_for_retry(channel_id, today)
    if min_ts is not None:
        retries = [r for r in retries if (r.get("tiktok_timestamp") or 0) >= min_ts]
    if retries:
        logger.info("[%s] Found %d video(s) due for retry", channel_id, len(retries))
        return {
            "id": retries[0]["tiktok_video_id"],
            "url": retries[0]["tiktok_url"],
            "title": retries[0]["tiktok_title"],
            "timestamp": retries[0]["tiktok_timestamp"],
        }

    # Fetch fresh profile and find newest unposted
    videos = get_profile_videos(channel["tiktok_username"])
    if videos is None:
        # None = fetch failed (network error / TikTok blocked runner IP)
        # Raise so run_channel can distinguish this from "no new content"
        raise TikTokUnreachableError(
            f"TikTok profile @{channel['tiktok_username']} is unreachable after retries"
        )
    if not videos:
        return None

    # Apply upload-date filter (monetised channels, fresh content only)
    if min_ts is not None:
        before = len(videos)
        videos = [v for v in videos if (v.get("timestamp") or 0) >= min_ts]
        filtered = before - len(videos)
        if filtered:
            logger.info(
                "[%s] Filtered %d video(s) older than min_upload_date (%s)",
                channel_id, filtered, channel["min_upload_date"],
            )

    already_posted = db.get_posted_video_ids(channel_id)
    eligible = [v for v in videos if v["id"] not in already_posted]

    # Slot throttle: skip slot 1 when backlog is too small so the remaining
    # video(s) are preserved for slot 2.  Once the backlog runs out entirely
    # both slots return no_content and the channel idles until new TikTok
    # videos arrive.
    min_backlog = channel.get("min_backlog_for_slot1")
    if slot == 1 and min_backlog is not None and len(eligible) < int(min_backlog):
        logger.info(
            "[%s] Slot 1 throttled — %d eligible video(s) available, "
            "need %d (min_backlog_for_slot1). Reserving for slot 2.",
            channel_id, len(eligible), int(min_backlog),
        )
        return None

    for video in eligible:
        # Record it in DB so we can track it even if download fails
        db.record_video_seen(channel_id, video)
        return video

    return None


def _parse_min_upload_date(min_date_str: Optional[str]) -> Optional[int]:
    """Convert 'YYYY-MM-DD' string to a Unix timestamp int, or None if not set."""
    if not min_date_str:
        return None
    try:
        return int(datetime.strptime(min_date_str, "%Y-%m-%d").timestamp())
    except ValueError:
        logger.warning(
            "Invalid min_upload_date %r — expected YYYY-MM-DD format, ignoring filter",
            min_date_str,
        )
        return None


# ── Download ──────────────────────────────────────────────────────────────────

def _download_with_retry(channel: Dict[str, Any], video: Dict[str, Any],
                         dry_run: bool) -> Optional[Path]:
    """Attempt download once (yt-dlp has its own internal retries)."""
    if dry_run:
        logger.info("[DRY RUN] Skipping download for %s", video["id"])
        return DOWNLOADS_DIR / f"{video['id']}.mp4"   # fake path

    channel_dir = DOWNLOADS_DIR / channel["id"]
    return download_video(
        video_url=video["url"],
        video_id=video["id"],
        output_dir=channel_dir,
    )


def _handle_download_failure(channel: Dict[str, Any], video: Dict[str, Any],
                              error_msg: str) -> None:
    today = date.today()
    db.mark_retry(
        channel_id=channel["id"],
        tiktok_video_id=video["id"],
        error_message=error_msg,
        next_retry_date=today + timedelta(days=1),
        max_retries=channel.get("max_retry_days", 3),
    )
    logger.warning("[%s] Video %s queued for retry tomorrow: %s",
                   channel["id"], video["id"], error_msg)


# ── Upload ────────────────────────────────────────────────────────────────────

def _resolve_title(channel: Dict[str, Any], video: Dict[str, Any]) -> str:
    """
    Return the best available title for a YouTube upload.
    If the TikTok title is missing or too short (<5 chars), fall back to:
        "{youtube_channel_name} — {Month DD, YYYY}"
    where the date is the TikTok video's upload date (or today if unknown).
    """
    title = (video.get("title") or "").strip()
    if len(title) >= 5:
        return title
    # Build fallback: channel name + video date
    channel_name = channel.get("youtube_channel_name") or channel.get("id", "")
    ts = video.get("timestamp")
    if ts:
        video_date = date.fromtimestamp(ts).strftime("%B %d, %Y")
    else:
        video_date = date.today().strftime("%B %d, %Y")
    fallback = f"{channel_name} — {video_date}"
    logger.info(
        "[%s] No title for video %s — using fallback: '%s'",
        channel["id"], video["id"], fallback,
    )
    return fallback


def _upload_video(channel: Dict[str, Any], video: Dict[str, Any],
                  local_file: Path, is_short: bool, slot: int,
                  dry_run: bool) -> Optional[str]:
    youtube = get_authenticated_client(
        credentials_file=channel["google_credentials_file"],
        token_file=channel["oauth_token_file"],
    )
    return upload_video(
        youtube_client=youtube,
        video_path=local_file,
        title=_resolve_title(channel, video),
        description=video.get("description") or "",
        tags=list(channel.get("default_tags") or []),
        category_id=str(channel.get("youtube_category_id", "22")),
        is_short=is_short,
        description_footer=channel.get("description_footer", ""),
        publish_at=_get_publish_at(channel, slot),
        dry_run=dry_run,
    )


def _get_publish_at(channel: Dict[str, Any], slot: int) -> Optional[str]:
    """
    Calculate the UTC publish time for this slot using slot_publish_times_utc config.
    Returns an ISO 8601 UTC string (e.g. '2026-05-26T15:00:00Z').
    Video uploads as Private and YouTube makes it Public at exactly that time,
    regardless of when GitHub Actions actually ran the workflow.

    GitHub Actions cron delays can be anywhere from minutes to 10+ hours.
    To handle this gracefully:
      - If target time is still in the future: schedule for today
      - If target time already passed today: schedule for TOMORROW at the same time
        (never publish immediately — always respect the configured schedule)
    """
    times = channel.get("slot_publish_times_utc") or {}
    # Accept both int and string keys from YAML
    time_str = times.get(slot) or times.get(str(slot))
    if not time_str:
        return None

    try:
        h, m = map(int, str(time_str).split(":"))
    except (ValueError, AttributeError):
        logger.warning("[%s] Invalid slot_publish_times_utc value: %r", channel["id"], time_str)
        return None

    now = datetime.now(timezone.utc)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta_seconds = (target - now).total_seconds()

    if delta_seconds < 0:
        # Target already passed today (GitHub cron ran late) — publish immediately.
        # Missing a day entirely is worse than publishing at the wrong time.
        logger.info(
            "[%s] Slot %d: %02d:%02dZ already passed today (GitHub cron delay) — "
            "publishing immediately",
            channel["id"], slot, h, m,
        )
        return None

    logger.info(
        "[%s] Slot %d will publish at %s (%d min from now)",
        channel["id"], slot, target.strftime("%Y-%m-%dT%H:%M:%SZ"), int(delta_seconds / 60),
    )
    publish_at = target.strftime("%Y-%m-%dT%H:%M:%SZ")
    return publish_at


def _handle_upload_failure(channel: Dict[str, Any], video: Dict[str, Any],
                            error_msg: str) -> None:
    today = date.today()
    db.mark_retry(
        channel_id=channel["id"],
        tiktok_video_id=video["id"],
        error_message=error_msg,
        next_retry_date=today + timedelta(days=1),
        max_retries=channel.get("max_retry_days", 3),
    )
