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

import hashlib, json, os, re, sys
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
    "savedSelf":      "wed",  # veto winner pulls themselves off
    "replacementNom": "wed",  # replacement nominee revealed at veto ceremony
    "bbBlockbuster":  "thu",
    "evicted":        "thu",  # live eviction Thursday
    "survivedVote":   "thu",  # revealed at the same moment as the eviction
    "twistSave":      "thu",  # twist blocks resolve on the live show
    "evictionPower":  "thu",  # ...as does who was holding the power
}

# ── The scoring board (league-voted 2026-08-05) ───────────────────────────
# This is the source of truth. Each run syncs it into the database and
# rescores every existing event to match, so rule changes apply
# retroactively without any manual re-entry. An event carrying
# "lockPoints": true keeps its stored value (manual admin overrides).
SCORING_BOARD = {
    "hoh":           ("Head of Household Win",            10),
    "veto":          ("Veto Win",                          3),
    "bbBlockbuster": ("BB Blockbuster Win",                5),
    "wallHang":      ("Wall Hang Win",                     3),
    "otev":          ("OTEV Win",                          2),
    "bbComics":      ("BB Comics Win",                     2),
    "safety":        ("Safety for the Week",               3),
    "nominated":     ("Nominated for Eviction",           -3),
    "survivedVote":  ("Survived Eviction Vote",            4),
    "pickedVeto":    ("Picked to Play in Veto",            2),
    "takenOffBlock": ("Taken Off Block by Someone Else",   5),
    "savedSelf":     ("Took Self Off Block (Veto Only)",   2),
    # Twist nights: a mass block nobody was nominated onto by an HOH, cleared
    # by the twist itself rather than by a veto holder choosing you. Worth
    # less than takenOffBlock (+5), which is earned social capital.
    "twistSave":     ("Saved from a Twist Block",           3),
    # Handed unilateral control over who goes home. Comp-win-tier impact, so
    # it pays like one.
    "evictionPower": ("Unilateral Eviction Power",          5),
    "madeJury":      ("Made It to Jury",                  10),
    "resurrection":  ("Resurrection / Battle Back Win",   15),
    "first":         ("Winner (1st Place)",               60),
    "second":        ("Runner-Up (2nd Place)",            40),
    "third":         ("3rd Place",                        20),
    "afh":           ("America's Favorite Houseguest",    25),
}

SCORING_FALLBACK = {k: pts for k, (_, pts) in SCORING_BOARD.items()}


def sync_scoring_board(data):
    """Write the canonical board into the data; returns changed labels/points."""
    scoring = data.setdefault("scoring", {})
    changes = []
    for key, (label, pts) in SCORING_BOARD.items():
        cur = scoring.get(key) or {}
        if cur.get("points") != pts or cur.get("label") != label:
            was = f"{cur.get('points')}" if cur else "new"
            scoring[key] = {"label": label, "points": pts}
            changes.append(f"{key}: {was} -> {pts:+d} ({label})")
    return changes


def resync_event_points(data):
    """Rescore stored events to the current board (retroactive rule changes)."""
    lines = []
    for hg in data.get("houseguests") or []:
        for ev in hg.get("events") or []:
            if ev.get("lockPoints"):
                continue
            want = get_points(data, ev.get("type"))
            if ev.get("points") == want:
                continue
            old = ev.get("points")
            ev["points"] = want
            for ep in data.get("episodes") or []:
                if ep.get("week") != ev.get("week"):
                    continue
                for le in ep.get("events") or []:
                    if (le.get("type") == ev.get("type")
                            and le.get("houseguestId") == hg["id"]
                            and not le.get("lockPoints")):
                        le["points"] = want
            lines.append(f"Week {ev.get('week')}: {hg['name']} {ev.get('type')} "
                         f"{old:+d} -> {want:+d}")
    return lines


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


STRUCK_TAGS = ("s", "del", "strike")


def cell_names_split(cell):
    """Return (live_names, struck_names) for a grid cell.

    Wikipedia strikes through a name that was on a list and then came off it —
    on a twist night the nominations cell holds the whole block with the saved
    houseguests struck out. Reading the raw text treats those saved players as
    live nominees, which is what scrambled Week 8: everyone stayed 'on the
    block' and the veto maths found nobody to credit with a save."""
    if cell is None:
        return [], []
    clone = BeautifulSoup(str(cell), "html.parser")
    struck = []
    for node in clone.find_all(STRUCK_TAGS):
        struck.extend(cell_names(node))
        node.decompose()
    return cell_names(clone), struck


EVICT_CHOICE_RE = re.compile(r"(.+?)'s\s+choice\s+to\s+(?:evict|eliminate)", re.I)


