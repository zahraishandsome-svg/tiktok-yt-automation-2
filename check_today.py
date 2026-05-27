import sqlite3
from datetime import date

con = sqlite3.connect('data/automation.db')
con.row_factory = sqlite3.Row
today = str(date.today())

print(f"=== Runs today ({today}) ===")
rows = con.execute(
    "SELECT channel_id, slot, status, videos_uploaded FROM runs WHERE run_date=? ORDER BY channel_id, slot",
    (today,)
).fetchall()
for r in rows:
    print(f"  {r['channel_id']} slot {r['slot']} -> {r['status']} ({r['videos_uploaded']} uploaded)")
if not rows:
    print("  (none yet)")

print()
print("=== Posted videos today (all channels) ===")
rows2 = con.execute(
    "SELECT channel_id, COUNT(*) as n FROM posted_videos WHERE status='uploaded' AND date(posted_at)=? GROUP BY channel_id",
    (today,)
).fetchall()
for r in rows2:
    print(f"  {r['channel_id']}: {r['n']} video(s) posted today")
if not rows2:
    print("  (none)")

print()
print("=== Total uploaded per channel (all time) ===")
rows3 = con.execute(
    "SELECT channel_id, COUNT(*) as n FROM posted_videos WHERE status='uploaded' GROUP BY channel_id"
).fetchall()
for r in rows3:
    print(f"  {r['channel_id']}: {r['n']} total")

con.close()
