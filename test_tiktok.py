import sys
import datetime
sys.path.insert(0, '.')
from src.tiktok_downloader import get_profile_videos

videos = get_profile_videos('ravenn.grwm')
if videos:
    print(f'Found {len(videos)} videos')
    for v in videos[:5]:
        ts = v.get('timestamp')
        dt = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d') if ts else 'unknown'
        title_preview = v['title'][:60].encode('ascii', 'replace').decode() if v.get('title') else '(no title)'
        vid_id = v['id']
        dur = v.get('duration')
        print(f'  [{dt}] {vid_id} | dur={dur}s | {title_preview}')
else:
    print('No videos found')
