# Architecture — TikTok → YouTube Automation

## Overview

A GitHub Actions-based pipeline that:
1. Fetches the latest videos from a TikTok profile using yt-dlp
2. Downloads the video without watermark
3. Uploads to a YouTube channel via the YouTube Data API v3
4. Schedules the video to go public at a precise time using YouTube's `publishAt` field
5. Commits the updated SQLite state DB back to the repo

---

## Repository Layout

```
tiktok-yt-automation/           # ch1 (Raven GRWM), ch2 (miraikun), ch3 (mukuzo)
tiktok-yt-automation-2/         # ch4 (aiivannaa)
```

Each repo is self-contained with its own credentials, tokens, and per-channel DBs.

```
.github/workflows/
  upload-slot1.yml              # Slot 1 upload for channel 1 (13:00 UTC)
  upload-slot2.yml              # Slot 2 upload for channel 1 (15:00 UTC)
  upload-slot1-ch2.yml          # Slot 1 ch2 (09:00 UTC)
  ... (one workflow per channel per slot)
  daily-summary.yml             # Nightly Discord summary (00:00 UTC)

src/
  tiktok_downloader.py          # yt-dlp wrapper + format selector
  youtube_uploader.py           # YouTube Data API v3 uploader
  channel_runner.py             # Full pipeline for one channel+slot
  db.py                         # SQLite state wrapper
  config.py                     # Channel config loader

data/
  channel_1.db                  # Per-channel SQLite state (committed)
  channel_2.db
  channel_3.db

credentials/                    # OAuth client secrets (gitignored)
tokens/                         # OAuth access/refresh tokens (gitignored)
```

---

## Workflow Execution Flow

```
cron-job.org (exact time)
  → workflow_dispatch → GitHub Actions
    → git checkout repo (with latest DB)
    → pip install requirements
    → restore credentials/tokens from GitHub Secrets
    → python run.py --slot N --channel channel_X
      → channel_runner.run_channel()
        → slot_already_ran()? → skip if True
        → _pick_next_video() → queries TikTok profile
        → download_video() → yt-dlp with play format
        → upload_video() → YouTube API, scheduled via publishAt
        → db.mark_uploaded()
    → git push (with retry loop)
    → Discord notification
```

---

## Download Format Selector

**Critical:** always use `format_id^=play`, never `format_id^=download`.

```python
_WATERMARK_FREE_FORMAT = (
    "bestvideo[format_id^=play][ext=mp4]+bestaudio"
    "/best[format_id^=play][ext=mp4]"
    "/best[format_id^=play]"
    "/best[ext=mp4]"
    "/best"
)
```

`format_id^=download` = TikTok's "Save Video" URL = **watermarked**
(`format_note='watermarked'`, `preference=-2` in yt-dlp source).

`format_id^=play` = raw stream = **no watermark**.

See `SKILL.md` for the full incident history.

---

## Scheduling Architecture

Videos are uploaded as **Private** with a `publishAt` UTC timestamp. YouTube
makes them public automatically at exactly that time, regardless of when the
GitHub Actions runner actually ran. This decouples upload timing from publish
timing and tolerates GitHub Actions cron delays of hours.

If the target publish time has already passed when the workflow runs, the video
publishes immediately (`publishAt` is omitted).

---

## State Machine (posted_videos.status)

```
                    +--------+
                    | pending|  ← seen on TikTok, not yet uploaded
                    +--------+
                       |
              download+upload OK
                       |
                    +----------+
                    | uploaded |  ← live on YouTube
                    +----------+
                       |
           YouTube video deleted manually
                       |
              +-----------------------+
              | deleted_repost_pending|  ← eligible for clean re-upload
              +-----------------------+
                       |
              picked up on next run
                       |
                    +----------+
                    | uploaded |  ← re-uploaded clean
                    +----------+

On download/upload failure:
  +---------------+     max_retries exceeded    +-----------------+
  | pending_retry |  ─────────────────────────> | failed_permanent|
  +---------------+                             +-----------------+
```

---

## DB Push Race Condition

When two channel workflows run simultaneously (same repo, different channels),
both try to `git push` the updated DB to `origin/main`. The second push fails
with "failed to push some refs". Fixed by a retry loop in every workflow:

```bash
for i in 1 2 3; do
  git push origin main && break
  git fetch origin main
  git rebase origin/main
done
```
