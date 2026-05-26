#!/usr/bin/env python3
"""
Interactive CLI to onboard a new TikTok→YouTube channel pair.

What it does:
  1. Asks for TikTok username and YouTube channel details
  2. Guides you through creating a Google Cloud project + enabling YouTube Data API
  3. Triggers the one-time OAuth browser flow to authorize access
  4. Appends the new channel entry to channels.yaml
  5. Tests the connection with a dry-run

Run:
  python onboard_channel.py

You only need to run this ONCE per channel. After that, tokens auto-refresh.
"""

import json
import sys
import os
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).parent))

CREDENTIALS_DIR = Path(__file__).parent / "credentials"
TOKENS_DIR = Path(__file__).parent / "tokens"
CHANNELS_FILE = Path(__file__).parent / "channels.yaml"


def main():
    print("\n" + "=" * 60)
    print("  TikTok → YouTube Channel Onboarding")
    print("=" * 60)
    print()

    # ── Step 1: Collect channel info ──────────────────────────────
    print("STEP 1: Channel information\n")

    channel_id = input("Enter a unique ID for this channel (e.g. channel_2, raven_grwm): ").strip()
    if not channel_id:
        print("ERROR: channel_id cannot be empty.")
        sys.exit(1)

    tiktok_username = input("TikTok username (without @): ").strip().lstrip("@")
    youtube_channel_name = input("YouTube channel display name: ").strip()
    youtube_category = input("YouTube category ID [press Enter for 22 = People & Blogs]: ").strip() or "22"

    tags_input = input("Default tags (comma-separated, e.g. grwm,fashion,haul): ").strip()
    tags = [t.strip() for t in tags_input.split(",") if t.strip()]

    desc_footer = input("Description footer (optional, press Enter to skip): ").strip()
    max_retry = input("Max retry days for failed downloads [press Enter for 3]: ").strip() or "3"

    print()

    # ── Step 2: Google Cloud setup instructions ───────────────────
    print("STEP 2: Google Cloud Console setup")
    print("-" * 40)
    print("""
You need to create a Google Cloud project and download OAuth credentials.
This is a ONE-TIME setup per channel (or per Google account).

Follow these steps in your browser:

  1. Go to: https://console.cloud.google.com/
  2. Click the project dropdown at the top → "New Project"
  3. Name it something like: tiktok-yt-{channel_id}
     (Note: Google allows ~12 projects per account)
  4. With the new project selected, go to:
       APIs & Services → Library
  5. Search for "YouTube Data API v3" → click it → "Enable"
  6. Go to: APIs & Services → Credentials
  7. Click "+ Create Credentials" → "OAuth client ID"
  8. If prompted, configure the OAuth consent screen first:
       - User type: External
       - App name: anything (e.g. "TikTok YT Bot")
       - Support email: your Google email
       - Add your email under "Test users"
       - Scopes: add "YouTube Data API v3" → .../auth/youtube.upload
  9. Back to Create OAuth client ID:
       - Application type: Desktop app
       - Name: anything
  10. Download the JSON file
  11. Rename it to: channel_{channel_id}_client_secret.json
  12. Move it to: credentials/ folder in this project
""".format(channel_id=channel_id))

    cred_filename = f"channel_{channel_id}_client_secret.json"
    cred_path = CREDENTIALS_DIR / cred_filename
    token_path = TOKENS_DIR / f"channel_{channel_id}_token.json"

    input(f"Press Enter when you have placed the file at:\n  credentials/{cred_filename}\n")

    if not cred_path.exists():
        print(f"\nERROR: File not found: {cred_path}")
        print("Please download and place the credentials file, then re-run this script.")
        sys.exit(1)

    print(f"✓ Credentials file found: {cred_path.name}")
    print()

    # ── Step 3: OAuth flow ─────────────────────────────────────────
    print("STEP 3: Authorise YouTube access (browser will open)")
    print("-" * 40)
    print("""
A browser window will open asking you to sign in to Google.
Sign in with the Google account that OWNS the YouTube channel.
Then click "Allow" to grant upload permissions.

IMPORTANT: If you see a warning "This app isn't verified", click
"Advanced" → "Go to [app name] (unsafe)" → "Allow".
This is expected for personal OAuth apps and is safe.
""")
    input("Press Enter to open the browser and start authorisation...")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)

        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        }
        TOKENS_DIR.mkdir(exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)

        print(f"\n✓ Authorisation successful! Token saved to: tokens/{token_path.name}")
    except Exception as exc:
        print(f"\nERROR during authorisation: {exc}")
        print("Try re-running this script. If the error persists, check that:")
        print("  - Your credentials file is a valid OAuth 2.0 Desktop app JSON")
        print("  - You added your Google email as a test user in the consent screen")
        sys.exit(1)

    # ── Step 4: Append to channels.yaml ───────────────────────────
    print("\nSTEP 4: Adding channel to channels.yaml")
    print("-" * 40)

    new_entry = {
        "id": channel_id,
        "tiktok_username": tiktok_username,
        "youtube_channel_name": youtube_channel_name,
        "google_credentials_file": f"credentials/{cred_filename}",
        "oauth_token_file": f"tokens/channel_{channel_id}_token.json",
        "videos_per_day": 2,
        "description_footer": desc_footer,
        "default_tags": tags,
        "youtube_category_id": youtube_category,
        "enabled": True,
        "max_retry_days": int(max_retry),
        "shorts_max_seconds": 180,
    }

    if CHANNELS_FILE.exists():
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    config.setdefault("channels", [])

    # Check for duplicate
    existing_ids = [ch.get("id") for ch in config["channels"]]
    if channel_id in existing_ids:
        print(f"WARNING: Channel '{channel_id}' already exists in channels.yaml.")
        overwrite = input("Overwrite it? (yes/no): ").strip().lower()
        if overwrite != "yes":
            print("Aborted — channels.yaml unchanged.")
            sys.exit(0)
        config["channels"] = [ch for ch in config["channels"] if ch.get("id") != channel_id]

    config["channels"].append(new_entry)

    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"✓ Channel '{channel_id}' added to channels.yaml")
    print()

    # ── Step 5: Dry run test ───────────────────────────────────────
    print("STEP 5: Test connection with dry run")
    print("-" * 40)
    run_test = input("Run a dry-run now to verify everything works? (yes/no): ").strip().lower()
    if run_test == "yes":
        import subprocess
        result = subprocess.run(
            [sys.executable, "run.py", "--slot", "1", "--channel", channel_id, "--dry-run"],
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            print("\n✓ Dry run passed! Channel is ready.")
        else:
            print("\n⚠ Dry run reported errors. Check the logs/ folder for details.")
    else:
        print("Skipping dry run. You can test manually with:")
        print(f"  python run.py --slot 1 --channel {channel_id} --dry-run")

    print("\n" + "=" * 60)
    print(f"  Channel '{channel_id}' onboarded successfully!")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