def parse_eviction(cell):
    """Split an eviction cell into (evicted_names, chooser_name or None).

    The first line is who went home; the rest is how. Normally that reads
    '9 of 10 votes to evict', but when a twist hands one houseguest the call
    it reads "<Name>'s choice to eliminate" — and that houseguest earns the
    eviction-power points."""
    if cell is None:
        return [], None
    lines = [ln.strip() for ln in cell_text(cell).split("\n") if ln.strip()]
    if not lines or lines[0].lower() in PLACEHOLDERS:
        return [], None
    evicted = [n.strip() for n in re.split(r"[,/]| & | and ", lines[0]) if n.strip()]
    m = EVICT_CHOICE_RE.match(" ".join(lines[1:]))
    return evicted, (m.group(1).strip() if m else None)


MAX_LABEL_LEN = 40  # real row labels are short; recap prose is not


def find_results_table(soup):
    """Find the voting-history-style grid: rows labeled HOH/Nominations/etc.

    Matches on short ROW LABELS plus a 'Week N' header row. Substring
    matching alone is not enough: the episodes table appears earlier on
    the page and its recap prose mentions both 'Head of Household' and
    'evicted', so it would win a naive search."""
    for table in soup.find_all("table", class_=re.compile(r"wikitable", re.I)):
        matrix = expand_table(table)
        if not matrix:
            continue
        labels = [re.sub(r"\s+", " ", cell_text(row[0])).lower()
                  for row in matrix if row and cell_text(row[0])]
        labels = [l for l in labels if len(l) <= MAX_LABEL_LEN]
        has_hoh = any(l.startswith("head of household") for l in labels)
        has_evicted = any(l.startswith("evicted") for l in labels)
        has_weeks = any(
            len({int(m.group(1)) for cell in row
                 for m in [re.search(r"week\s*(\d+)", cell_text(cell), re.I)] if m}) >= 2
            for row in matrix)
        if has_hoh and has_evicted and has_weeks:
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


def parse_segments(matrix):
    """Return an ordered list of column segments from the expanded grid:

        [{"week": 8, "day": "Day 52", "col": 8, "cells": {category: cell}}, ...]

    One segment per COLUMN, not per week. Late in the season a single "Week N"
    header spans two day-columns (a twist night plus a regular cycle, or a
    double eviction), and each of those is a self-contained game: its own
    nominations, its own veto, its own eviction. Merging them into one bucket
    scrambles the maths — a player nominated on day one and saved on day two
    looks like they were never saved at all."""
    # Map column index -> week number from the header row containing "Week N" cells
    col_week, header_row = {}, -1
    for ri, row in enumerate(matrix):
        hits = {}
        for i, cell in enumerate(row):
            m = re.search(r"week\s*(\d+)", cell_text(cell), re.I)
            if m:
                hits[i] = int(m.group(1))
        if len(set(hits.values())) >= 2:
            col_week, header_row = hits, ri
            break
    if not col_week:
        print("Could not find a 'Week N' header row in the results table.")
        return []

    # The row under the header carries a sub-label ("Day 52", "Finale") for any
    # week that was split across columns. A column whose sub-label just repeats
    # the week name is an ordinary single-column week.
    col_day = {}
    if header_row + 1 < len(matrix):
        sub = matrix[header_row + 1]
        for i in col_week:
            if i < len(sub):
                label = re.sub(r"\s+", " ", cell_text(sub[i])).strip()
                if label and not re.fullmatch(r"week\s*\d+", label, re.I):
                    col_day[i] = label

    segments = {i: {"week": w, "day": col_day.get(i), "col": i, "cells": {}}
                for i, w in col_week.items()}

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
            if i not in segments or cell is row[0]:
                continue
            # Keep the cell itself, not its text: strikethrough markup inside it
            # is the only record of who came off a twist block.
            segments[i]["cells"].setdefault(category, cell)

    return [segments[i] for i in sorted(segments)]


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


def already_has_event(hg, week, event_type, day=None):
    # `day` is part of the identity: a week split across two day-columns can
    # legitimately nominate the same player twice, and those are two real
    # trips to the block, not a duplicate.
    for ev in (hg.get("events") or []):
        if (ev.get("week") == week and ev.get("type") == event_type
                and ev.get("day") == day):
            return True
    return False


def add_event(data, hg, week, event_type, description, day=None):
    if already_has_event(hg, week, event_type, day):
        return False
    pts = get_points(data, event_type)
    tag = {"day": day} if day else {}
    hg["events"] = hg.get("events") or []
    hg["events"].append({
        "week": week, "type": event_type, "points": pts, **tag,
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
        "type": event_type, "houseguestId": hg["id"], "points": pts, **tag,
        "description": description,
    })
    return True


