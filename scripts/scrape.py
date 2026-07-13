"""
BB28 Wikipedia scraper.

Fetches the Wikipedia season page, parses the results grid (weeks as COLUMNS,
event types as rows), and writes new scoring events to the live Firebase
database that the website reads. Also writes data.json as a versioned backup.

Spoiler gate: Wikipedia editors fill in results from the live feeds BEFORE
episodes air on CBS. Each event type only publishes once the episode that
reveals it has finished airing (Wed/Thu/Sun cadence — see REVEAL_CADENCE).

Usage:
    python scripts/scrape.py            # scrape and publish
    python scripts/scrape.py --dry-run  # show what would publish, write nothing
"""

import json, re, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

DATA_FILE = Path(__file__).parent.parent / "data.json"
FIREBASE_URL = "https://bb28-fantasy-default-rtdb.firebaseio.com/gameData.json"
WIKI_URL = "https://en.wikipedia.org/wiki/Big_Brother_28_(American_season)"
HEADERS = {"User-Agent": "BB28FantasyBot/1.0 (github.com/bbujnows/big-brother)"}

# ── Spoiler gate configuration ────────────────────────────────────────────
# BB28 runs July-September, entirely within Eastern Daylight Time (UTC-4).
ET = timezone(timedelta(hours=-4))
REVEAL_HOUR = (22, 5)  # an episode counts as aired at 10:05 PM ET that night

# Week 1 broadcast dates; week N = these + 7*(N-1) days.
WEEK1_DATES = {
    "sun": date(2026, 7, 12),
    "wed": date(2026, 7, 15),
    "thu": date(2026, 7, 16),
}

# Which night of the cycle reveals each event type on CBS.
# Best guess at BB28's cadence — adjust these as we observe the real pattern.
REVEAL_CADENCE = {
    "hoh":            "sun",  # HOH comp airs Sunday
    "nominated":      "sun",  # nomination ceremony airs Sunday
    "veto":           "wed",  # veto comp + ceremony air Wednesday
    "takenOffBlock":  "wed",
    "replacementNom": "wed",  # replacement nominee revealed at veto ceremony
    "bbBlockbuster":  "thu",
    "evicted":        "thu",  # live eviction Thursday
}

# Fallback points if a type is missing from the database's scoring config.
SCORING_FALLBACK = {
    "hoh": 10, "veto": 5, "bbBlockbuster": 5, "nominated": -3,
    "takenOffBlock": 5, "safety": 3, "madeJury": 10,
}


def week_date(week, day_key):
    return WEEK1_DATES[day_key] + timedelta(weeks=week - 1)


def is_aired(week, event_type, now_et=None):
    """Has the episode that reveals this event finished airing?"""
    day_key = REVEAL_CADENCE.get(event_type, "thu")  # unknown types wait for Thursday (safest)
    d = week_date(week, day_key)
    reveal_at = datetime(d.year, d.month, d.day, *REVEAL_HOUR, tzinfo=ET)
    now = now_et or datetime.now(ET)
    return now >= reveal_at


