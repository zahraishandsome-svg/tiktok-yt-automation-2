"""
Loops through all enabled channels and runs channel_runner for each.
Error isolation: if one channel fails, the others still run.
Aggregates results and triggers Discord notification on any failure.
"""

import logging
from typing import Dict, Any, List, Optional

from .config import get_enabled_channels
from .channel_runner import run_channel
from .notifier import send_failure_alert

logger = logging.getLogger(__name__)


def run_all_channels(
    config: Dict[str, Any],
    slot: int,
    channel_filter: Optional[str] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """
    Run the pipeline for every enabled channel.
    channel_filter: if set, only run this channel ID (used with --channel flag for testing).
    Returns list of per-channel result dicts.
    """
    channels = get_enabled_channels(config)

    if channel_filter:
        channels = [ch for ch in channels if ch["id"] == channel_filter]
        if not channels:
            logger.error("No enabled channel with id='%s' found in channels.yaml", channel_filter)
            return []

    if not channels:
        logger.warning("No enabled channels found — nothing to do")
        return []

    logger.info("Running slot %d | channels=%d | dry_run=%s", slot, len(channels), dry_run)

    results = []
    failures = []

    for channel in channels:
        channel_id = channel["id"]
        logger.info("── Starting channel: %s (@%s) ──", channel_id, channel["tiktok_username"])
        try:
            result = run_channel(channel=channel, slot=slot, dry_run=dry_run)
            results.append(result)

            if result["status"] == "failed":
                failures.append(result)
                logger.error("[%s] FAILED: %s", channel_id, result.get("error"))
            elif result["status"] == "success":
                logger.info("[%s] SUCCESS: %s", channel_id, result.get("youtube_url"))
            else:
                logger.info("[%s] %s", channel_id, result["status"].upper())

        except Exception as exc:
            # Belt-and-suspenders: channel_runner should never raise, but just in case
            error_msg = f"Unhandled exception in channel runner: {exc}"
            logger.exception("[%s] %s", channel_id, error_msg)
            result = {
                "channel_id": channel_id,
                "slot": slot,
                "status": "failed",
                "video_uploaded": None,
                "youtube_url": None,
                "error": error_msg,
            }
            results.append(result)
            failures.append(result)

    # Notifications
    webhook_url = config.get("discord_webhook_url", "")
    if webhook_url:
        if failures:
            send_failure_alert(webhook_url=webhook_url, failures=failures, slot=slot)

    _log_summary(results)
    return results


def _log_summary(results: List[Dict[str, Any]]) -> None:
    success = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    no_content = [r for r in results if r["status"] == "no_content"]

    logger.info(
        "── Summary: %d success | %d failed | %d skipped | %d no_content ──",
        len(success), len(failed), len(skipped), len(no_content),
    )
    for r in success:
        logger.info("  ✓ %s → %s", r["channel_id"], r.get("youtube_url"))
    for r in failed:
        logger.error("  ✗ %s → %s", r["channel_id"], r.get("error"))