# ── Apply scraped results ─────────────────────────────────────────────────
def apply_segment(data, seg, published, held, unmatched):
    """Score one column of the results grid.

    Two shapes turn up. A regular cycle has an HOH who nominates, a veto that
    may shuffle the block, and a house vote. A TWIST night has no HOH, no
    initial nominations and no veto — a mass block appears, some of it is
    struck through (saved), and somebody is sent home, sometimes by a single
    houseguest holding a power rather than by a vote."""
    week = seg["week"]
    day = seg.get("day")
    cells = seg["cells"]
    where = f"Week {week}" + (f" ({day})" if day else "")

    def names(category):
        return cell_names_split(cells.get(category))

    def resolve(raw):
        out = []
        for name in raw:
            hg = find_guest_by_name(data, name)
            if hg:
                out.append(hg)
            else:
                unmatched.add(name)
        return out

    def emit(raw, event_type, desc, gate_type=None):
        gate = gate_type or event_type
        if not raw:
            return
        if not is_aired(week, gate):
            held.append((week, gate, len(raw)))
            return
        for hg in resolve(raw):
            if add_event(data, hg, week, event_type, desc, day=day):
                pts = get_points(data, event_type)
                published.append(
                    f"{where}: {hg['name']} {'+' if pts >= 0 else ''}{pts} ({event_type})")

    hoh, _ = names("hoh")
    initial, _ = names("noms_initial")
    veto, _ = names("veto")
    blockbuster, _ = names("blockbuster")
    final_live, final_struck = names("noms_final")
    evicted, chooser = parse_eviction(cells.get("evicted"))

    # ── Twist night ──────────────────────────────────────────────────────
    # No HOH and no initial nominations, but people were still on a block.
    if not hoh and not initial and (final_live or final_struck):
        block = final_struck + final_live
        # The block was real — someone went home off it — so it costs the
        # same as any other nomination.
        emit(block, "nominated", f"Nominated in a twist ({where})",
             gate_type="evicted")
        # Struck through = pulled off the twist block.
        emit(final_struck, "twistSave", f"Saved from the twist block ({where})")
        if chooser:
            emit([chooser], "evictionPower",
                 f"Held the power to eliminate ({where})")
        if evicted:
            evicted_ids = {h["id"] for h in resolve(evicted)}
            survivors = [h["name"] for h in resolve(final_live)
                         if h["id"] not in evicted_ids]
            emit(survivors, "survivedVote", f"Survived the twist ({where})")
    # ── Regular cycle ────────────────────────────────────────────────────
    else:
        emit(hoh, "hoh", f"Won Head of Household ({where})")
        emit(initial, "nominated", f"Nominated for eviction ({where})")
        emit(veto, "veto", f"Won Power of Veto ({where})")
        emit(blockbuster, "bbBlockbuster", f"Won BB Blockbuster ({where})")

        # Compare initial vs final nominations: saved / replacement nominees
        if final_live or final_struck:
            init_ids = {h["id"]: h for h in resolve(initial)}
            final_ids = {h["id"]: h for h in resolve(final_live)}
            saved = [h for hid, h in init_ids.items() if hid not in final_ids]
            replacements = [h for hid, h in final_ids.items() if hid not in init_ids]
            # A nominee who used the veto on themselves scores savedSelf (+2);
            # anyone else pulled off by the veto winner scores takenOffBlock
            # (+5). A Blockbuster winner comes off the block automatically —
            # the bbBlockbuster points already cover it, no extra award.
            veto_ids = {h["id"] for h in resolve(veto)}
            bb_ids = {h["id"] for h in resolve(blockbuster)}
            self_veto = [h["name"] for h in saved if h["id"] in veto_ids]
            other_saved = [h["name"] for h in saved
                           if h["id"] not in veto_ids and h["id"] not in bb_ids]
            emit(self_veto, "savedSelf", f"Took themselves off the block ({where})")
            emit(other_saved, "takenOffBlock", f"Taken off the block ({where})")
            emit([h["name"] for h in replacements], "nominated",
                 f"Named replacement nominee ({where})", gate_type="replacementNom")

            # Survived the eviction vote: on the final block, still standing
            # after the live show. Only once the eviction itself is known, so
            # nobody is credited before the vote actually happens.
            if evicted:
                evicted_ids = {h["id"] for h in resolve(evicted)}
                survivors = [h["name"] for hid, h in final_ids.items()
                             if hid not in evicted_ids]
                emit(survivors, "survivedVote", f"Survived the eviction vote ({where})")

    # Evictions: status change only (no points)
    if evicted:
        if not is_aired(week, "evicted"):
            held.append((week, "evicted", len(evicted)))
        else:
            for hg in resolve(evicted):
                if hg.get("status") == "active":
                    hg["status"] = "evicted"
                    hg["weekEvicted"] = week
                    published.append(f"{where}: {hg['name']} marked evicted")


