"""Derived views over the poller's readings: trips, battery history, parked drain, awake time.

All read-only over tesla.db. Readings are sparse (the car is only read once it has stayed awake
~30 min, then every few minutes while driving), so trips start late; the reading taken before a
trip (where the car was parked) supplies the true start odometer, battery and place.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import statistics

import charging

TRIP_GAP = 20 * 60          # driving readings further apart than this are separate trips


def _con(db_path: pathlib.Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _reads(db_path, since: float) -> list[dict]:
    with _con(db_path) as c:
        # One reading before the window too, so a trip that starts at the edge has its baseline.
        prev = c.execute("SELECT ts FROM reads WHERE ts < ? ORDER BY ts DESC LIMIT 1", (since,)).fetchone()
        start = prev["ts"] if prev else since
        return [dict(r) for r in c.execute(
            "SELECT ts, mode, battery_level, battery_range, charging_state, odometer, latitude, longitude, "
            "speed, shift_state, outside_temp FROM reads WHERE ts >= ? ORDER BY ts", (start,))]


def track(db_path, since: float, until: float | None = None) -> list[list]:
    with _con(db_path) as c:
        rows = c.execute("SELECT ts, latitude, longitude, speed, mode FROM reads WHERE ts >= ? AND ts <= ? "
                         "AND latitude IS NOT NULL ORDER BY ts", (since, until or 9e12)).fetchall()
    return [[r["ts"], r["latitude"], r["longitude"], r["speed"], r["mode"]] for r in rows]


def trips(db_path, days: float, pack_kwh: float) -> list[dict]:
    since = dt.datetime.now().timestamp() - days * 86400
    rs = _reads(db_path, since)
    places = charging._places()
    out, i = [], 0
    while i < len(rs):
        if rs[i]["mode"] != "driving":
            i += 1
            continue
        j = i
        while j + 1 < len(rs) and rs[j + 1]["mode"] == "driving" and rs[j + 1]["ts"] - rs[j]["ts"] <= TRIP_GAP:
            j += 1
        base = rs[i - 1] if i > 0 else None
        end = rs[j + 1] if j + 1 < len(rs) and rs[j + 1]["ts"] - rs[j]["ts"] <= TRIP_GAP * 2 else rs[j]
        drive = rs[i:j + 1]
        first = drive[0]
        odo0 = (base or first)["odometer"]
        dist = (end["odometer"] - odo0) if end["odometer"] is not None and odo0 is not None else None
        seen = (end["odometer"] - first["odometer"]) if end["odometer"] and first["odometer"] else None
        b0 = (base or first)["battery_level"]
        used = (b0 - end["battery_level"]) if b0 is not None and end["battery_level"] is not None else None
        if base and base["charging_state"] not in (None, "Disconnected"):
            used = None                       # was plugged in before: the start % is not the trip's
        wh_mi = (used / 100 * pack_kwh * 1000 / dist) if used and dist and dist > 0.5 and used > 0 else None
        pts = [[r["latitude"], r["longitude"]] for r in ([base] if base else []) + drive + ([end] if end is not drive[-1] else [])
               if r and r["latitude"] is not None]
        start_ll = (base or first)
        out.append({
            "start": first["ts"], "end": end["ts"], "baseline_ts": base["ts"] if base else None,
            "from": charging._place(start_ll["latitude"], start_ll["longitude"], places),
            "to": charging._place(end["latitude"], end["longitude"], places),
            "from_ll": [start_ll["latitude"], start_ll["longitude"]],
            "to_ll": [end["latitude"], end["longitude"]],
            "miles": round(dist, 1) if dist is not None else None,
            "miles_seen": round(seen, 1) if seen is not None else None,
            "battery_start": b0, "battery_end": end["battery_level"],
            "pct_used": round(used, 1) if used is not None else None,
            "wh_per_mile": round(wh_mi) if wh_mi else None,
            "max_mph": max((r["speed"] or 0) for r in drive),
            "outside_temp": first["outside_temp"],
            "readings": len(drive), "points": pts,
        })
        i = j + 1
    return list(reversed(out))


def battery(db_path, days: float) -> dict:
    since = dt.datetime.now().timestamp() - days * 86400
    rs = [r for r in _reads(db_path, since) if r["battery_level"] is not None]
    series = [[r["ts"], r["battery_level"], r["battery_range"],
               round(r["battery_range"] / r["battery_level"] * 100, 1) if r["battery_range"] and r["battery_level"] else None]
              for r in rs if r["ts"] >= since]
    drains = []
    for a, b in zip(rs, rs[1:]):
        hours = (b["ts"] - a["ts"]) / 3600
        unplugged = a["charging_state"] in (None, "Disconnected") and b["charging_state"] in (None, "Disconnected")
        still = a["odometer"] is not None and b["odometer"] is not None and abs(b["odometer"] - a["odometer"]) < 0.2
        if hours >= 3 and unplugged and still and b["battery_level"] <= a["battery_level"]:
            drains.append({"from": a["ts"], "to": b["ts"], "hours": round(hours, 1),
                           "pct_per_day": round((a["battery_level"] - b["battery_level"]) / hours * 24, 2)})
    with _con(db_path) as c:
        states = c.execute("SELECT ts, state FROM states WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    awake: dict[str, list[int]] = {}
    for s in states:
        d = dt.date.fromtimestamp(s["ts"]).isoformat()
        awake.setdefault(d, [0, 0])
        awake[d][0] += s["state"] == "online"
        awake[d][1] += 1
    full = [s[3] for s in series if s[3]]
    return {
        "series": series,
        "drains": drains[-30:],
        "drain_median": statistics.median([d["pct_per_day"] for d in drains]) if drains else None,
        "awake_by_day": [[d, round(v[0] / v[1] * 100)] for d, v in sorted(awake.items())],
        "range_at_100_latest": full[-1] if full else None,
        "range_at_100_first": full[0] if full else None,
    }


def activity(db_path, days: float) -> list[dict]:
    since = dt.datetime.now().timestamp() - days * 86400
    with _con(db_path) as c:
        ev = [{"ts": r["ts"], "kind": r["kind"], "text": r["message"]}
              for r in c.execute("SELECT * FROM events WHERE ts >= ?", (since,))]
        al = [{"ts": r["ts"], "kind": "alert", "text": f"{r['message']} — email {r['result']}"}
              for r in c.execute("SELECT * FROM alerts WHERE ts >= ?", (since,))]
        rows = c.execute("SELECT ts, mode, battery_level FROM reads WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    # Back-to-back readings in the same state become one line.
    rd, run = [], []

    def flush():
        if not run:
            return
        a, b = run[0], run[-1]
        pct = (f", {a['battery_level']:.0f}%" + (f" → {b['battery_level']:.0f}%" if len(run) > 1 and
               round(a["battery_level"]) != round(b["battery_level"]) else "")) if a["battery_level"] is not None else ""
        what = {"driving": "while driving", "charging": "while charging", "parked": "parked"}.get(a["mode"], a["mode"])
        rd.append({"ts": a["ts"], "kind": "read",
                   "text": (f"{len(run)} readings {what}" if len(run) > 1 else f"Read the car: {what}") + pct
                   + (f" (until {dt.datetime.fromtimestamp(b['ts']).strftime('%H:%M')})" if len(run) > 1 else "")})

    for r in rows:
        if run and (r["mode"] != run[-1]["mode"] or r["ts"] - run[-1]["ts"] > 3 * 3600):
            flush()
            run = []
        run.append(r)
    flush()
    return sorted(ev + al + rd, key=lambda e: e["ts"], reverse=True)[:500]
