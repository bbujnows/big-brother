"""One-off: log the Episode 1 safety winners (Week 1) to Firebase.
Matches the exact event shape admin.js addEvent() writes. Safe to re-run (skips duplicates).
"""

import json
import urllib.request
from datetime import datetime, timezone

FB = "https://bb28-fantasy-default-rtdb.firebaseio.com/gameData.json"

EVENTS = [
    ("Rome Seymour",  "Won safety — 1988 mall comp (premiere)"),
    ("Chuk Anyanwu",  "Won safety — 2018 Fiji jungle comp (premiere)"),
    ("Jason De Puy",  "Won safety — 2010 Vegas slushy comp (premiere)"),
]
WEEK = 1
AIR_DATE = "2026-07-09"


def main():
    with urllib.request.urlopen(FB) as r:
        data = json.load(r)

    now = datetime.now(timezone.utc).isoformat()
    added = 0

    episodes = data.get("episodes") or []
    ep = next((e for e in episodes if e.get("week") == WEEK), None)
    if not ep:
        ep = {"week": WEEK, "airDate": AIR_DATE, "events": []}
        episodes.append(ep)
        episodes.sort(key=lambda e: e["week"])
    if not ep.get("airDate"):
        ep["airDate"] = AIR_DATE
    ep["events"] = ep.get("events") or []

    for name, desc in EVENTS:
        hg = next((h for h in data["houseguests"] if h["name"] == name), None)
        if not hg:
            print(f"NOT FOUND: {name}")
            continue
        hg_events = hg.get("events") or []
        if any(e.get("week") == WEEK and e.get("type") == "safety" for e in hg_events):
            print(f"skip (already logged): {name}")
            continue
        pts = data["scoring"]["safety"]["points"]
        hg_events.append({"week": WEEK, "type": "safety", "points": pts,
                          "description": desc, "addedAt": now})
        hg["events"] = hg_events
        ep["events"].append({"type": "safety", "houseguestId": hg["id"],
                             "points": pts, "description": desc})
        print(f"logged: {name} +{pts} ({desc})")
        added += 1

    if added == 0:
        print("Nothing to write.")
        return

    data["episodes"] = episodes
    body = json.dumps(data).encode()
    req = urllib.request.Request(FB, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        r.read()
    print(f"Saved {added} event(s) to Firebase.")


if __name__ == "__main__":
    main()