def clear_merged_week_events(data, split_weeks):
    """Drop auto-scraped events for a week that is split across day-columns but
    whose stored events predate the split (no `day` tag).

    Those were scored from the two days mashed together and are wrong in ways
    no in-place edit can fix — phantom nominations, missing self-saves,
    survival points for players who were never on that block. Dropping them
    lets apply_segment rebuild the week correctly from Wikipedia on this same
    run. Manual admin entries (lockPoints) are never touched.

    Also clears the evicted status for those weeks so it re-derives; a player
    who really was evicted is re-marked moments later from the same grid."""
    if not split_weeks:
        return []
    weeks = set(split_weeks)
    lines = []
    for hg in data.get("houseguests") or []:
        keep, dropped = [], []
        for ev in (hg.get("events") or []):
            if (ev.get("week") in weeks and not ev.get("day")
                    and not ev.get("lockPoints")):
                dropped.append(ev["week"])
            else:
                keep.append(ev)
        if dropped:
            hg["events"] = keep
            for wk in sorted(set(dropped)):
                n = sum(1 for w in dropped if w == wk)
                lines.append(f"Week {wk}: cleared {n} merged-week event(s) for "
                             f"{hg['name']} — rebuilding per day")
        if hg.get("weekEvicted") in weeks:
            hg["status"] = "active"
            hg["weekEvicted"] = None
    for ep in data.get("episodes") or []:
        if ep.get("week") in weeks:
            ep["events"] = [le for le in (ep.get("events") or [])
                            if le.get("day") or le.get("lockPoints")]
    return lines


def reclassify_self_saves(data):
    """Correct legacy off-block scoring. Two rules:

    1. takenOffBlock (+5) held by a player who won the veto that week is a
       self-save — rescored to savedSelf (+3). +5 is reserved for being
       taken off the block by someone else.
    2. savedSelf/takenOffBlock held by a player who won the Blockbuster
       that week (without a veto win) is REMOVED: a Blockbuster win takes
       the winner off the block automatically, and the bbBlockbuster
       points already cover it.

    Fixes the player's events and the matching episode-log entries.
    Returns log lines."""
    pts = get_points(data, "savedSelf")
    lines = []
    for hg in data.get("houseguests") or []:
        events = hg.get("events") or []
        veto_weeks = {ev.get("week") for ev in events if ev.get("type") == "veto"}
        bb_weeks = {ev.get("week") for ev in events if ev.get("type") == "bbBlockbuster"}
        keep = []
        for ev in events:
            week = ev.get("week")
            etype = ev.get("type")
            if etype == "takenOffBlock" and week in veto_weeks:
                old_pts = ev.get("points")
                ev["type"] = "savedSelf"
                ev["points"] = pts
                ev["description"] = f"Took themselves off the block (Week {week})"
                for ep in data.get("episodes") or []:
                    if ep.get("week") != week:
                        continue
                    for le in ep.get("events") or []:
                        if le.get("type") == "takenOffBlock" and le.get("houseguestId") == hg["id"]:
                            le["type"] = "savedSelf"
                            le["points"] = pts
                            le["description"] = ev["description"]
                lines.append(f"Week {week}: {hg['name']} self-save rescored "
                             f"{'+' if old_pts >= 0 else ''}{old_pts} -> +{pts} (savedSelf)")
                keep.append(ev)
            elif etype in ("savedSelf", "takenOffBlock") and week in bb_weeks and week not in veto_weeks:
                for ep in data.get("episodes") or []:
                    if ep.get("week") != week:
                        continue
                    ep["events"] = [le for le in (ep.get("events") or [])
                                    if not (le.get("type") == etype and le.get("houseguestId") == hg["id"])]
                lines.append(f"Week {week}: {hg['name']} off-block points removed "
                             f"({'+' if ev.get('points', 0) >= 0 else ''}{ev.get('points')}) - "
                             f"Blockbuster win already covers coming off the block")
            else:
                keep.append(ev)
        hg["events"] = keep
    return lines


# ── Season So Far summaries ───────────────────────────────────────────────
# Each run REWRITES every houseguest's `summary` from their full aired event
# history, so the text always leads with what's most significant right now
# (old news compresses or drops instead of accumulating line by line).
# Hand-written color lives in `storyNotes` (Admin page) and is never touched.