# ── Fetch ─────────────────────────────────────────────────────────────────
def fetch_wiki():
    try:
        r = requests.get(WIKI_URL, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            print(f"Fetched: {WIKI_URL}")
            return BeautifulSoup(r.text, "html.parser")
        print(f"Wikipedia returned HTTP {r.status_code}")
    except Exception as e:
        print(f"Failed to fetch Wikipedia: {e}")
    return None


def fetch_firebase():
    r = requests.get(FIREBASE_URL, timeout=20)
    r.raise_for_status()
    return r.json()


def push_firebase(data):
    r = requests.put(FIREBASE_URL, json=data, timeout=20)
    r.raise_for_status()


# ── Table parsing (weeks as columns) ──────────────────────────────────────
def expand_table(table):
    """Expand a <table> into a 2D matrix of cells, resolving rowspan/colspan."""
    grid = {}
    for row_i, tr in enumerate(table.find_all("tr")):
        col_i = 0
        for cell in tr.find_all(["td", "th"]):
            while (row_i, col_i) in grid:
                col_i += 1
            try:
                rs = int(cell.get("rowspan") or 1)
            except ValueError:
                rs = 1
            try:
                cs = int(cell.get("colspan") or 1)
            except ValueError:
                cs = 1
            for r in range(rs):
                for c in range(cs):
                    grid[(row_i + r, col_i + c)] = cell
            col_i += cs
    if not grid:
        return []
    n_rows = max(r for r, _ in grid) + 1
    n_cols = max(c for _, c in grid) + 1
    return [[grid.get((r, c)) for c in range(n_cols)] for r in range(n_rows)]


def cell_text(cell):
    if cell is None:
        return ""
    t = cell.get_text("\n", strip=True)
    return re.sub(r"\[[^\]]*\]", "", t).strip()  # drop footnote refs like [a]


PLACEHOLDERS = {"none", "no nominations", "no nominees", "not used", "n/a", "tbd", "tba", ""}


def cell_names(cell):
    """Split a grid cell into individual houseguest names."""
    text = cell_text(cell)
    names = []
    for part in re.split(r"[\n,]| & | and ", text):
        part = re.sub(r"\s*\d[\d\s:to&–—-]*$", "", part).strip()  # strip vote tallies
        part = part.strip("()")
        if part and part.lower() not in PLACEHOLDERS:
            names.append(part)
    return names


def find_results_table(soup):
    """Find the voting-history-style grid: rows labeled HOH/Nominations/etc."""
    for table in soup.find_all("table", class_=re.compile(r"wikitable", re.I)):
        matrix = expand_table(table)
        labels = " | ".join(cell_text(row[0]).lower() for row in matrix if row)
        if "head of household" in labels and "evicted" in labels:
            return matrix
    return None


ROW_TYPES = [
    (re.compile(r"head of household", re.I), "hoh"),
    (re.compile(r"nominations.*initial|initial.*nominations", re.I), "noms_initial"),
    (re.compile(r"nominations.*final|final.*nominations", re.I), "noms_final"),
    (re.compile(r"veto", re.I), "veto"),
    (re.compile(r"block\s*buster", re.I), "blockbuster"),
    (re.compile(r"^evicted", re.I), "evicted"),
]


def parse_weeks(matrix):
    """Return {week_number: {category: set(names)}} from the expanded grid."""
    # Map column index -> week number from the header row containing "Week N" cells
    col_week = {}
    for row in matrix:
        hits = {}
        for i, cell in enumerate(row):
            m = re.search(r"week\s*(\d+)", cell_text(cell), re.I)
            if m:
                hits[i] = int(m.group(1))
        if len(set(hits.values())) >= 2:
            col_week = hits
            break
    if not col_week:
        print("Could not find a 'Week N' header row in the results table.")
        return {}

    weeks = {}
    seen_noms_plain = False
    for row in matrix:
        label = re.sub(r"\s+", " ", cell_text(row[0]))
        category = None
        for pattern, cat in ROW_TYPES:
            if pattern.search(label):
                category = cat
                break
        # A row labeled just "Nominations" (no initial/final split) counts as initial
        if category is None and re.fullmatch(r"nominations?", label, re.I) and not seen_noms_plain:
            category = "noms_initial"
            seen_noms_plain = True
        if category is None:
            continue
        for i, cell in enumerate(row):
            wk = col_week.get(i)
            if wk is None or cell is row[0]:
                continue
            bucket = weeks.setdefault(wk, {})
            bucket.setdefault(category, set()).update(cell_names(cell))
    return weeks


# ── Roster matching / event helpers ───────────────────────────────────────
def _norm(s):
    return re.sub(r"[^a-z]", "", s.lower())


def find_guest_by_name(data, name):
    """Match a scraped name against the roster by full name, first or last name.
    Returns None on no match or an ambiguous match."""
    n = _norm(name)
    if not n:
        return None
    matches = []
    for hg in data["houseguests"]:
        tokens = hg["name"].split()
        candidates = {_norm(hg["name"])} | {_norm(t) for t in tokens}
        if n in candidates:
            matches.append(hg)
    return matches[0] if len(matches) == 1 else None


def get_points(data, event_type):
    cfg = (data.get("scoring") or {}).get(event_type)
    if cfg and isinstance(cfg.get("points"), (int, float)):
        return cfg["points"]
    return SCORING_FALLBACK.get(event_type, 0)


def already_has_event(hg, week, event_type):
    for ev in (hg.get("events") or []):
        if ev.get("week") == week and ev.get("type") == event_type:
            return True
    return False


def add_event(data, hg, week, event_type, description):
    if already_has_event(hg, week, event_type):
        return False
    pts = get_points(data, event_type)
    hg["events"] = hg.get("events") or []
    hg["events"].append({
        "week": week, "type": event_type, "points": pts,
        "description": description, "addedAt": datetime.now(timezone.utc).isoformat(),
    })

    data["episodes"] = data.get("episodes") or []
    ep = next((e for e in data["episodes"] if e.get("week") == week), None)
    if not ep:
        ep = {"week": week, "airDate": str(week_date(week, "sun")), "events": []}
        data["episodes"].append(ep)
        data["episodes"].sort(key=lambda x: x["week"])
    ep["events"] = ep.get("events") or []
    ep["events"].append({
        "type": event_type, "houseguestId": hg["id"], "points": pts, "description": description,
    })
    return True


# ── Apply scraped results ─────────────────────────────────────────────────
def apply_week(data, week, results, published, held, unmatched):
    def resolve(names):
        out = []
        for name in names:
            hg = find_guest_by_name(data, name)
            if hg:
                out.append(hg)
            else:
                unmatched.add(name)
        return out

    def emit(names, event_type, desc_fn, gate_type=None):
        gate = gate_type or event_type
        if not names:
            return
        if not is_aired(week, gate):
            held.append((week, gate, len(names)))
            return
        for hg in resolve(names):
            if add_event(data, hg, week, event_type, desc_fn(hg)):
                pts = get_points(data, event_type)
                published.append(f"Week {week}: {hg['name']} {'+' if pts >= 0 else ''}{pts} ({event_type})")

    emit(results.get("hoh", ()), "hoh", lambda h: f"Won Head of Household (Week {week})")
    emit(results.get("noms_initial", ()), "nominated", lambda h: f"Nominated for eviction (Week {week})")
    emit(results.get("veto", ()), "veto", lambda h: f"Won Power of Veto (Week {week})")
    emit(results.get("blockbuster", ()), "bbBlockbuster", lambda h: f"Won BB Blockbuster (Week {week})")

    # Compare initial vs final nominations: saved / replacement nominees
    initial = results.get("noms_initial") or set()
    final = results.get("noms_final") or set()
    if final:
        init_ids = {h["id"]: h for h in resolve(initial)}
        final_ids = {h["id"]: h for h in resolve(final)}
        saved = [h for hid, h in init_ids.items() if hid not in final_ids]
        replacements = [h for hid, h in final_ids.items() if hid not in init_ids]
        emit([h["name"] for h in saved], "takenOffBlock",
             lambda h: f"Taken off the block (Week {week})")
        emit([h["name"] for h in replacements], "nominated",
             lambda h: f"Named replacement nominee (Week {week})", gate_type="replacementNom")

    # Evictions: status change only (no points)
    evicted = results.get("evicted") or set()
    if evicted:
        if not is_aired(week, "evicted"):
            held.append((week, "evicted", len(evicted)))
        else:
            for hg in resolve(evicted):
                if hg.get("status") == "active":
                    hg["status"] = "evicted"
                    hg["weekEvicted"] = week
                    published.append(f"Week {week}: {hg['name']} marked evicted")


def main():
    dry_run = "--dry-run" in sys.argv

    try:
        data = fetch_firebase()
    except Exception as e:
        print(f"Could not read Firebase: {e}")
        sys.exit(1)
    if not data or not data.get("houseguests"):
        print("No houseguests in Firebase data — nothing to update.")
        return

    soup = fetch_wiki()
    if not soup:
        print("Could not fetch Wikipedia page. Skipping update.")
        return

    matrix = find_results_table(soup)
    if not matrix:
        print("No results grid on the Wikipedia page yet. Skipping update.")
        return

    weeks = parse_weeks(matrix)
    if not weeks:
        print("Results grid found but no week columns parsed. Skipping update.")
        return
    print(f"Parsed results grid: weeks {sorted(weeks)}")

    published, held, unmatched = [], [], set()
    for week in sorted(weeks):
        apply_week(data, week, weeks[week], published, held, unmatched)

    for line in published:
        print(f"PUBLISH  {line}")
    # Held items are logged WITHOUT names so even the Action log stays spoiler-free
    for week, gate, count in held:
        print(f"HELD     Week {week}: {count} {gate} result(s) — episode hasn't aired yet")
    for name in sorted(unmatched):
        print(f"UNMATCHED name on Wikipedia (not on our roster): {name}")

    if not published:
        print("No new aired events to publish.")
        return

    if dry_run:
        print(f"DRY RUN — {len(published)} event(s) would publish. Nothing written.")
        return

    data["lastUpdated"] = str(date.today())
    push_firebase(data)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Published {len(published)} update(s) to Firebase; data.json backup written.")


if __name__ == "__main__":
    main()
