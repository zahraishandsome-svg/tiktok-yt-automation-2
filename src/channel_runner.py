"""
Runs the full TikTok→YouTube pipeline for a single channel.
Called by orchestrator.py — never runs all channels directly.
Returns a result dict so orchestrator can aggregate and notify.

Upload modes (set via upload_mode in channels.yaml):
  short_only   (default) — original 9:16 uploaded as a YouTube Short (or regular if long/horizontal).
                            Zero behaviour change for existing channels.
  dual         — same TikTok source → Short upload + 4:3 blurred-fill longform upload.
                 Both happen in a single slot run. Counts as 2 videos_uploaded.
  longform_only — source converted to 4:3 blurred-fill, uploaded as a regular (non-Short) video.
  split        — slot 1 picks a fresh TikTok and uploads it as a Short (9:16).
                 slot 2 picks a DIFFERENT TikTok and uploads it as a 4:3 longform.
                 Cross-format exclusion: a video uploaded in either format is never re-used.

Horizontal source videos (width >= height) are NEVER converted:
  - short_only:    upload as regular (non-Short) video — unchanged
  - longform_only: upload as regular video (no conversion needed)
  - dual:          upload once as regular video; both format_type rows marked done
"""

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from googleapiclient.errors import HttpError

from . import db
from .tiktok_downloader import (
    get_profile_videos, download_video, is_watermarked,
    is_short_video, cleanup_download, cleanup_stale_downloads, _PROFILE_BATCH,
)
from .youtube_uploader import get_authenticated_client, upload_video
from .video_converter import convert_to_4_3_blurred, trim_video, is_ffmpeg_available, is_vertical as _file_is_vertical

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"

VALID_UPLOAD_MODES = {"short_only", "dual", "longform_only", "split", "trim_dual"}


class TikTokUnreachableError(Exception):
    """Raised when the TikTok profile fetch fails after all retries."""


