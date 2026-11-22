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
    # Safe, additive migration for databases created by earlier Allerion versions.
    columns = {row["name"] for row in c.execute("PRAGMA table_info(samples)")}
    if "position_ticks" not in columns:
        c.execute("ALTER TABLE samples ADD COLUMN position_ticks INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_user ON samples(user_id,ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_item ON samples(item_id,ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_session_item ON samples(session_id,item_id,ts)")
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
    with LOCK, connect() as db:
      for s in sessions if isinstance(sessions,list) else []:
        user=s.get("UserId"); name=s.get("UserName")
        if not user or not name: continue
        item=s.get("NowPlayingItem") or {}; play_state=s.get("PlayState") or {}
        paused=bool(play_state.get("IsPaused"))
        item_id=str(item.get("Id") or "") or None
        position=play_state.get("PositionTicks")
        try:
            position=int(position) if position is not None else None
        except (TypeError, ValueError):
            position=None

        if not item:
            playback_state="online"
        elif paused:
            playback_state="paused"
        elif position is None:
            playback_state="unknown"
        else:
            session_id=str(s.get("Id") or s.get("DeviceId") or "unknown")
            previous=db.execute(
                """SELECT ts,position_ticks FROM samples
                   WHERE session_id=? AND item_id=? AND position_ticks IS NOT NULL
                   ORDER BY ts DESC,id DESC LIMIT 1""",
                (session_id,item_id)
            ).fetchone()
            # Jellyfin may omit PlayState.IsPlaying. Confirm playback by observing
            # forward movement of PositionTicks for this same session and item.
            if previous and position > int(previous["position_ticks"]):
                elapsed=max(0, now-int(previous["ts"]))
                advance_seconds=(position-int(previous["position_ticks"]))/10_000_000
                # Allow modest timing jitter, but reject implausibly large jumps.
                playback_state="playing" if 0 < advance_seconds <= elapsed + 15 else "unknown"
            else:
                playback_state="unknown"

        rows.append((now,day,str(user),str(name),str(s.get("Id") or s.get("DeviceId") or "unknown"),
          item_id,str(item.get("Name") or "") or None,
          str(item.get("SeriesName") or "") or None,str(item.get("Type") or "") or None,
          str(s.get("DeviceName") or s.get("Device") or "")[:120],
          str(s.get("Client") or "")[:120], playback_state, int(paused), position))
      db.executemany("""INSERT INTO samples(ts,day,user_id,user_name,session_id,item_id,item_name,
        series_name,item_type,device,client,state,paused,position_ticks)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",rows)
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
        daily_users = db.execute(
            "SELECT day,COUNT(DISTINCT user_id) users FROM samples WHERE ts>=? GROUP BY day",
            (since,)
        ).fetchall()
        daily_peaks = db.execute(
            "SELECT day,MAX(n) peak FROM (SELECT day,ts,COUNT(DISTINCT user_id) n "
            "FROM samples WHERE ts>=? GROUP BY day,ts) GROUP BY day",
            (since,)
        ).fetchall()
        peak_by_day = {row["day"]: row["peak"] for row in daily_peaks}
        for row in daily_users:
            points.append({"day": row["day"], "users": row["users"],
                           "peak": peak_by_day.get(row["day"], 0)})
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
        recent_since = now - max(INTERVAL * 3, 180)
        latest_sessions = db.execute("""SELECT s.* FROM samples s
          JOIN (SELECT session_id,MAX(id) id FROM samples
                WHERE ts>=? GROUP BY session_id) latest ON latest.id=s.id
          ORDER BY s.ts DESC LIMIT 100""", (recent_since,)).fetchall()
    active_sessions = [{
      "user": r["user_name"], "state": r["state"], "paused": bool(r["paused"]),
      "item": r["item_name"], "series": r["series_name"], "device": r["device"],
      "client": r["client"], "last_seen": r["ts"]
    } for r in latest_sessions]
    return {"configured":True,"period":period,"interval_seconds":INTERVAL,"data_points":points,
      "movies":movies,"users":users,"active_sessions":active_sessions,
      "active_session_count":len(active_sessions),"sample_count":total,"first_sample":first,
      "note":"Session presence is based on the latest collector sample within three polling intervals; watch time is estimated from periodic samples."}
