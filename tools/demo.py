#!/usr/bin/env python3
"""Run the web page on made-up data: no car, no Tesla account, nothing sent anywhere.

    python3 tools/demo.py            # then open http://127.0.0.1:9878/

A week of drives around San Francisco, two home charges, some parked drain, and a year of
Supercharger sessions. Uses a throwaway config + database in a temp folder.
"""
import gzip
import json
import os
import pathlib
import random
import sys
import tempfile
import time
import datetime as dt

TMP = pathlib.Path(tempfile.mkdtemp(prefix="tesla-demo-"))
os.environ["TESLA_CONFIG_DIR"] = str(TMP)                        # never read the real config
(TMP / "config.toml").write_text(f'name = "Demo X"\nlisten = "127.0.0.1:9878"\ndata_dir = "{TMP}"\n'
                                 f'vin = "DEMO00000000000000"\nhome_rate_label = "Utility"\n')
(TMP / "client_id").write_text("demo")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tesla_service as ts  # noqa: E402
import webapp  # noqa: E402

random.seed(7)
HOME = (37.7599, -122.4148)       # Mission District
WORK = (37.7946, -122.3999)       # Financial District
c = ts.db()
now = time.time()
odo, batt = 41250.0, 78.0


def read(t, mode, lat, lon, **kw):
    cs = {"battery_level": kw.get("b", batt), "battery_range": kw.get("b", batt) * 2.9,
          "charging_state": kw.get("cs", "Disconnected"), "charger_power": kw.get("kw", 0),
          "charge_energy_added": kw.get("kwh", 0), "charger_voltage": kw.get("v", 0),
          "charger_actual_current": kw.get("a", 0), "charge_limit_soc": 80, "charge_current_request": 32,
          "charge_current_request_max": 48, "scheduled_charging_mode": "Off"}
    raw = {"display_name": "Demo X", "charge_state": cs,
           "drive_state": {"latitude": lat, "longitude": lon, "speed": kw.get("spd"),
                           "shift_state": "D" if mode == "driving" else None},
           "vehicle_state": {"odometer": odo, "locked": True, "sentry_mode": False, "car_version": "2026.38.6",
                             "tpms_pressure_fl": 2.9, "tpms_pressure_fr": 2.92, "tpms_pressure_rl": 2.95,
                             "tpms_pressure_rr": 2.88, "df": 0, "pf": 0, "dr": 0, "pr": 0, "ft": 0, "rt": 0},
           "climate_state": {"inside_temp": 18, "outside_temp": 14, "driver_temp_setting": 21,
                             "is_climate_on": False, "cabin_overheat_protection": "On"},
           "vehicle_config": {"car_type": "modelx", "trim_badging": "100d"}}
    f = ts.flatten(raw)
    c.execute(f"INSERT INTO reads(ts, mode, {', '.join(ts.READ_COLS)}, raw) VALUES "
              f"({', '.join('?' * (len(ts.READ_COLS) + 3))})",
              [int(t), mode, *[f[k] for k in ts.READ_COLS], gzip.compress(json.dumps(raw).encode())])


def drive(t0, a, b):
    global odo, batt
    for k in range(1, 8):
        frac = k / 7
        odo += 0.7
        batt -= 0.4
        read(t0 + k * 120, "driving", a[0] + (b[0] - a[0]) * frac + random.uniform(-.002, .002),
             a[1] + (b[1] - a[1]) * frac + random.uniform(-.002, .002), spd=random.randint(12, 38))
    read(t0 + 1000, "parked", *b)


read(now - 8 * 86400, "parked", *HOME)
for day in range(7):
    base = now - (7 - day) * 86400
    batt -= 0.5
    read(base + 7 * 3600, "parked", *HOME)
    drive(base + 8 * 3600, HOME, WORK)
    drive(base + 17.5 * 3600, WORK, HOME)
    if day in (2, 5):
        kwh = 0
        for k in range(5):
            kwh += 8
            batt = min(80, batt + 7)
            read(base + 19 * 3600 + k * 1800, "charging", *HOME, cs="Charging", kw=7.6, kwh=kwh, v=240, a=32)
        read(base + 22 * 3600, "parked", *HOME, cs="Complete", kwh=kwh)
    for h in range(0, 24):
        c.execute("INSERT INTO states VALUES(?,?)",
                  (int(base + h * 3600), "online" if h in (8, 17, 18, 19, 20) else "asleep"))

SITES = [("San Francisco, CA - Folsom Street", 0.45), ("Daly City, CA", 0.39),
         ("San Mateo, CA - Bovet Road", 0.42), ("Oakland, CA - Broadway", 0.48), ("Fremont, CA", 0.34)]
for i in range(70):
    start = dt.datetime.fromtimestamp(now - i * 5.2 * 86400 - random.randint(0, 40000)).astimezone()
    site, rate = random.choice(SITES)
    rate = round(rate + random.choice([-0.08, 0, 0, 0.06]), 2)
    kwh = round(random.uniform(18, 62), 2)
    mins = int(kwh / random.uniform(1.3, 1.9))
    stop = start + dt.timedelta(minutes=mins)
    s = {"sessionId": 9000 + i, "siteLocationName": site, "chargeStartDateTime": start.isoformat(),
         "chargeStopDateTime": stop.isoformat(), "unlatchDateTime": (stop + dt.timedelta(minutes=2)).isoformat(),
         "fees": [{"feeType": "CHARGING", "pricingType": "NO_CHARGE", "rateBase": rate, "usageBase": kwh,
                   "usageTier1": 2, "usageTier2": mins - 2, "uom": "kwh", "totalDue": 0, "currencyCode": "USD"},
                  {"feeType": "CONGESTION", "pricingType": "NO_CHARGE", "rateBase": 0.5, "usageBase": 0, "uom": "min"}]}
    if i in (12, 40):
        s["fees"].append({"feeType": "PARKING", "pricingType": "PAYMENT", "rateBase": 0.5, "usageBase": 6,
                          "uom": "min", "totalDue": 3.0})
    c.execute("INSERT INTO sc_sessions VALUES(?,?,?)", (str(s["sessionId"]), s["chargeStartDateTime"], json.dumps(s)))
ts.add_event(c, "wake", "no reading for 24 h: sent wake_up (HTTP 200)")
c.execute("INSERT INTO settings VALUES('sc_last_sync', ?)", (json.dumps(now),))
c.commit()


class DemoTesla(ts.Tesla):
    """Never touches the network."""
    def __init__(self, store):
        self.db = store

    def refresh_if_needed(self, force=False):
        pass

    def request(self, *a, **k):
        return 408, None

    def state(self):
        return "asleep"

    def wake(self):
        return 200


ts.Tesla = DemoTesla
poller = ts.Poller()
poller.mode, poller.next_poll_at = "asleep", time.time() + 1500
webapp.setup(ts, poller)
from http.server import ThreadingHTTPServer  # noqa: E402

print(f"Demo running: http://127.0.0.1:9878/   (data in {TMP})", flush=True)
ThreadingHTTPServer(ts.METRICS_ADDR, ts._Handler).serve_forever()