COMP_WINS = {
    "hoh":           ("an", "HOH win", "HOH wins"),
    "veto":          ("a", "veto win", "veto wins"),
    "bbBlockbuster": ("a", "Blockbuster win", "Blockbuster wins"),
    "wallHang":      ("a", "wall-hang win", "wall-hang wins"),
    "otev":          ("an", "OTEV win", "OTEV wins"),
    "bbComics":      ("a", "BB Comics win", "BB Comics wins"),
    "safety":        ("a", "safety win", "safety wins"),
}

_NUM_WORDS = {2: "two", 3: "three", 4: "four", 5: "five",
              6: "six", 7: "seven", 8: "eight", 9: "nine"}


def _count_phrase(n, article, singular, plural):
    if n == 1:
        return f"{article} {singular}"
    return f"{_NUM_WORDS.get(n, str(n))} {plural}"


def _join(items):
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def build_summary(hg, current_week, evicted_weeks):
    """Compose the spoiler-safe 'Season So Far' paragraph for one houseguest.
    Works only from already-published (i.e., aired) events."""
    events = hg.get("events") or []
    weeks_of = {}
    for ev in events:
        if ev.get("week"):
            weeks_of.setdefault(ev.get("type"), set()).add(ev["week"])

    nom_weeks = sorted(weeks_of.get("nominated", set()))
    self_saved_weeks = weeks_of.get("savedSelf", set())
    # A Blockbuster win while nominated takes the winner off the block too
    # (no separate event — the win itself covers it).
    bb_off_weeks = weeks_of.get("bbBlockbuster", set()) & set(nom_weeks)
    twist_saved_weeks = weeks_of.get("twistSave", set())
    saved_weeks = (weeks_of.get("takenOffBlock", set()) | self_saved_weeks
                   | bb_off_weeks | twist_saved_weeks)
    status = hg.get("status", "active")
    sents = []
    reigning = False

    # 1. Current situation leads
    if status == "winner":
        sents.append("Winner of Big Brother 28.")
    elif status in ("evicted", "jury"):
        wk = hg.get("weekEvicted")
        lead = f"Evicted in Week {wk}" if wk else "Evicted from the house"
        if status == "jury":
            lead += " and now sits on the jury"
        sents.append(lead + ".")
    else:
        hoh_weeks = weeks_of.get("hoh", set())
        if current_week in hoh_weeks and current_week not in evicted_weeks:
            reigning = True
            sents.append(f"Reigning Head of Household after winning the Week {current_week} comp.")
        elif (current_week in nom_weeks and current_week not in saved_weeks
              and current_week not in evicted_weeks):
            sents.append("Currently on the block ahead of the next eviction.")

    # 2. Comp resume (skip the HOH already covered by a reigning lead)
    resume = []
    for etype, (article, singular, plural) in COMP_WINS.items():
        n = len(weeks_of.get(etype, set()))
        if etype == "hoh" and reigning:
            n -= 1
        if n > 0:
            phrase = _count_phrase(n, article, singular, plural)
            if etype == "safety" and weeks_of.get("safety") == {1}:
                phrase += " from premiere night"
            resume.append(phrase)
    if resume:
        verb = "Finished with" if status in ("evicted", "jury", "winner") else "Owns"
        tail = "." if status in ("evicted", "jury", "winner") else " so far."
        sents.append(f"{verb} {_join(resume)}{tail}")

    # 3. Block record (past weeks; the lead already covers a current nomination)
    survived = [w for w in nom_weeks
                if w not in saved_weeks and w in evicted_weeks and hg.get("weekEvicted") != w]
    bits = []
    if self_saved_weeks:
        if len(self_saved_weeks) == 1:
            bits.append(f"pulled themselves off the block in Week {min(self_saved_weeks)}")
        else:
            bits.append(f"pulled themselves off the block {_NUM_WORDS.get(len(self_saved_weeks), len(self_saved_weeks))} times")
    power_weeks = weeks_of.get("evictionPower", set())
    if power_weeks:
        bits.append(f"single-handedly decided the Week {min(power_weeks)} eviction")
    if twist_saved_weeks:
        if len(twist_saved_weeks) == 1:
            bits.append(f"came off a twist block in Week {min(twist_saved_weeks)}")
        else:
            bits.append(f"came off a twist block {_NUM_WORDS.get(len(twist_saved_weeks), len(twist_saved_weeks))} times")
    other_saved = weeks_of.get("takenOffBlock", set())
    if other_saved:
        if len(other_saved) == 1:
            bits.append(f"was pulled off the block in Week {min(other_saved)}")
        else:
            bits.append(f"was pulled off the block {_NUM_WORDS.get(len(other_saved), len(other_saved))} times")
    if survived:
        if len(survived) == 1:
            bits.append(f"survived the Week {survived[0]} eviction vote")
        else:
            bits.append(f"survived {_NUM_WORDS.get(len(survived), len(survived))} eviction votes on the block")
    if bits:
        s = " and ".join(bits)
        sents.append(s[0].upper() + s[1:] + ".")

    if not sents:
        return "Has stayed off the block and out of the comp spotlight so far — the quiet game."
    return " ".join(sents)


