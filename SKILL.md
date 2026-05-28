# TikTok → YouTube Automation — Skill Reference

> Quick-reference for operators and developers. For full architecture see
> `reference/architecture.md`. For debugging see `scripts/troubleshoot.md`.

---

## Channel Map

| ID        | Name               | TikTok            | Schedule (UTC)     | Repo                      |
|-----------|--------------------|-------------------|--------------------|---------------------------|
| channel_1 | Raven GRWM Haul    | @ravenn.grwm      | 13:00 + 15:00      | tiktok-yt-automation      |
| channel_2 | miraikun.dayo1     | @miraikun0515.dayo | 09:00 + 10:00     | tiktok-yt-automation      |
| channel_3 | 今日のむくぞ         | @__muk            | 09:00 + 10:00      | tiktok-yt-automation      |
| channel_4 | aiivannaa          | @aiivannaa        | 14:00 + 16:00      | tiktok-yt-automation-2    |

---

## Video Status Reference

| Status                    | Meaning                                                       |
|---------------------------|---------------------------------------------------------------|
| `pending`                 | Seen on TikTok profile, not yet uploaded                      |
| `uploaded`                | Successfully uploaded to YouTube                              |
| `pending_retry`           | Download or upload failed; scheduled for retry tomorrow       |
| `failed_permanent`        | Failed after max retries; will never be re-attempted          |
| `skipped`                 | Explicitly skipped (e.g. duplicate, out of scope)             |
| `deleted_repost_pending`  | YouTube video was deleted; video is eligible for clean re-upload |

**Note:** `get_posted_video_ids()` excludes `uploaded`, `failed_permanent`, `skipped`, and
`pending_retry` from the eligible pool. It does NOT exclude `pending` or
`deleted_repost_pending` — these will be re-selected on the next run.

---

## Watermark Fix (2026-05-28)

### Root Cause
The `_WATERMARK_FREE_FORMAT` selector in `src/tiktok_downloader.py` was using
`format_id^=download` as the primary selector. This selects TikTok's **"Save
Video"** stream, which yt-dlp explicitly marks as:
```python
'format_note': 'watermarked',
'preference': -2,
```
The result was a TikTok logo burned into every downloaded video.

**Why Japanese channels (ch2, ch3) were unaffected:** their creators have
"Allow Downloads" disabled on TikTok → no `download` format exists →
selector fell through to `best[ext=mp4]` → picked `play` (clean stream).

**Why Western channels (ch1, ch4) were affected:** their creators have
downloads enabled → `download` format was found first → watermarked.

### Fix Applied
```python
# BEFORE (broken — picks watermarked download_addr stream)
_WATERMARK_FREE_FORMAT = (
    "bestvideo[format_id^=download][ext=mp4]+bestaudio/bestvideo[ext=mp4]+bestaudio/best"
)

# AFTER (correct — picks clean play_addr stream)
_WATERMARK_FREE_FORMAT = (
    "bestvideo[format_id^=play][ext=mp4]+bestaudio"
    "/best[format_id^=play][ext=mp4]"
    "/best[format_id^=play]"
    "/best[ext=mp4]"
    "/best"
)
```
Fix applied to: `tiktok-yt-automation`, `tiktok-yt-automation-2`, and
`tiktok-fb-automation` on 2026-05-28.

### Cleanup Performed
- 6 watermarked YouTube videos per affected channel marked `deleted_repost_pending`
- Today's success run records cleared → workflows re-triggered same day
- Videos will be re-downloaded and re-uploaded clean on next workflow run

---

## `deleted_repost_pending` — When and How

**When it's set:** when a YouTube video is deleted and the TikTok source should
be re-downloaded and re-uploaded clean.

**DB functions:**
- `db.mark_deleted_repost_pending(channel_id, tiktok_video_id)` — sets status,
  clears `youtube_video_id` and `posted_at`
- `db.delete_todays_success_runs(channel_id)` — removes today's success run
  records so `slot_already_ran()` returns False and the workflow can re-run

**Scheduler behaviour:** the scheduler's `_pick_next_video()` calls
`get_posted_video_ids()` which does NOT include `deleted_repost_pending`, so
the video is eligible for re-selection. On re-upload it becomes `uploaded` again
with a new YouTube video ID and fresh `posted_at` timestamp.

---

## DB Push Race Condition Fix (2026-05-28)

All upload workflows use a retry loop when pushing the updated DB to Git:
```bash
for i in 1 2 3; do
  git push origin main && break
  echo "Push failed (attempt $i/3) -- rebasing and retrying..."
  git fetch origin main
  git rebase origin/main
done
```
This prevents the "failed to push some refs" error when two channel workflows
push simultaneously.

---

## GitHub Token

- Token: stored in `C:\Users\Zahid\projects\setup_cronjobs.py` (local only, gitignored)
- Expires: 2026-08-26
- Used in: cron-job.org `workflow_dispatch` calls and local remote URLs
- To update: see `scripts/troubleshoot.md` → "Updating the GitHub token in cron-job.org"

---

## Alerting & Summary

- **Instant Discord alerts:** sent per upload (success with YouTube URL, or
  failure with error message)
- **Nightly summary (00:00 UTC):** shows one row per channel per slot.
  Prefers the most recent SUCCESS run per slot; falls back to the most recent
  non-running run. Failures are invisible in the summary if a later success
  exists for the same channel+slot.
