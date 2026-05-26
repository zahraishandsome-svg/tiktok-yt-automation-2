"""
Discord webhook notifications.
Only fires when DISCORD_WEBHOOK_URL is set in .env — silent otherwise.
"""

import logging
import requests
from datetime import date
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

_TIMEOUT = 10   # seconds


def send_failure_alert(webhook_url: str, failures: List[Dict[str, Any]], slot: int) -> None:
    """Post a failure alert to Discord immediately when any channel fails."""
    if not webhook_url:
        return

    lines = [f"🚨 **TikTok→YT Automation | Slot {slot} Failures** ({date.today()})"]
    for f in failures:
        lines.append(f"• `{f['channel_id']}` — {f.get('error', 'unknown error')}")

    _post(webhook_url, "\n".join(lines))


def send_daily_summary(
    webhook_url: str,
    db_rows: List[Dict[str, Any]],
    channel_names: Dict[str, str] = None,
) -> None:
    """Post an end-of-day summary after slot 2 completes, covering both slots."""
    if not webhook_url:
        return

    channel_names = channel_names or {}

    def label(channel_id: str) -> str:
        name = channel_names.get(channel_id)
        return f"`{channel_id}` ({name})" if name else f"`{channel_id}`"

    success_rows = [r for r in db_rows if r["status"] == "success"]
    failed_rows = [r for r in db_rows if r["status"] == "failed"]
    no_content_rows = [r for r in db_rows if r["status"] == "no_content"]

    emoji = "✅" if not failed_rows else "⚠️"
    lines = [
        f"{emoji} **Daily Upload Summary** ({date.today()})",
        f"Uploaded: {len(success_rows)} | Failed: {len(failed_rows)} | No content: {len(no_content_rows)}",
    ]

    for r in success_rows:
        yt_id = r.get("youtube_video_id")
        url = f"https://www.youtube.com/watch?v={yt_id}" if yt_id else "?"
        lines.append(f"  ✓ {label(r['channel_id'])} slot {r.get('slot', '?')} → {url}")
    for r in failed_rows:
        lines.append(f"  ✗ {label(r['channel_id'])} slot {r.get('slot', '?')} — {r.get('error_message', '?')}")
    for r in no_content_rows:
        lines.append(f"  — {label(r['channel_id'])} slot {r.get('slot', '?')}: no new content")

    _post(webhook_url, "\n".join(lines))


def _post(webhook_url: str, content: str) -> None:
    try:
        resp = requests.post(
            webhook_url,
            json={"content": content[:2000]},   # Discord limit is 2000 chars
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        logger.debug("Discord notification sent")
    except requests.RequestException as exc:
        # Notification failure must never crash the main run
        logger.warning("Discord notification failed: %s", exc)
