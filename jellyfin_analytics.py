"""Persistent Jellyfin analytics collector for Allerion.

Configure JELLYFIN_URL and JELLYFIN_API_KEY in the server container.
Samples active sessions every 60 seconds; historical data begins at deployment.
"""
import json, os, sqlite3, threading, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB = os.path.join(os.environ.get("DATA_DIR", "/data"), "jellyfin_analytics.sqlite3")
URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
KEY = os.environ.get("JELLYFIN_API_KEY", "")
INTERVAL = max(30, int(os.environ.get("JELLYFIN_ANALYTICS_INTERVAL", "60")))
LOCK = threading.Lock()
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))

def connect():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS samples(
      id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, day TEXT NOT NULL,
      user_id TEXT NOT NULL, user_name TEXT NOT NULL, session_id TEXT NOT NULL,
      item_id TEXT, item_name TEXT, series_name TEXT, item_type TEXT,
      device TEXT, client TEXT, state TEXT NOT NULL, paused INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_user ON samples(user_id,ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_item ON samples(item_id,ts)")
    return c

def sample_once():
    if not URL or not KEY:
        return {"configured": False, "message": "Set JELLYFIN_URL and JELLYFIN_API_KEY on the Allerion server."}
    req = urllib.request.Request(URL + "/Sessions", headers={
      "Accept":"application/json", "X-Emby-Token":KEY,
      "Authorization":'MediaBrowser Client="Allerion", Device="CasaOS", DeviceId="allerion-server", Version="1.0", Token="'+KEY+'"'
    })
    with urllib.request.urlopen(req, timeout=12) as response:
        sessions = json.loads(response.read(2_000_000).decode("utf-8"))
    now = int(time.time()); day = datetime.fromtimestamp(now, TZ).date().isoformat()
    rows=[]
    for s in sessions if isinstance(sessions,list) else []:
        user=s.get("UserId"); name=s.get("UserName")
        if not user or not name: continue
        item=s.get("NowPlayingItem") or {}; state=s.get("PlayState") or {}
        paused=bool(state.get("IsPaused"))
        playing=bool(item)
        rows.append((now,day,str(user),str(name),str(s.get("Id") or s.get("DeviceId") or "unknown"),
          str(item.get("Id") or "") or None,str(item.get("Name") or "") or None,
          str(item.get("SeriesName") or "") or None,str(item.get("Type") or "") or None,
          str(s.get("DeviceName") or s.get("Device") or "")[:120],
          str(s.get("Client") or "")[:120], "paused" if playing and paused else "playing" if playing else "online", int(paused)))
    with LOCK, connect() as db:
        db.executemany("INSERT INTO samples(ts,day,user_id,user_name,session_id,item_id,item_name,series_name,item_type,device,client,state,paused) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",rows)
        # Retain 2 years; database remains bounded.
        db.execute("DELETE FROM samples WHERE ts < ?", (now-63072000,))
    return {"configured":True,"sampled_at":now,"sessions":len(rows)}

def collector():
    while True:
        try: sample_once()
        except Exception: pass
        time.sleep(INTERVAL)

def start():
    threading.Thread(target=collector, name="jellyfin-analytics", daemon=True).start()

def report(period="week", metric="users"):
    if not URL or not KEY:
        return {"configured":False,"message":"Configure JELLYFIN_URL and JELLYFIN_API_KEY on the Allerion server."}
    days={"day":1,"week":7,"month":30,"year":365}.get(period,7)
    now=int(time.time()); since=now-days*86400
    with connect() as db:
        # Daily unique users and peak simultaneous sampled sessions.
        points=[]
        for row in db.execute("SELECT day,COUNT(DISTINCT user_id) users,MAX(n) peak FROM (SELECT day,ts,COUNT(DISTINCT user_id) n FROM samples WHERE ts>=? GROUP BY day,ts) GROUP BY day ORDER BY day",(since,)):
            points.append(dict(row))
        movies=[dict(r) for r in db.execute("""SELECT item_id id, item_name title, series_name,
          COUNT(DISTINCT session_id || ':' || item_id) plays, ROUND(SUM(CASE WHEN state='playing' THEN 1 ELSE 0 END)*? / 60.0,1) watch_minutes
          FROM samples WHERE ts>=? AND item_id IS NOT NULL AND item_type='Movie'
          GROUP BY item_id,item_name,series_name ORDER BY plays DESC,watch_minutes DESC LIMIT 20""",(INTERVAL,since))]
        users=[dict(r) for r in db.execute("""SELECT user_id id,user_name name,
          COUNT(DISTINCT CASE WHEN item_id IS NOT NULL THEN session_id || ':' || item_id END) plays,
          COUNT(DISTINCT item_id) titles,
          ROUND(SUM(CASE WHEN state='playing' THEN 1 ELSE 0 END)*? / 60.0,1) watch_minutes
          FROM samples WHERE ts>=? GROUP BY user_id,user_name ORDER BY watch_minutes DESC,plays DESC LIMIT 20""",(INTERVAL,since))]
        total=db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        first=db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    return {"configured":True,"period":period,"interval_seconds":INTERVAL,"data_points":points,
      "movies":movies,"users":users,"sample_count":total,"first_sample":first,
      "note":"Estimated watch time from periodic session samples; historical data starts when collection is enabled."}
