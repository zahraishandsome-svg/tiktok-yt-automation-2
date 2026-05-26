"""
Load and validate channels.yaml + .env settings.
All channel-specific values come from channels.yaml — nothing is hardcoded here.
"""

import os
import yaml
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


def load_config() -> Dict[str, Any]:
    """Load full config: channels.yaml + .env merged into one dict."""
    load_dotenv(PROJECT_ROOT / ".env")

    channels_file = PROJECT_ROOT / "channels.yaml"
    if not channels_file.exists():
        raise FileNotFoundError(f"channels.yaml not found at {channels_file}")

    with open(channels_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    channels = raw.get("channels", [])
    if not isinstance(channels, list):
        raise ValueError("channels.yaml must have a top-level 'channels' list")

    validated = []
    for ch in channels:
        validated.append(_validate_channel(ch))

    return {
        "channels": validated,
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "project_root": PROJECT_ROOT,
    }


def _validate_channel(ch: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure required fields exist and resolve file paths to absolute."""
    required = [
        "id", "tiktok_username", "youtube_channel_name",
        "google_credentials_file", "oauth_token_file",
    ]
    for field in required:
        if not ch.get(field):
            raise ValueError(f"Channel config missing required field: '{field}'")

    # Resolve credential paths relative to project root
    ch["google_credentials_file"] = PROJECT_ROOT / ch["google_credentials_file"]
    ch["oauth_token_file"] = PROJECT_ROOT / ch["oauth_token_file"]

    # Apply defaults
    ch.setdefault("videos_per_day", 2)
    ch.setdefault("description_footer", "")
    ch.setdefault("default_tags", [])
    ch.setdefault("youtube_category_id", "22")
    ch.setdefault("enabled", True)
    ch.setdefault("max_retry_days", 3)
    ch.setdefault("shorts_max_seconds", 180)  # 3 minutes

    return ch


def get_enabled_channels(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only channels with enabled=true."""
    return [ch for ch in config["channels"] if ch.get("enabled", True)]