# ── AI color pass ─────────────────────────────────────────────────────────
# When ANTHROPIC_API_KEY is set (GitHub Actions secret), aired episode recap
# blurbs from Wikipedia are sent to the Claude API along with each player's
# verified facts, and the returned story-aware blurbs replace the rules-based
# summaries. Blurbs for unaired episodes are empty on Wikipedia, so this
# source is spoiler-safe by construction. Falls back to rules-based text on
# any failure. `summaryDigest` in the data records what the last AI pass saw,
# so the API is only called when the recaps or facts actually change.

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
MAX_BLURBS = 6  # most recent aired episodes sent to the API (facts + previous blurbs carry older context)
PROMPT_VERSION = 4  # bump to force a fresh AI pass after prompt changes


def _strip_wiki_markup(text):
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)  # [[A|B]] -> B
    text = re.sub(r"\{\{efn\|[^}]*\}\}", "", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)                     # leftover templates
    text = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = text.replace("''", "")
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


_EPISODES_WIKITEXT = None  # fetched at most once per run


def fetch_episodes_wikitext():
    """Raw wikitext of the article's Episodes section (cached per run)."""
    global _EPISODES_WIKITEXT
    if _EPISODES_WIKITEXT is not None:
        return _EPISODES_WIKITEXT
    api = "https://en.wikipedia.org/w/api.php"
    try:
        r = requests.get(api, params={"action": "parse", "page": "Big_Brother_28_(American_season)",
                                      "prop": "sections", "format": "json"},
                         headers=HEADERS, timeout=20)
        sections = r.json()["parse"]["sections"]
        idx = next(s["index"] for s in sections if s["line"].lower() == "episodes")
        r = requests.get(api, params={"action": "parse", "page": "Big_Brother_28_(American_season)",
                                      "prop": "wikitext", "section": idx, "format": "json"},
                         headers=HEADERS, timeout=20)
        _EPISODES_WIKITEXT = r.json()["parse"]["wikitext"]["*"]
    except Exception as e:
        print(f"Could not fetch the Episodes section: {e}")
        _EPISODES_WIKITEXT = ""
    return _EPISODES_WIKITEXT


def _clean_episode_title(raw, number):
    """Tidy Wikipedia's episode titles for display."""
    title = _strip_wiki_markup(raw or "").strip().strip('"').strip()
    if number == 1:
        return "Season Premiere"
    if "unlocked" in title.lower():
        return "Big Brother: Unlocked"          # drop the trailing air date
    return title or f"Episode {number}"


def fetch_episode_schedule():
    """Every announced episode: number, display title, and air date.

    Schedule data only — no results — so future episodes are safe to show."""
    schedule = []
    for block in fetch_episodes_wikitext().split("{{Episode list/sublist")[1:]:
        m_num = re.search(r"EpisodeNumber2\s*=\s*(\d+)", block)
        m_date = re.search(r"OriginalAirDate\s*=\s*\{\{Start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})", block)
        if not m_num or not m_date:
            continue
        m_title = re.search(r"Title\s*=\s*(.*)", block)
        number = int(m_num.group(1))
        schedule.append({
            "ep": number,
            "title": _clean_episode_title(m_title.group(1) if m_title else "", number),
            "date": str(date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))),
        })
    schedule.sort(key=lambda e: (e["date"], e["ep"]))
    return schedule


def fetch_episode_blurbs():
    """Return aired, non-empty episode recap texts from Wikipedia, oldest first."""
    wikitext = fetch_episodes_wikitext()
    if not wikitext:
        return []

    blurbs = []
    now = datetime.now(ET)
    for block in wikitext.split("{{Episode list/sublist")[1:]:
        m_date = re.search(r"OriginalAirDate\s*=\s*\{\{Start date\|(\d{4})\|(\d{1,2})\|(\d{1,2})", block)
        m_sum = re.search(r"ShortSummary\s*=\s*(.*?)\n\s*\|\s*LineColor", block, re.S)
        if not m_date or not m_sum:
            continue
        text = _strip_wiki_markup(m_sum.group(1))
        if not text:
            continue
        d = date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))
        aired_at = datetime(d.year, d.month, d.day, *REVEAL_HOUR, tzinfo=ET)
        if now >= aired_at:
            blurbs.append({"date": str(d), "text": text})
    blurbs.sort(key=lambda b: b["date"])
    return blurbs[-MAX_BLURBS:]


