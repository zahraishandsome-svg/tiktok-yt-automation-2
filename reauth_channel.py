"""
One-off re-authentication for a single channel's YouTube OAuth token.
Deletes the (dead) token, runs the browser OAuth flow, saves a fresh token.
Usage: python reauth_channel.py channel_3
"""
import sys
import os
from pathlib import Path

# Resolve all relative paths (credentials/, tokens/) against this script's repo,
# so it works no matter which directory it's invoked from.
os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from src.youtube_uploader import get_authenticated_client  # noqa: E402

if len(sys.argv) < 2:
    print("Usage: python reauth_channel.py <channel_id>")
    sys.exit(1)

channel_id = sys.argv[1]
cred = Path(f"credentials/{channel_id}_client_secret.json")
token = Path(f"tokens/{channel_id}_token.json")

if not cred.exists():
    print(f"ERROR: missing {cred}")
    sys.exit(1)

# Remove the dead token so get_authenticated_client triggers a fresh browser OAuth.
if token.exists():
    token.unlink()
    print(f"Removed dead token: {token}")

print(f"Opening browser for {channel_id} OAuth — sign in with the Google account that owns this channel...")
client = get_authenticated_client(cred, token)
print(f"SUCCESS — fresh token saved to {token}")
# Quick sanity check: list the channel to confirm the token actually works.
try:
    resp = client.channels().list(part="snippet", mine=True).execute()
    title = resp["items"][0]["snippet"]["title"]
    print(f"Authenticated as YouTube channel: {title}")
except Exception as exc:
    print(f"WARN: token saved but channel check failed: {exc}")
