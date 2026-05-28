# Troubleshooting Guide

---

## Watermarks on uploaded videos

**Symptom:** TikTok logo visible in bottom-left corner of YouTube videos.

**Root cause:** `_WATERMARK_FREE_FORMAT` in `src/tiktok_downloader.py` is using
`format_id^=download` instead of `format_id^=play`.

- `format_id^=download` = TikTok's "Save Video" stream = **watermarked**
- `format_id^=play` = raw stream = **clean, no watermark**

**Diagnosis:**
```bash
# Run yt-dlp with --verbose on a video URL from the affected channel
# Look for a line like: "[info] tiktok:... Downloading format download"
# It should say "play" not "download"
python -c "
import yt_dlp
opts = {'verbose': True, 'skip_download': True}
with yt_dlp.YoutubeDL(opts) as ydl:
    ydl.extract_info('https://www.tiktok.com/@ravenn.grwm/video/VIDEO_ID', download=False)
" 2>&1 | grep -i "format\|play\|download\|watermark"
```

**Fix:** in `src/tiktok_downloader.py`:
```python
# CORRECT — always use format_id^=play
_WATERMARK_FREE_FORMAT = (
    "bestvideo[format_id^=play][ext=mp4]+bestaudio"
    "/best[format_id^=play][ext=mp4]"
    "/best[format_id^=play]"
    "/best[ext=mp4]"
    "/best"
)
```

**Note:** if watermarks appear again, first check which format yt-dlp is
selecting with `--verbose`. Should always show `play` not `download`.

**Why some channels are immune:** creators with "Allow Downloads" disabled on
TikTok have no `download` format exposed → selector auto-falls through to
`best[ext=mp4]` → picks `play` (clean). Channels that ARE affected are those
whose creators have downloads enabled.

---

## "Failed to push some refs" (DB commit race condition)

**Symptom:** one of two simultaneous channel workflow runs fails with:
```
! [rejected] main -> main (fetch first)
error: failed to push some refs
```

**Cause:** two workflows in the same repo both push DB changes to `origin/main`
at the same time. The second push is rejected because the first one moved HEAD.

**Fix (already deployed):** all upload workflows use a retry+rebase loop:
```bash
for i in 1 2 3; do
  git push origin main && break
  echo "Push failed (attempt $i/3) -- rebasing and retrying..."
  git fetch origin main
  git rebase origin/main
done
```
If you see this error again, confirm the loop is present in the affected workflow.

---

## slot_already_ran() blocking a re-run

**Symptom:** workflow runs but immediately exits with "Slot N already ran
successfully today — skipping".

**When this is intentional:** prevents double-posting on the same day.

**When you need to force a re-run** (e.g. after deleting watermarked videos):
```python
import sqlite3
from datetime import date

conn = sqlite3.connect("data/channel_X.db")
conn.execute(
    "DELETE FROM runs WHERE channel_id=? AND run_date=? AND status='success'",
    ("channel_X", date.today().isoformat())
)
conn.commit()
conn.close()
```
Then commit the DB and re-trigger the workflow via `workflow_dispatch`.

---

## No videos uploaded today / "no_content" status

**Possible causes:**
1. All videos in the TikTok profile's top 50 are already in DB as `uploaded`
   → the pipeline fetches the full profile and checks again. If all are posted,
   it returns `no_content`. Channel needs new TikTok content.
2. TikTok profile is unreachable (bot detection, IP block, timeout)
   → check the run logs in GitHub Actions artifacts. Retry tomorrow.
3. `min_upload_date` filter set in channel config is too restrictive
   → all eligible videos are older than the cutoff.

---

## TikTok profile returns empty / "Unable to extract secondary user ID"

**Symptom:**
```
ERROR: [tiktok:user] username: Unable to extract secondary user ID
```

**Cause:** TikTok rate-limiting or bot detection on the GitHub Actions IP.
Usually transient — resolves on next run (tomorrow's schedule).

**If persistent:** add `TIKTOK_COOKIES_FILE` env var pointing to a Netscape-
format cookies file exported from a logged-in TikTok browser session.

---

## Duplicate video uploaded to two slots

**Cause:** both slot workflows ran simultaneously and both picked the same
unposted video before either committed the DB back.

**Prevention:** already fixed by the DB push retry loop — second workflow
fetches latest DB (with first video now `uploaded`), picks a different video.

**Recovery:**
1. Delete the duplicate from YouTube Studio
2. In the DB, the duplicate's `tiktok_video_id` will have status `uploaded`
   from the first successful run — leave it as-is (it was re-uploaded clean by
   the second run too, so both YouTube IDs were valid)

---

## deleted_repost_pending — videos not being re-uploaded

**Symptom:** videos set to `deleted_repost_pending` are not picked up by
the next workflow run.

**Check 1:** confirm `slot_already_ran()` isn't blocking:
```sql
SELECT * FROM runs WHERE run_date = date('now') AND status = 'success';
```
If rows exist for the channel+slot, delete them and re-trigger.

**Check 2:** confirm TikTok profile is reachable (see above).

**Check 3:** confirm `get_posted_video_ids()` excludes the right statuses:
```python
# In src/db.py — should NOT include 'deleted_repost_pending'
rows = conn.execute("""
    SELECT tiktok_video_id FROM posted_videos
    WHERE channel_id = ? AND status IN ('uploaded', 'failed_permanent', 'skipped', 'pending_retry')
""", (channel_id,)).fetchall()
```

---

## Updating the GitHub token in cron-job.org

When the GitHub PAT expires:
1. Generate a new token at https://github.com/settings/tokens with `workflow` scope
2. Update `GITHUB_TOKEN` in `C:\Users\Zahid\projects\setup_cronjobs.py`
3. Update remote URLs in both local repos:
   ```bash
   git remote set-url origin https://NEW_TOKEN@github.com/zahraishandsome-svg/REPO.git
   ```
4. Use the cron-job.org session JWT (via XHR interception) to bulk-update all jobs —
   the REST API key has a rate limit of ~7 calls before 429.