WIN_LABELS = {"hoh": "HOH", "veto": "veto", "bbBlockbuster": "Blockbuster", "safety": "safety",
              "wallHang": "wall hang", "otev": "OTEV", "bbComics": "BB Comics"}


def _verified_wins(hg):
    wins = [f"{WIN_LABELS[ev['type']]} (Week {ev.get('week', '?')})"
            for ev in (hg.get("events") or []) if ev.get("type") in WIN_LABELS]
    return "; ".join(wins) if wins else "NONE — this player has won nothing this season"


def ai_color_pass(api_key, hgs, facts, blurbs):
    """Ask Claude to blend facts + recap color. Returns {hg_id: blurb} or None."""
    recap_lines = "\n\n".join(f"[aired {b['date']}] {b['text']}" for b in blurbs)
    fact_lines = "\n".join(
        f"- {hg['id']} | {hg['name']} | status: {hg.get('status', 'active')} | "
        f"verified wins: {_verified_wins(hg)} | {facts[hg['id']]}"
        for hg in hgs)
    prev_lines = "\n".join(f"- {hg['id']}: {hg['summary']}" for hg in hgs if hg.get("summary"))

    prompt = f"""You write the "Season So Far" blurbs for a Big Brother 28 fan website.

Below are (1) this season's aired episode recaps, (2) each houseguest's verified competition/nomination facts, and (3) the previously published blurbs.

Write a fresh 1-3 sentence "Season So Far" blurb for EVERY houseguest listed, blending the facts with story color from the recaps (alliances, big moves, betrayals, funny moments).

Rules:
- Use ONLY information present in the recaps and facts below. Never invent or speculate beyond them.
- Each player's "verified wins" field is the complete, authoritative list of everything they have won. If it says NONE, that player has won nothing — no comps, no safety — no matter how a recap sentence reads. Being in the group whose member won does NOT make them a winner.
- Only describe a player as a member of an alliance if the recap explicitly names them as one of its members. Re-read the recap sentence carefully before attributing membership.
- Lead with the player's current situation, then their most significant storylines. Old news should compress or drop as bigger things happen.
- If the recaps never mention a player, write from their facts alone.
- The previous blurbs are a continuity reference only and MAY CONTAIN ERRORS. Never treat them as a source of facts: re-verify every claim you carry forward against the recaps and facts, and silently correct anything they got wrong (a misattributed win, a wrong alliance roster). When previous blurbs conflict with the recaps or facts, the recaps and facts always win.
- Carry forward still-relevant, verified storylines (like alliance membership) even when the latest recap doesn't repeat them; drop anything the newer material makes obsolete.
- Keep each blurb under 60 words. Plain text, no markdown.
- Refer to houseguests by first name.

EPISODE RECAPS (aired episodes only):
{recap_lines}

HOUSEGUEST FACTS:
{fact_lines}

PREVIOUS BLURBS:
{prev_lines if prev_lines else "(none yet)"}

First, inside a <scratch> block, extract from the recaps: (a) every alliance with its exact member list as literally named in the recap text, and (b) every competition or safety win with the exact winner's name. Cross-check each against the verified-wins fields before writing.

Then, after the scratch block, reply with a JSON object mapping every houseguest id to their new blurb string. No other text after the JSON."""

    try:
        r = requests.post(ANTHROPIC_URL, timeout=120,
                          headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": ANTHROPIC_MODEL, "max_tokens": 5000,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        # The response is a <scratch> verification block followed by the JSON object
        after_scratch = text.find("</scratch>")
        start = text.find("{", after_scratch if after_scratch != -1 else 0)
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in response")
        result = json.loads(text[start:end + 1])
        if not isinstance(result, dict):
            raise ValueError("response is not a JSON object")
        return {k: str(v).strip() for k, v in result.items() if str(v).strip()}
    except Exception as e:
        print(f"AI color pass failed ({e}); using rules-based summaries.")
        return None


def update_summaries(data, dry_run=False):
    """Regenerate every houseguest's summary; returns how many changed.

    With ANTHROPIC_API_KEY set: facts + aired recaps go through the Claude API
    (only when they've changed since the last successful pass).
    Without the key: rules-based facts text — unless an AI pass has published
    before (summaryDigest present), in which case summaries are left alone so
    a local run can't clobber the Action's AI-written text."""
    hgs = data.get("houseguests") or []
    if not hgs:
        return 0
    all_weeks = [ev["week"] for hg in hgs for ev in (hg.get("events") or []) if ev.get("week")]
    current_week = max(all_weeks) if all_weeks else 1
    evicted_weeks = {hg.get("weekEvicted") for hg in hgs if hg.get("weekEvicted")}
    facts = {hg["id"]: build_summary(hg, current_week, evicted_weeks) for hg in hgs}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    new_texts = facts

    if api_key:
        blurbs = fetch_episode_blurbs()
        digest = hashlib.sha256(json.dumps([PROMPT_VERSION, facts, blurbs], sort_keys=True).encode()).hexdigest()
        if data.get("summaryDigest") == digest:
            return 0  # nothing the AI saw has changed; keep current summaries
        if dry_run:
            print(f"DRY RUN — AI color pass would run on {len(blurbs)} recap(s). Skipping the API call.")
            return 0
        ai = ai_color_pass(api_key, hgs, facts, blurbs)
        if ai:
            new_texts = {hg["id"]: ai.get(hg["id"]) or facts[hg["id"]] for hg in hgs}
            data["summaryDigest"] = digest  # only marks SUCCESSFUL passes; failures retry next run
    elif data.get("summaryDigest"):
        return 0  # AI pipeline owns summaries; don't overwrite them with plain facts

    changed = 0
    for hg in hgs:
        if hg.get("summary") != new_texts[hg["id"]]:
            hg["summary"] = new_texts[hg["id"]]
            changed += 1
    return changed


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

    segments = parse_segments(matrix)
    if not segments:
        # Exit non-zero so the Action goes red — a silent no-op here once let
        # a parser break go unnoticed for a full episode cycle.
        print("ERROR: results grid found but no week columns parsed — "
              "the Wikipedia table layout likely changed. Nothing updated.")
        sys.exit(1)
    split_weeks = sorted({s["week"] for s in segments
                          if sum(1 for t in segments if t["week"] == s["week"]) > 1})
    print(f"Parsed results grid: {len(segments)} column(s) across weeks "
          f"{sorted({s['week'] for s in segments})}"
          + (f"; split weeks {split_weeks}" if split_weeks else ""))

    # Sync the league-voted scoring board, fix any legacy self-save types, then
    # rescore stored events to the current board. All three run BEFORE this
    # run's results so the duplicate guard sees corrected data.
    board_changes = sync_scoring_board(data)
    for line in board_changes:
        print(f"SCORING  {line}")
    rescored = clear_merged_week_events(data, split_weeks)
    rescored += reclassify_self_saves(data)
    rescored += resync_event_points(data)
    for line in rescored:
        print(f"RESCORE  {line}")

    # Broadcast schedule — refreshed from Wikipedia so the Episode Log page
    # never goes stale as CBS announces more dates.
    schedule = fetch_episode_schedule()
    schedule_changed = bool(schedule) and data.get("schedule") != schedule
    if schedule_changed:
        was = len(data.get("schedule") or [])
        data["schedule"] = schedule
        print(f"SCHEDULE {was} -> {len(schedule)} episode(s) listed "
              f"(through {schedule[-1]['date']})")

    published, held, unmatched = [], [], set()
    for seg in segments:
        apply_segment(data, seg, published, held, unmatched)

    refreshed = update_summaries(data, dry_run=dry_run)
    if refreshed:
        print(f"SUMMARY  rewrote 'Season So Far' text for {refreshed} houseguest(s)")

    for line in published:
        print(f"PUBLISH  {line}")
    # Held items are logged WITHOUT names so even the Action log stays spoiler-free
    for week, gate, count in held:
        print(f"HELD     Week {week}: {count} {gate} result(s) — episode hasn't aired yet")
    for name in sorted(unmatched):
        print(f"UNMATCHED name on Wikipedia (not on our roster): {name}")

    if not published and not refreshed and not rescored and not board_changes and not schedule_changed:
        print("No new aired events to publish; summaries and schedule already current.")
        return

    if dry_run:
        print(f"DRY RUN — {len(published)} event(s), {len(rescored)} rescore(s), "
              f"{len(board_changes)} board change(s), {int(schedule_changed)} schedule "
              f"update(s), and {refreshed} summary rewrite(s) would publish. Nothing written.")
        return

    data["lastUpdated"] = str(date.today())
    push_firebase(data)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Published {len(published)} event(s) and {refreshed} summary rewrite(s) to Firebase; data.json backup written.")


if __name__ == "__main__":
    main()
