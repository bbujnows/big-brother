# Scraper Dry-Run Findings — Episode Auto-Update

**Date:** 2026-07-06 (≈3 days before the BB28 premiere on July 9, 2026)
**Tested by:** Brandon (via Claude Code)
**Scope:** Read-only dry run of the episode auto-update mechanism. No code was changed, and nothing was published to the website.

## How the mechanism is supposed to work
- A **GitHub Action** (`.github/workflows/scrape.yml`) runs on a timer — once a day at 2am ET — plus a manual "run now" button.
- It runs `scripts/scrape.py`, which **scrapes the BB28 Wikipedia page**, reads the weekly results tables (HOH, Nominees, Veto, Evicted), matches names to our cast in `data.json`, awards points, and writes the results back.
- That push makes the GitHub Pages site rebuild, so the site shows the new scores.
- Note: it's Wikipedia scraping, **not** an official CBS feed — so updates depend on a Wikipedia volunteer editing the page after each episode, and only appear up to ~24h later (once the daily run fires).

## Findings

### ❌ Finding 1 — The scraper's URLs are both dead (critical)
Both hardcoded Wikipedia URLs in `scripts/scrape.py` (lines 41–44) return **404 — they don't exist**. As written today, the scraper would fetch nothing and silently do nothing every night. This is the critical blocker.

### ✅ Finding 2 — The real page exists, and we found it
The actual article is **"Big Brother 28 (American season)"**:
`https://en.wikipedia.org/wiki/Big_Brother_28_(American_season)`
Neither guessed URL matched that exact naming. The real page loads fine (200 OK, ~175 KB) and already contains all the right sections — *Head of Household, Nominations, Power of Veto, Evicted, Houseguests* were all present.

### ⚠️ Finding 3 — No results tables to parse yet (expected)
The page currently has only **1 table**, and it is **not** a weekly competition-results grid (no HOH-summary headers). That's expected before premiere — there are no weekly results to publish yet. The parser's real test (handling Wikipedia's messy merged-cell grids) **can't be validated until episodes air** and that table gets built.

## Bottom line
- **One definite bug, easily fixed:** point the scraper at the correct URL. Without this, it will never work.
- **One unknown, unavoidable for now:** whether the table parser can read the real weekly grid — untestable until the grid exists (after July 9).

## Recommendation
1. **Fix the URL** in `scripts/scrape.py` (a one-line change) so the mechanism is at least aimed at the real page: `https://en.wikipedia.org/wiki/Big_Brother_28_(American_season)`.
2. **Re-test right after the premiere**, when a real results table exists, to confirm the parser actually handles it — and harden it then if needed.
3. Also remember: the scraper does nothing until the **cast is entered** into `data.json` first (via the admin page).
