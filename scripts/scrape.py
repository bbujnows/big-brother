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
    saved_weeks = weeks_of.get("takenOffBlock", set())
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
    if saved_weeks:
        if len(saved_weeks) == 1:
            bits.append(f"was pulled off the block in Week {min(saved_weeks)}")
        else:
            bits.append(f"was pulled off the block {_NUM_WORDS.get(len(saved_weeks), len(saved_weeks))} times")
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
PROMPT_VERSION = 2  # bump to force a fresh AI pass after prompt changes


def _strip_wiki_markup(text):
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)  # [[A|B]] -> B
    text = re.sub(r"\{\{efn\|[^}]*\}\}", "", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)                     # leftover templates
    text = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = text.replace("''", "")
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def fetch_episode_blurbs():
    """Return aired, non-empty episode recap texts from Wikipedia, oldest first."""
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
        wikitext = r.json()["parse"]["wikitext"]["*"]
    except Exception as e:
        print(f"Could not fetch episode recaps: {e}")
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


def ai_color_pass(api_key, hgs, facts, blurbs):
    """Ask Claude to blend facts + recap color. Returns {hg_id: blurb} or None."""
    recap_lines = "\n\n".join(f"[aired {b['date']}] {b['text']}" for b in blurbs)
    fact_lines = "\n".join(f"- {hg['id']} | {hg['name']} | status: {hg.get('status', 'active')} | {facts[hg['id']]}"
                           for hg in hgs)
    prev_lines = "\n".join(f"- {hg['id']}: {hg['summary']}" for hg in hgs if hg.get("summary"))

    prompt = f"""You write the "Season So Far" blurbs for a Big Brother 28 fan website.

Below are (1) this season's aired episode recaps, (2) each houseguest's verified competition/nomination facts, and (3) the previously published blurbs.

Write a fresh 1-3 sentence "Season So Far" blurb for EVERY houseguest listed, blending the facts with story color from the recaps (alliances, big moves, betrayals, funny moments).

Rules:
- Use ONLY information present in the recaps and facts below. Never invent or speculate beyond them.
- The HOUSEGUEST FACTS lines are authoritative for competition results. Never credit a player with winning a competition, safety, HOH, or veto unless their own facts line says so — being in the group whose member won does NOT make them a winner.
- Only describe a player as a member of an alliance if the recap explicitly names them as one of its members. Re-read the recap sentence carefully before attributing membership.
- Lead with the player's current situation, then their most significant storylines. Old news should compress or drop as bigger things happen.
- If the recaps never mention a player, write from their facts alone.
- Carry forward still-relevant storylines from the previous blurbs (like alliance membership) even when the latest recap doesn't repeat them; drop anything the newer material makes obsolete.
- Keep each blurb under 60 words. Plain text, no markdown.
- Refer to houseguests by first name.

EPISODE RECAPS (aired episodes only):
{recap_lines}

HOUSEGUEST FACTS:
{fact_lines}

PREVIOUS BLURBS:
{prev_lines if prev_lines else "(none yet)"}

Reply with ONLY a JSON object mapping every houseguest id to their new blurb string."""

    try:
        r = requests.post(ANTHROPIC_URL, timeout=120,
                          headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": ANTHROPIC_MODEL, "max_tokens": 3000,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        result = json.loads(text)
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

    weeks = parse_weeks(matrix)
    if not weeks:
        print("Results grid found but no week columns parsed. Skipping update.")
        return
    print(f"Parsed results grid: weeks {sorted(weeks)}")

    published, held, unmatched = [], [], set()
    for week in sorted(weeks):
        apply_week(data, week, weeks[week], published, held, unmatched)

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

    if not published and not refreshed:
        print("No new aired events to publish; summaries already current.")
        return

    if dry_run:
        print(f"DRY RUN — {len(published)} event(s) and {refreshed} summary rewrite(s) would publish. Nothing written.")
        return

    data["lastUpdated"] = str(date.today())
    push_firebase(data)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Published {len(published)} event(s) and {refreshed} summary rewrite(s) to Firebase; data.json backup written.")


if __name__ == "__main__":
    main()
