"""
YouTube Data API v3 uploader.
Each channel has its own OAuth credentials file + token file — no cross-channel sharing.
Tokens auto-refresh when expired. First run triggers a browser OAuth consent flow.
"""

import json
import logging
import random
import time
from pathlib import Path
from typing import Optional, Dict, Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YT_TITLE_LIMIT = 100      # YouTube hard limit
YT_DESCRIPTION_LIMIT = 5000

# HTTP status codes that are transient and safe to retry
_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
_MAX_UPLOAD_RETRIES = 5


def get_authenticated_client(credentials_file: Path, token_file: Path):
    """
    Build an authenticated YouTube API client for one channel.
    If the token file exists and is valid it's used directly.
    If expired it's refreshed silently.
    If missing, triggers browser OAuth flow (first-time setup only).
    """
    creds = _load_token(token_file)

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired token for %s", token_file.name)
        creds.refresh(Request())
        _save_token(creds, token_file)

    elif not creds or not creds.valid:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"OAuth client secret not found: {credentials_file}\n"
                "Run onboard_channel.py to set up credentials for this channel."
            )
        logger.info("No valid token found — starting OAuth flow (browser will open)")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        _save_token(creds, token_file)

    return build("youtube", "v3", credentials=creds)


def upload_video(
    youtube_client,
    video_path: Path,
    title: str,
    description: str,
    tags: list,
    category_id: str,
    is_short: bool,
    description_footer: str = "",
    publish_at: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Upload a video to YouTube.
    Returns the YouTube video ID on success, None on failure.
    Raises HttpError for API errors so the caller can handle retry logic.
    """
    final_title = _truncate_title(title)
    final_description = _build_description(description, description_footer)
    final_tags = list(tags)

    # Adding #Shorts to tags (not title/description) helps algorithm discovery
    if is_short and "Shorts" not in final_tags and "shorts" not in final_tags:
        final_tags.append("Shorts")

    logger.info(
        "Preparing upload | title='%s' | short=%s | tags=%s | publish_at=%s | dry_run=%s",
        final_title, is_short, final_tags, publish_at or "immediate", dry_run,
    )

    if dry_run:
        logger.info("[DRY RUN] Would upload: %s", video_path.name)
        return "DRY_RUN_VIDEO_ID"

    # Use scheduled publishing when publish_at is provided — video is uploaded as
    # Private and YouTube makes it Public at exactly the specified UTC time.
    # This decouples upload time from publish time so GitHub Actions delays
    # never affect when viewers actually see the video.
    body = {
        "snippet": {
            "title": final_title,
            "description": final_description,
            "tags": final_tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "private" if publish_at else "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at
        logger.info("Video will go Public at: %s", publish_at)

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=5 * 1024 * 1024,   # 5 MB chunks
    )

    request = youtube_client.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    last_progress = -1
    retry_count = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct != last_progress:
                    logger.info("Upload progress: %d%%", pct)
                    last_progress = pct
            retry_count = 0  # reset on any successful chunk
        except HttpError as exc:
            if exc.resp.status in _TRANSIENT_STATUS_CODES and retry_count < _MAX_UPLOAD_RETRIES:
                retry_count += 1
                wait = min(60, (2 ** retry_count) + random.uniform(0, 1))
                logger.warning(
                    "Upload transient error %s — retry %d/%d in %.1fs",
                    exc.resp.status, retry_count, _MAX_UPLOAD_RETRIES, wait,
                )
                time.sleep(wait)
            else:
                raise

    video_id = response.get("id")
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info("Upload complete: %s", video_url)
    return video_id


def get_video_metadata(youtube_client, video_id: str) -> Optional[Dict[str, Any]]:
    """Fetch back the uploaded video's metadata for verification."""
    try:
        resp = youtube_client.videos().list(
            part="snippet,status,contentDetails",
            id=video_id,
        ).execute()
        items = resp.get("items", [])
        return items[0] if items else None
    except HttpError as exc:
        logger.warning("Could not fetch metadata for %s: %s", video_id, exc)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_token(token_file: Path) -> Optional[Credentials]:
    if not token_file.exists():
        return None
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", SCOPES),
        )
    except Exception as exc:
        logger.warning("Could not load token from %s: %s", token_file, exc)
        return None


def _save_token(creds: Credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }, f, indent=2)
    logger.debug("Token saved to %s", token_file)


def _truncate_title(title: str) -> str:
    """Trim title to YouTube's 100-char limit, appending '…' if cut."""
    title = title.strip()
    if len(title) > YT_TITLE_LIMIT:
        return title[: YT_TITLE_LIMIT - 1] + "…"
    return title


def _build_description(description: str, footer: str) -> str:
    parts = [description.strip()]
    if footer and footer.strip():
        parts.append("\n\n" + footer.strip())
    result = "".join(parts)
    if len(result) > YT_DESCRIPTION_LIMIT:
        result = result[: YT_DESCRIPTION_LIMIT - 3] + "..."
    return result
