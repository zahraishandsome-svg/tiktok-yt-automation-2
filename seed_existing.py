#!/usr/bin/env python3
"""
One-time script: marks already-uploaded TikTok videos as 'uploaded' in the DB
so the bot never re-posts them.

How it works:
  1. Fetches every video from the YouTube channel via the API
  2. Fetches every video from the TikTok profile via yt-dlp
  3. Matches by title (normalised: lowercase, strip whitespace/emoji-noise)
  4. Marks matched TikTok video IDs as 'uploaded' in the DB

Run once, then forget about it:
  python seed_existing.py --channel channel_1
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src.config import load_config
from src.db import init_db, get_connection
from src.tiktok_downloader import get_profile_videos

_SEED_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _get_seed_client(credentials_file: Path, token_file: Path):
    """Like get_authenticated_client but with broader read scope."""
    creds = None
    seed_token = token_file.parent / (token_file.stem + "_seed.json")

    if seed_token.exists():
        import json
        data = json.load(open(seed_token))
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes", _SEED_SCOPES),
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

    if not creds or not creds.valid:
        print("Opening browser for one-time seed authorization (needs read access)...")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), _SEED_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        import json
        seed_token.parent.mkdir(parents=True, exist_ok=True)
        json.dump({
            "token": creds.token, "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri, "client_id": creds.client_id,
            "client_secret": creds.client_secret, "scopes": list(creds.scopes or _SEED_SCOPES),
        }, open(seed_token, "w"), indent=2)

    return build("youtube", "v3", credentials=creds)


def _normalise(text: str) -> str:
    """Strip emoji, punctuation, lowercase — for fuzzy title matching."""
    if not text:
        return ""
    # Remove emoji (broad Unicode ranges)
    text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    # Remove punctuation and hashtags
    text = re.sub(r'[^\w\s]', ' ', text)
    # Collapse whitespace, lowercase
    return re.sub(r'\s+', ' ', text).strip().lower()


def fetch_youtube_titles(youtube) -> dict:
    """
    Returns {normalised_title: youtube_video_id} for all videos on the
    authenticated user's channel, using the uploads playlist.
    """
    # Get the uploads playlist ID for the authenticated channel
    ch_resp = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch_resp.get("items", [])
    if not items:
        raise ValueError("No YouTube channel found for this Google account.")
    uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    print(f"Fetching videos from uploads playlist {uploads_playlist}...")

    titles = {}
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            vid_id = item["snippet"]["resourceId"]["videoId"]
            title = item["snippet"].get("title", "")
            norm = _normalise(title)
            if norm:
                titles[norm] = vid_id
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    print(f"  Found {len(titles)} videos on YouTube channel")
    return titles


def seed(channel_cfg: dict) -> None:
    creds_file = Path(channel_cfg["google_credentials_file"])
    token_file = Path(channel_cfg["oauth_token_file"])
    tiktok_user = channel_cfg["tiktok_username"]
    channel_id = channel_cfg["id"]
    yt_channel = channel_cfg.get("youtube_channel_id") or channel_cfg.get("youtube_channel_name", "")

    print(f"\n=== Seeding channel: {channel_id} ===")

    youtube = _get_seed_client(creds_file, token_file)
    yt_titles = fetch_youtube_titles(youtube)

    print(f"Fetching TikTok profile: @{tiktok_user}...")
    tiktok_videos = get_profile_videos(tiktok_user)
    if tiktok_videos is None:
        print(f"ERROR: Could not fetch TikTok profile @{tiktok_user} — network error or profile unreachable")
        sys.exit(1)
    print(f"  Found {len(tiktok_videos)} TikTok videos")

    matched = 0
    already_in_db = 0
    unmatched_yt = []

    with get_connection() as conn:
        for video in tiktok_videos:
            vid_id = video["id"]
            title = video.get("title", "")
            norm_tt = _normalise(title)

            # Check if already in DB
            row = conn.execute(
                "SELECT status FROM posted_videos WHERE channel_id=? AND tiktok_video_id=?",
                (channel_id, vid_id),
            ).fetchone()

            if row:
                already_in_db += 1
                continue

            # Try to match against YouTube titles
            yt_vid_id = yt_titles.get(norm_tt)
            if yt_vid_id:
                conn.execute(
                    """INSERT INTO posted_videos
                       (channel_id, tiktok_video_id, tiktok_title, tiktok_url, status,
                        youtube_video_id, posted_at, retry_count)
                       VALUES (?, ?, ?, ?, 'uploaded', ?, datetime('now'), 0)""",
                    (channel_id, vid_id, title,
                     f"https://www.tiktok.com/@{tiktok_user}/video/{vid_id}",
                     yt_vid_id),
                )
                print(f"  [MATCHED] {vid_id} -> yt:{yt_vid_id} | {title[:60].encode('ascii','replace').decode()}")
                matched += 1
            else:
                # Partial match: check if any YouTube title starts with the TikTok title
                norm_short = norm_tt[:40]
                partial = next((yt_id for norm_yt, yt_id in yt_titles.items()
                                if norm_yt.startswith(norm_short) or norm_short in norm_yt), None)
                if partial and norm_short:
                    conn.execute(
                        """INSERT INTO posted_videos
                           (channel_id, tiktok_video_id, tiktok_title, tiktok_url, status,
                            youtube_video_id, posted_at, retry_count)
                           VALUES (?, ?, ?, ?, 'uploaded', ?, datetime('now'), 0)""",
                        (channel_id, vid_id, title,
                         f"https://www.tiktok.com/@{tiktok_user}/video/{vid_id}",
                         partial),
                    )
                    print(f"  [PARTIAL] {vid_id} -> yt:{partial} | {title[:60].encode('ascii','replace').decode()}")
                    matched += 1

        conn.commit()

    # Report YouTube videos that couldn't be matched (may be non-TikTok content)
    matched_yt_ids = set()
    for video in tiktok_videos:
        norm_tt = _normalise(video.get("title", ""))
        if norm_tt in yt_titles:
            matched_yt_ids.add(yt_titles[norm_tt])

    unmatched_yt = [(title, vid_id) for title, vid_id in yt_titles.items()
                    if vid_id not in matched_yt_ids]

    print(f"\n--- Results ---")
    print(f"  TikTok videos matched to YouTube: {matched}")
    print(f"  Already in DB (skipped):          {already_in_db}")
    if unmatched_yt:
        print(f"  YouTube videos NOT matched ({len(unmatched_yt)}) — may be original content or title mismatch:")
        for norm_title, yt_id in unmatched_yt[:10]:
            print(f"    yt:{yt_id} | {norm_title[:70].encode('ascii','replace').decode()}")
        if len(unmatched_yt) > 10:
            print(f"    ... and {len(unmatched_yt) - 10} more")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", required=True,
                        help="Channel ID from channels.yaml (e.g. channel_1)")
    args = parser.parse_args()

    init_db()
    config = load_config()

    channel_cfg = next(
        (c for c in config["channels"] if c["id"] == args.channel), None
    )
    if not channel_cfg:
        print(f"ERROR: channel '{args.channel}' not found in channels.yaml")
        sys.exit(1)

    seed(channel_cfg)


if __name__ == "__main__":
    main()
