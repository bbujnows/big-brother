"""
BB28 Wikipedia scraper.
Reads data.json, fetches the Wikipedia season page, finds new weekly results,
updates houseguest events and episode log, then writes data.json back.
"""

import json, re, sys
from datetime import date
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

DATA_FILE = Path(__file__).parent.parent / "data.json"

SCORING = {
    "hoh":           10,
    "veto":           5,
    "bbBlockbuster":  5,
    "wallHang":       5,
    "otev":           5,
    "bbComics":       5,
    "otherComp":      5,
    "nominated":     -3,
    "pickedVeto":     2,
    "takenOffBlock":  5,
    "weekSurvived":   5,
    "madeJury":      10,
    "resurrection":  15,
    "first":         60,
    "second":        40,
    "third":         20,
    "afh":           20,
}

# Wikipedia page for BB28 — update this URL once CBS announces the season
WIKI_URLS = [
    "https://en.wikipedia.org/wiki/Big_Brother_(American_season_28)",
    "https://en.wikipedia.org/wiki/Big_Brother_28_(American_TV_series)",
]

HEADERS = {"User-Agent": "BB28FantasyBot/1.0 (github.com/bbujnows/big-brother)"}


def fetch_wiki():
    for url in WIKI_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                print(f"Fetched: {url}")
                return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"Failed {url}: {e}")
    return None


def find_guest_by_name(data, name):
    name_clean = name.strip().lower()
    for hg in data["houseguests"]:
        if hg["name"].lower() == name_clean:
            return hg
        # Partial match on first name
        if hg["name"].lower().split()[0] == name_clean.split()[0]:
            return hg
    return None


def already_has_event(hg, week, event_type):
    for ev in (hg.get("events") or []):
        if ev.get("week") == week and ev.get("type") == event_type:
            return True
    return False


def add_event(data, hg, week, event_type, description, air_date=""):
    if already_has_event(hg, week, event_type):
        return False
    pts = SCORING.get(event_type, 0)
    ev = {"week": week, "type": event_type, "points": pts,
          "description": description, "addedAt": str(date.today())}
    hg.setdefault("events", []).append(ev)

    # Add to episodes log
    ep = next((e for e in data["episodes"] if e["week"] == week), None)
    if not ep:
        ep = {"week": week, "airDate": air_date, "events": []}
        data["episodes"].append(ep)
        data["episodes"].sort(key=lambda x: x["week"])
    ep.setdefault("events", []).append({
        "type": event_type, "houseguestId": hg["id"], "points": pts, "description": description
    })
    return True


def parse_competition_tables(soup, data):
    """
    Wikipedia BB pages typically have a weekly summary table with columns:
    Week | Airdate | HOH | Nominees | POV | POV Used | Evicted
    Column names vary by season — we try common patterns.
    """
    changes = 0
    tables = soup.find_all("table", class_=re.compile(r"wikitable", re.I))

    for table in tables:
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any(h in headers for h in ["hoh", "head of household", "head of\nhousehold"]):
            continue

        # Map column indices
        col = {}
        for i, h in enumerate(headers):
            if "week" in h:               col["week"]     = col.get("week", i)
            if "hoh" in h or "head" in h: col["hoh"]      = i
            if "nomin" in h:              col["nominated"] = i
            if "pov" in h or "veto" in h: col["veto"]     = col.get("veto", i)
            if "evict" in h:              col["evicted"]   = i
            if "air" in h or "date" in h: col["date"]      = col.get("date", i)

        if "hoh" not in col:
            continue

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue

            def cell_text(key):
                if key not in col or col[key] >= len(cells):
                    return ""
                return cells[col[key]].get_text(separator=", ", strip=True)

            # Determine week number
            week_str = cell_text("week").strip()
            week_num = None
            m = re.search(r"\d+", week_str)
            if m:
                week_num = int(m.group())
            if not week_num:
                continue

            air_date = cell_text("date")

            # HOH
            hoh_name = cell_text("hoh").split(",")[0].strip()
            hoh_hg = find_guest_by_name(data, hoh_name)
            if hoh_hg:
                if add_event(data, hoh_hg, week_num, "hoh", f"Won Head of Household (Week {week_num})", air_date):
                    changes += 1

            # Nominees
            nom_text = cell_text("nominated")
            if nom_text:
                for name in re.split(r",|&|and", nom_text):
                    name = name.strip()
                    hg = find_guest_by_name(data, name)
                    if hg:
                        if add_event(data, hg, week_num, "nominated", f"Nominated for eviction (Week {week_num})", air_date):
                            changes += 1

            # Veto winner (first name listed, or just the POV cell)
            veto_text = cell_text("veto")
            if veto_text:
                veto_name = veto_text.split(",")[0].strip()
                veto_hg = find_guest_by_name(data, veto_name)
                if veto_hg:
                    if add_event(data, veto_hg, week_num, "veto", f"Won Power of Veto (Week {week_num})", air_date):
                        changes += 1

            # Evicted
            evicted_text = cell_text("evicted")
            if evicted_text:
                evicted_name = evicted_text.split(",")[0].strip()
                evicted_hg = find_guest_by_name(data, evicted_name)
                if evicted_hg and evicted_hg.get("status") == "active":
                    evicted_hg["status"] = "evicted"
                    evicted_hg["weekEvicted"] = week_num

    return changes


def add_weekly_survival_points(data):
    """Give +5 per week survived to all active/jury houseguests."""
    changes = 0
    max_week = max((ep["week"] for ep in data["episodes"]), default=0)
    if max_week == 0:
        return 0
    for hg in data["houseguests"]:
        weeks_survived = hg.get("weekEvicted") or max_week
        current_survived = len([e for e in (hg.get("events") or []) if e.get("type") == "weekSurvived"])
        for w in range(current_survived + 1, weeks_survived + 1):
            if add_event(data, hg, w, "weekSurvived", f"Survived Week {w}"):
                changes += 1
    return changes


def main():
    if not DATA_FILE.exists():
        print(f"data.json not found at {DATA_FILE}")
        sys.exit(1)

    with open(DATA_FILE) as f:
        data = json.load(f)

    if not data.get("houseguests"):
        print("No houseguests in data.json yet — nothing to update.")
        return

    soup = fetch_wiki()
    if not soup:
        print("Could not fetch Wikipedia page. Skipping update.")
        return

    changes = parse_competition_tables(soup, data)
    changes += add_weekly_survival_points(data)
    data["lastUpdated"] = str(date.today())

    if changes > 0:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Updated data.json with {changes} new event(s).")
    else:
        print("No new events found.")


if __name__ == "__main__":
    main()
