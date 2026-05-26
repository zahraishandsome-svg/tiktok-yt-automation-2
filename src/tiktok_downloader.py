"""
TikTok scraping and downloading via yt-dlp.
Watermark removal is handled by preferring the 'download_addr' format over 'play_addr'.
Never raises — returns None on failure so channel_runner can decide retry logic.
"""

import logging
import shutil
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import yt_dlp

logger = logging.getLogger(__name__)

# Format selector that prefers the non-watermarked download URL.
# TikTok exposes two copies: play_addr (watermarked) and download_addr (clean).
# yt-dlp's TikTok extractor labels the clean one; we filter by format_id prefix.
_WATERMARK_FREE_FORMAT = (
    "bestvideo[format_id^=download][ext=mp4]+bestaudio/bestvideo[ext=mp4]+bestaudio/best"
)


_FETCH_RETRIES = 3
_FETCH_RETRY_BASE_WAIT = 2   # seconds, doubles each attempt


def get_profile_videos(tiktok_username: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch video metadata from a public TikTok profile.
    Returns:
      - List of video dicts sorted newest-first on success (may be empty if no videos)
      - None if the profile could not be fetched after all retries (network/TikTok error)
        — callers must treat None as an alert-worthy failure, not just "no content"
    Does NOT download — just lists metadata.
    """
    url = f"https://www.tiktok.com/@{tiktok_username}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",   # list without downloading
        "ignoreerrors": True,
        "skip_download": True,
    }

    logger.info("Fetching video list from TikTok: @%s", tiktok_username)
    info = None
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break   # success — exit retry loop
        except Exception as exc:
            if attempt < _FETCH_RETRIES:
                wait = _FETCH_RETRY_BASE_WAIT ** attempt
                logger.warning(
                    "TikTok fetch attempt %d/%d failed for @%s, retrying in %ds: %s",
                    attempt, _FETCH_RETRIES, tiktok_username, wait, exc,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "TikTok fetch failed for @%s after %d attempts — profile may be "
                    "blocked or unreachable: %s",
                    tiktok_username, _FETCH_RETRIES, exc,
                )
                return None   # ← distinct from empty profile

    if not info or "entries" not in info:
        logger.warning("No entries returned for @%s — profile is empty or private", tiktok_username)
        return []   # ← accessible but empty

    videos = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        videos.append({
            "id": entry.get("id"),
            "url": entry.get("url") or entry.get("webpage_url"),
            "title": _clean_title(entry.get("title") or ""),
            "description": entry.get("description") or "",
            "timestamp": entry.get("timestamp"),        # Unix epoch
            "duration": entry.get("duration"),          # seconds
            "width": entry.get("width"),
            "height": entry.get("height"),
        })

    # Newest first — this is the posting priority order
    videos.sort(key=lambda v: v.get("timestamp") or 0, reverse=True)
    logger.info("Found %d videos on @%s profile", len(videos), tiktok_username)
    return videos


def download_video(video_url: str, video_id: str, output_dir: Path) -> Optional[Path]:
    """
    Download one TikTok video without watermark.
    Returns the local file path on success, None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": _WATERMARK_FREE_FORMAT,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "ignoreerrors": False,
        # Needed for some TikTok region restrictions
        "geo_bypass": True,
    }

    logger.info("Downloading TikTok video %s", video_id)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info:
                logger.error("yt-dlp returned no info for %s", video_id)
                return None
    except yt_dlp.utils.DownloadError as exc:
        logger.error("Download failed for %s: %s", video_id, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error downloading %s: %s", video_id, exc)
        return None

    # Locate the output file (ext could be mp4 or webm)
    for ext in ("mp4", "webm", "mkv"):
        candidate = output_dir / f"{video_id}.{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            logger.info("Downloaded: %s (%.1f MB)", candidate.name,
                        candidate.stat().st_size / 1_048_576)
            return candidate

    logger.error("Download reported success but no output file found for %s", video_id)
    return None


def is_watermarked(file_path: Path) -> bool:
    """
    Heuristic: if the filename contains 'watermark' the downloader picked the wrong format.
    yt-dlp shouldn't produce such files with our format selector, but we check anyway.
    """
    return "watermark" in file_path.name.lower()


def is_short_video(duration: Optional[float], width: Optional[int],
                   height: Optional[int], max_seconds: int = 180) -> bool:
    """True if video qualifies as a YouTube Short (vertical + under max_seconds)."""
    vertical = (height or 0) > (width or 0)
    short_enough = (duration or 999) <= max_seconds
    return vertical and short_enough


def cleanup_download(file_path: Path) -> None:
    """Delete a downloaded video file. Safe to call even if file is gone."""
    try:
        if file_path.exists():
            file_path.unlink()
            logger.debug("Deleted local file: %s", file_path)
    except Exception as exc:
        logger.warning("Could not delete %s: %s", file_path, exc)


def cleanup_stale_downloads(output_dir: Path, max_age_days: int = 7) -> None:
    """Remove any video files older than max_age_days to prevent disk bloat."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    if not output_dir.exists():
        return
    for f in output_dir.iterdir():
        if f.suffix in (".mp4", ".webm", ".mkv"):
            modified = datetime.utcfromtimestamp(f.stat().st_mtime)
            if modified < cutoff:
                f.unlink()
                logger.info("Purged stale download: %s", f.name)


def _clean_title(title: str) -> str:
    """Strip common TikTok junk from titles before storing."""
    # TikTok sometimes sets title to the username or a hashtag dump; keep as-is.
    return title.strip()