def run_channel(channel: Dict[str, Any], slot: int, dry_run: bool = False) -> Dict[str, Any]:
    """
    Full pipeline for one channel, one slot.
    Returns: {channel_id, slot, status, video_uploaded, youtube_url,
              youtube_url_longform, error}
    Never raises — all exceptions are caught and returned in the result dict.
    """
    channel_id = channel["id"]
    upload_mode = channel.get("upload_mode", "short_only")

    result = {
        "channel_id": channel_id,
        "slot": slot,
        "status": "skipped",
        "video_uploaded": None,
        "youtube_url": None,
        "youtube_url_longform": None,
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

        # Pick one video to upload this slot.
        # trim_dual slot 2: may not need a fresh video (uses today's longform from DB).
        # Still try to pick one in case slot 1 failed and self-heal is needed.
        if upload_mode == "trim_dual" and slot == 2:
            today_longform = db.get_todays_longform_video(channel_id)
            if today_longform is not None:
                video = None  # slot 2 will use today's longform — no fresh pick needed
            else:
                # Slot 1 failed — pick a fresh video for self-heal
                video = _pick_next_video(channel, 1, upload_mode)
                if video is None:
                    logger.info("[%s] No unposted videos available for slot 2 self-heal", channel_id)
                    db.finish_run(run_id, "no_content")
                    result["status"] = "no_content"
                    return result
        else:
            video = _pick_next_video(channel, slot, upload_mode)
            if video is None:
                logger.info("[%s] No unposted videos available for slot %d", channel_id, slot)
                db.finish_run(run_id, "no_content")
                result["status"] = "no_content"
                return result

        logger.info("[%s] Selected video: %s | '%s'", channel_id, video["id"], video.get("title", ""))

        # Dispatch to mode-specific runner.
        # NOTE: vertical orientation is determined AFTER download (from the actual
        # file via ffprobe) because extract_flat metadata often omits width/height.
        if upload_mode == "dual":
            _run_dual(channel, video, slot, run_id, dry_run, result)
        elif upload_mode == "longform_only":
            _run_longform_only(channel, video, slot, run_id, dry_run, result)
        elif upload_mode == "split":
            # Slot 1 → Short (9:16); Slot 2 → Longform (4:3 blurred-fill).
            # Each slot picks a completely different TikTok video.
            if slot == 2:
                _run_longform_only(channel, video, slot, run_id, dry_run, result)
            else:
                _run_short_only(channel, video, slot, run_id, dry_run, result)
        elif upload_mode == "trim_dual":
            # Slot 1 → upload original 3+ min video as longform.
            # Slot 2 → trim same video to 2:59, upload as Short.
            # Slot 2 is self-healing: if slot 1 failed, it runs slot 1 first then trims.
            _run_trim_dual(channel, video, slot, run_id, dry_run, result)
        else:
            # short_only (default) — original behaviour
            _run_short_only(channel, video, slot, run_id, dry_run, result)

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


# ── Mode-specific runners ─────────────────────────────────────────────────────

def _run_short_only(channel, video, slot, run_id, dry_run, result):
    """Original behaviour — unchanged."""
    channel_id = channel["id"]

    local_file = _download_with_retry(channel, video, dry_run)
    if local_file is None:
        _handle_download_failure(channel, video, "Download failed after retries",
                                 format_type="short")
        db.finish_run(run_id, "failed", error_message="Download failed")
        result["status"] = "failed"
        result["error"] = "Download failed"
        return

    short = is_short_video(
        duration=video.get("duration"),
        width=video.get("width"),
        height=video.get("height"),
        max_seconds=channel.get("shorts_max_seconds", 180),
    )

    youtube_id = _upload_video(channel, video, local_file, short, slot, dry_run)

    if youtube_id:
        if not dry_run:
            db.mark_uploaded(channel_id, video["id"], youtube_id, format_type="short",
                             tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                             tiktok_timestamp=video.get("timestamp"))
            db.finish_run(run_id, "success", videos_uploaded=1)
        else:
            db.finish_run(run_id, "dry_run", videos_uploaded=0)
            logger.info("[%s] [DRY RUN] Would have uploaded: https://www.youtube.com/watch?v=%s",
                        channel_id, youtube_id)
        cleanup_download(local_file)
        result["status"] = "success"
        result["video_uploaded"] = video.get("title", video["id"])
        result["youtube_url"] = f"https://www.youtube.com/watch?v={youtube_id}"
        if not dry_run:
            logger.info("[%s] Uploaded: %s", channel_id, result["youtube_url"])
    else:
        _handle_upload_failure(channel, video, "Upload returned no video ID",
                                format_type="short")
        db.finish_run(run_id, "failed", error_message="Upload failed")
        result["status"] = "failed"
        result["error"] = "Upload returned no video ID"


def _run_longform_only(channel, video, slot, run_id, dry_run, result):
    """
    Upload ONLY the 4:3 blurred-fill version.
    Horizontal videos are uploaded as-is (no conversion needed).
    Orientation is probed from the downloaded file (not from TikTok metadata,
    which is often missing width/height in extract_flat mode).
    """
    channel_id = channel["id"]

    local_file = _download_with_retry(channel, video, dry_run)
    if local_file is None:
        _handle_download_failure(channel, video, "Download failed after retries",
                                 format_type="longform")
        db.finish_run(run_id, "failed", error_message="Download failed")
        result["status"] = "failed"
        result["error"] = "Download failed"
        return

    # Probe orientation from the actual file (reliable even when metadata is absent)
    vertical = _file_is_vertical(local_file) if not dry_run else (
        (video.get("height") or 0) > (video.get("width") or 0)
    )
    logger.info("[%s] Video orientation: %s", channel_id,
                "vertical (will convert to 4:3)" if vertical else "horizontal (upload as-is)")

    # Convert vertical → 4:3 blurred-fill; horizontal passes through unchanged
    upload_file = local_file
    converted_file = None
    if vertical and not dry_run:
        converted_file = _convert_video(channel, video, local_file, format_type="longform")
        if converted_file is None:
            db.finish_run(run_id, "failed", error_message="Conversion failed")
            result["status"] = "failed"
            result["error"] = "4:3 conversion failed"
            cleanup_download(local_file)
            return
        upload_file = converted_file

    # Longform uploads are NEVER Shorts (they are horizontal 4:3)
    title = _resolve_longform_title(channel, video)
    youtube_id = _upload_video(
        channel, video, upload_file, is_short=False, slot=slot, dry_run=dry_run,
        title_override=title,
    )

    # Clean up both files
    cleanup_download(local_file)
    if converted_file and converted_file != local_file:
        cleanup_download(converted_file)

    if youtube_id:
        if not dry_run:
            db.mark_uploaded(channel_id, video["id"], youtube_id, format_type="longform",
                             tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                             tiktok_timestamp=video.get("timestamp"))
            db.finish_run(run_id, "success", videos_uploaded=1)
        else:
            db.finish_run(run_id, "dry_run", videos_uploaded=0)
            logger.info("[%s] [DRY RUN] Would have uploaded longform: https://www.youtube.com/watch?v=%s",
                        channel_id, youtube_id)
        result["status"] = "success"
        result["video_uploaded"] = video.get("title", video["id"])
        result["youtube_url"] = f"https://www.youtube.com/watch?v={youtube_id}"
        if not dry_run:
            logger.info("[%s] Longform uploaded: %s", channel_id, result["youtube_url"])
    else:
        _handle_upload_failure(channel, video, "Upload returned no video ID",
                                format_type="longform")
        db.finish_run(run_id, "failed", error_message="Upload failed")
        result["status"] = "failed"
        result["error"] = "Upload returned no video ID"


def _run_dual(channel, video, slot, run_id, dry_run, result):
    """
    Upload BOTH the original Short (9:16) AND the 4:3 blurred-fill longform.
    Horizontal videos are uploaded once and marked done for both formats.
    Orientation is probed from the downloaded file (reliable vs. extract_flat metadata).
    """
    channel_id = channel["id"]

    # Check which formats are already done (handles partial-failure retries)
    short_status = db.get_format_status(channel_id, video["id"], "short")
    longform_status = db.get_format_status(channel_id, video["id"], "longform")

    short_done = short_status in ("uploaded", "failed_permanent", "skipped")
    longform_done = longform_status in ("uploaded", "failed_permanent", "skipped")

    if short_done and longform_done:
        logger.info("[%s] Both formats already done for %s — skipping",
                    channel_id, video["id"])
        db.finish_run(run_id, "skipped")
        result["status"] = "skipped"
        return

    # Download once (shared between both uploads)
    local_file = _download_with_retry(channel, video, dry_run)
    if local_file is None:
        if not short_done:
            _handle_download_failure(channel, video, "Download failed after retries",
                                     format_type="short")
        if not longform_done:
            _handle_download_failure(channel, video, "Download failed after retries",
                                     format_type="longform")
        db.finish_run(run_id, "failed", error_message="Download failed")
        result["status"] = "failed"
        result["error"] = "Download failed"
        return

    # Probe orientation from the actual file
    vertical = _file_is_vertical(local_file) if not dry_run else (
        (video.get("height") or 0) > (video.get("width") or 0)
    )
    logger.info("[%s] Video orientation: %s", channel_id,
                "vertical" if vertical else "horizontal (upload once, mark both formats done)")

    videos_uploaded = 0
    short_yt_id = None
    longform_yt_id = None

    # ── Short upload ───────────────────────────────────────────────────────────
    if not short_done:
        if vertical:
            short_is_short = is_short_video(
                duration=video.get("duration"),
                width=video.get("width"),
                height=video.get("height"),
                max_seconds=channel.get("shorts_max_seconds", 180),
            )
        else:
            short_is_short = False  # horizontal → regular video

        short_yt_id = _upload_video(channel, video, local_file,
                                     is_short=short_is_short, slot=slot, dry_run=dry_run)
        if short_yt_id:
            if not dry_run:
                db.mark_uploaded(channel_id, video["id"], short_yt_id, format_type="short",
                                 tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                                 tiktok_timestamp=video.get("timestamp"))
            videos_uploaded += 1
            result["youtube_url"] = f"https://www.youtube.com/watch?v={short_yt_id}"
            logger.info("[%s] Short uploaded: %s", channel_id, result["youtube_url"])
        else:
            _handle_upload_failure(channel, video, "Short upload returned no video ID",
                                    format_type="short")
            logger.warning("[%s] Short upload failed for %s", channel_id, video["id"])

    # ── Longform upload ────────────────────────────────────────────────────────
    if not longform_done:
        converted_file = None
        upload_file = local_file

        if vertical and not dry_run:
            converted_file = _convert_video(channel, video, local_file, format_type="longform")
            if converted_file is None:
                # Conversion failed — mark longform for retry, continue
                _handle_upload_failure(channel, video, "4:3 conversion failed",
                                        format_type="longform")
                logger.warning("[%s] Longform conversion failed for %s", channel_id, video["id"])
            else:
                upload_file = converted_file

        if converted_file is not None or not vertical or dry_run:
            # Horizontal videos: upload original file directly (no conversion)
            longform_title = _resolve_longform_title(channel, video)
            longform_yt_id = _upload_video(
                channel, video, upload_file, is_short=False, slot=slot, dry_run=dry_run,
                title_override=longform_title,
            )
            if longform_yt_id:
                if not dry_run:
                    db.mark_uploaded(channel_id, video["id"], longform_yt_id, format_type="longform",
                                     tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                                     tiktok_timestamp=video.get("timestamp"))
                    if not vertical:
                        # Horizontal: also mark short as done (same upload serves both)
                        db.mark_uploaded(channel_id, video["id"], longform_yt_id, format_type="short",
                                         tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                                         tiktok_timestamp=video.get("timestamp"))
                videos_uploaded += 1
                result["youtube_url_longform"] = f"https://www.youtube.com/watch?v={longform_yt_id}"
                logger.info("[%s] Longform uploaded: %s", channel_id, result["youtube_url_longform"])
            else:
                _handle_upload_failure(channel, video, "Longform upload returned no video ID",
                                        format_type="longform")
                logger.warning("[%s] Longform upload failed for %s", channel_id, video["id"])

            if converted_file and converted_file != local_file:
                cleanup_download(converted_file)

    cleanup_download(local_file)

    # ── Result ─────────────────────────────────────────────────────────────────
    if videos_uploaded > 0:
        if not dry_run:
            db.finish_run(run_id, "success", videos_uploaded=videos_uploaded)
        else:
            db.finish_run(run_id, "dry_run", videos_uploaded=0)
        result["status"] = "success"
        result["video_uploaded"] = video.get("title", video["id"])
    else:
        db.finish_run(run_id, "failed", error_message="All uploads failed")
        result["status"] = "failed"
        result["error"] = "All uploads failed"


def _run_trim_dual(channel, video, slot, run_id, dry_run, result):
    """
    trim_dual mode — for channels with 3+ min TikTok videos:
      Slot 1: upload original video as longform (3+ min → not a Short automatically).
      Slot 2: download same video, trim to 2:59, upload as Short.
              Self-healing: if slot 1 failed, slot 2 runs slot 1 first then trims.
    """
    channel_id = channel["id"]
    videos_uploaded = 0

    if slot == 1:
        # Upload original as longform (no conversion — 3+ min vertical uploads as regular video)
        local_file = _download_with_retry(channel, video, dry_run)
        if local_file is None:
            _handle_download_failure(channel, video, "Download failed after retries", format_type="longform")
            db.finish_run(run_id, "failed", error_message="Download failed")
            result["status"] = "failed"
            result["error"] = "Download failed"
            return

        youtube_id = _upload_video(channel, video, local_file, is_short=False, slot=slot, dry_run=dry_run)
        cleanup_download(local_file)

        if youtube_id:
            if not dry_run:
                db.mark_uploaded(channel_id, video["id"], youtube_id, format_type="longform",
                                 tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                                 tiktok_timestamp=video.get("timestamp"))
                db.finish_run(run_id, "success", videos_uploaded=1)
            else:
                db.finish_run(run_id, "dry_run", videos_uploaded=0)
            result["status"] = "success"
            result["video_uploaded"] = video.get("title", video["id"])
            result["youtube_url"] = f"https://www.youtube.com/watch?v={youtube_id}"
            logger.info("[%s] Longform uploaded: %s", channel_id, result["youtube_url"])
        else:
            _handle_upload_failure(channel, video, "Upload returned no video ID", format_type="longform")
            db.finish_run(run_id, "failed", error_message="Upload failed")
            result["status"] = "failed"
            result["error"] = "Longform upload failed"

    else:  # slot == 2
        # Find today's longform video (slot 1 may or may not have run)
        longform_row = db.get_todays_longform_video(channel_id)

        if longform_row is None:
            # Slot 1 failed — run it now before trimming
            logger.warning("[%s] Slot 2 (trim_dual): no longform found today — running slot 1 first", channel_id)
            if video is None:
                logger.error("[%s] No video available to recover slot 1", channel_id)
                db.finish_run(run_id, "failed", error_message="No video available for slot 1 recovery")
                result["status"] = "failed"
                result["error"] = "No video available"
                return

            local_file = _download_with_retry(channel, video, dry_run)
            if local_file is None:
                _handle_download_failure(channel, video, "Download failed after retries", format_type="longform")
                db.finish_run(run_id, "failed", error_message="Download failed")
                result["status"] = "failed"
                result["error"] = "Download failed"
                return

            lf_youtube_id = _upload_video(channel, video, local_file, is_short=False, slot=1, dry_run=dry_run)
            if not lf_youtube_id:
                _handle_upload_failure(channel, video, "Longform upload failed", format_type="longform")
                db.finish_run(run_id, "failed", error_message="Longform upload failed")
                result["status"] = "failed"
                result["error"] = "Longform upload failed"
                cleanup_download(local_file)
                return

            if not dry_run:
                db.mark_uploaded(channel_id, video["id"], lf_youtube_id, format_type="longform",
                                 tiktok_url=video.get("url"), tiktok_title=video.get("title"),
                                 tiktok_timestamp=video.get("timestamp"))
            videos_uploaded += 1
            result["youtube_url"] = f"https://www.youtube.com/watch?v={lf_youtube_id}"
            logger.info("[%s] Recovered slot 1 longform: %s", channel_id, result["youtube_url"])

            # Re-use the already-downloaded file for trimming below
            longform_video = {"id": video["id"], "url": video.get("url"),
                              "title": video.get("title"), "timestamp": video.get("timestamp")}
            trim_source = local_file
        else:
            # Slot 1 already ran — download the same video again for trimming
            longform_video = {
                "id": longform_row["tiktok_video_id"],
                "url": longform_row.get("tiktok_url"),
                "title": longform_row.get("tiktok_title"),
                "timestamp": longform_row.get("tiktok_timestamp"),
            }
            trim_source = _download_with_retry(channel, longform_video, dry_run)
            if trim_source is None:
                _handle_download_failure(channel, longform_video, "Download failed for trim", format_type="short")
                db.finish_run(run_id, "failed", error_message="Download failed for trim")
                result["status"] = "failed"
                result["error"] = "Download failed for trim"
                return

        # Trim to 2:59 (179 seconds) and upload as Short
        trimmed_file = None
        try:
            if not dry_run:
                trimmed_file = trim_source.parent / f"{trim_source.stem}_short.mp4"
                trim_video(trim_source, trimmed_file, duration_seconds=179)
                upload_file = trimmed_file
            else:
                upload_file = trim_source

            short_youtube_id = _upload_video(channel, longform_video, upload_file,
                                              is_short=True, slot=slot, dry_run=dry_run)
            if short_youtube_id:
                if not dry_run:
                    db.mark_uploaded(channel_id, longform_video["id"], short_youtube_id,
                                     format_type="short",
                                     tiktok_url=longform_video.get("url"),
                                     tiktok_title=longform_video.get("title"),
                                     tiktok_timestamp=longform_video.get("timestamp"))
                videos_uploaded += 1
                result["youtube_url_longform"] = result.get("youtube_url")
                result["youtube_url"] = f"https://www.youtube.com/watch?v={short_youtube_id}"
                logger.info("[%s] Trimmed Short uploaded: %s", channel_id, result["youtube_url"])
            else:
                _handle_upload_failure(channel, longform_video, "Short upload failed", format_type="short")
                logger.warning("[%s] Trimmed Short upload failed", channel_id)

        except Exception as exc:
            logger.error("[%s] Trim/upload error: %s", channel_id, exc)
            _handle_upload_failure(channel, longform_video, f"Trim error: {exc}", format_type="short")
        finally:
            cleanup_download(trim_source)
            if trimmed_file and trimmed_file.exists():
                cleanup_download(trimmed_file)

        if videos_uploaded > 0:
            if not dry_run:
                db.finish_run(run_id, "success", videos_uploaded=videos_uploaded)
            else:
                db.finish_run(run_id, "dry_run", videos_uploaded=0)
            result["status"] = "success"
            result["video_uploaded"] = longform_video.get("title", longform_video["id"])
        else:
            db.finish_run(run_id, "failed", error_message="All uploads failed")
            result["status"] = "failed"
            result["error"] = "All uploads failed"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _convert_video(channel: Dict[str, Any], video: Dict[str, Any],
                   local_file: Path, format_type: str) -> Optional[Path]:
    """
    Convert to 4:3 blurred-fill. Returns converted path, or None on failure.
    """
    channel_id = channel["id"]
    converted_path = local_file.parent / f"{local_file.stem}_longform.mp4"
    try:
        return convert_to_4_3_blurred(local_file, converted_path)
    except Exception as exc:
        logger.error("[%s] Conversion error for %s: %s", channel_id, video["id"], exc)
        _handle_upload_failure(channel, video, f"Conversion failed: {exc}",
                                format_type=format_type)
        return None


def _resolve_longform_title(channel: Dict[str, Any], video: Dict[str, Any]) -> str:
    """
    Title for the longform (4:3) upload.
    Appends longform_title_suffix from channel config (default: empty string).
    """
    base = _resolve_title(channel, video)
    suffix = (channel.get("longform_title_suffix") or "").strip()
    if suffix:
        # Ensure total length stays within YouTube's 100-char limit
        combined = f"{base} {suffix}"
        if len(combined) <= 100:
            return combined
        # Truncate base to fit suffix
        max_base = 100 - len(suffix) - 1
        return f"{base[:max_base].rstrip()} {suffix}"
    return base


# ── Video selection ───────────────────────────────────────────────────────────

def _pick_next_video(channel: Dict[str, Any], slot: int,
                     upload_mode: str = "short_only") -> Optional[Dict[str, Any]]:
    """
    Priority order:
      1. Videos in pending_retry state that are due today (retries take priority)
      2. New unposted videos, sorted newest-first

    Supports optional channel config keys:
      min_upload_date       YYYY-MM-DD — ignore TikTok videos older than this date.
      min_backlog_for_slot1 int — slot 1 is skipped unless at least this many
                            unuploaded eligible videos exist.
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

    # Fetch first batch (fast path — newest _PROFILE_BATCH videos only)
    raw_batch = get_profile_videos(channel["tiktok_username"])  # default end=_PROFILE_BATCH
    if raw_batch is None:
        raise TikTokUnreachableError(
            f"TikTok profile @{channel['tiktok_username']} is unreachable after retries"
        )
    if not raw_batch:
        return None

    already_posted = db.get_posted_video_ids(channel_id, upload_mode=upload_mode)

    def _filter(vids):
        if min_ts is not None:
            vids = [v for v in vids if (v.get("timestamp") or 0) >= min_ts]
        return [v for v in vids if v["id"] not in already_posted]

    eligible = _filter(raw_batch)

    # If all videos in the batch are already posted and we got a full batch,
    # there may be older unposted videos — fall back to fetching the full profile.
    if not eligible and len(raw_batch) >= _PROFILE_BATCH:
        logger.info(
            "[%s] All %d videos in first batch already posted — fetching full profile",
            channel_id, _PROFILE_BATCH,
        )
        all_videos = get_profile_videos(channel["tiktok_username"], end=None)
        if all_videos is None:
            raise TikTokUnreachableError(
                f"TikTok profile @{channel['tiktok_username']} is unreachable after retries"
            )
        if min_ts is not None:
            before = len(all_videos)
            all_videos = [v for v in all_videos if (v.get("timestamp") or 0) >= min_ts]
            filtered = before - len(all_videos)
            if filtered:
                logger.info(
                    "[%s] Filtered %d video(s) older than min_upload_date (%s)",
                    channel_id, filtered, channel["min_upload_date"],
                )
        eligible = [v for v in all_videos if v["id"] not in already_posted]

    # Slot throttle: skip slot 1 when backlog is too small
    min_backlog = channel.get("min_backlog_for_slot1")
    if slot == 1 and min_backlog is not None and len(eligible) < int(min_backlog):
        logger.info(
            "[%s] Slot 1 throttled — %d eligible video(s) available, "
            "need %d (min_backlog_for_slot1). Reserving for slot 2.",
            channel_id, len(eligible), int(min_backlog),
        )
        return None

    for video in eligible:
        # Record it in DB so we can track it even if download fails.
        # Use format_type matching what this slot will actually upload so
        # get_posted_video_ids() correctly excludes it on the next run.
        # dual:         create the 'short' row (the 'longform' row is created by mark_uploaded upsert)
        # longform_only: create the 'longform' row directly
        # short_only:   create the 'short' row (legacy, unchanged)
        # split:        slot 1 → 'short'; slot 2 → 'longform'
        if upload_mode in ("longform_only", "trim_dual"):
            seen_format = "longform"
        elif upload_mode == "split":
            seen_format = "longform" if slot == 2 else "short"
        else:
            seen_format = "short"
        db.record_video_seen(channel_id, video, format_type=seen_format)
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
                              error_msg: str, format_type: str = "short") -> None:
    today = date.today()
    db.mark_retry(
        channel_id=channel["id"],
        tiktok_video_id=video["id"],
        error_message=error_msg,
        next_retry_date=today + timedelta(days=1),
        max_retries=channel.get("max_retry_days", 3),
        format_type=format_type,
    )
    logger.warning("[%s] Video %s / %s queued for retry tomorrow: %s",
                   channel["id"], video["id"], format_type, error_msg)


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
                  dry_run: bool, title_override: Optional[str] = None) -> Optional[str]:
    youtube = get_authenticated_client(
        credentials_file=channel["google_credentials_file"],
        token_file=channel["oauth_token_file"],
    )
    return upload_video(
        youtube_client=youtube,
        video_path=local_file,
        title=title_override or _resolve_title(channel, video),
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
      - If target time is still in the future (>15 min): schedule for today
      - If target time is within 15 min: schedule for today (YouTube minimum is 5 min)
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
                            error_msg: str, format_type: str = "short") -> None:
    today = date.today()
    db.mark_retry(
        channel_id=channel["id"],
        tiktok_video_id=video["id"],
        error_message=error_msg,
        next_retry_date=today + timedelta(days=1),
        max_retries=channel.get("max_retry_days", 3),
        format_type=format_type,
    )
