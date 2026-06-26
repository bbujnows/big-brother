# BB28 — "Live Feed" Surveillance Theme · Integration Guide

This folder is a drop-in reskin of your site. It reuses your **existing class names**, so
your `index.html`, `app.js`, `admin.js`, etc. need **no changes** to get the new look.

## Files
- `style.css` — full theme. Replaces your current `style.css`.
- `bb-eye.svg` — the surveillance-eye logo mark (used by the nav + available standalone).

## Install (the 30-second version)
1. Copy `style.css` over your existing `big-brother/style.css`.
2. Copy `bb-eye.svg` into `big-brother/` (same folder as `style.css`, so the nav logo's
   `mask: url('bb-eye.svg')` resolves).
3. Reload. Done — every page themes automatically, including all JS-generated content
   (owner cards, the full 17-row scoring grid, the houseguest table, countdown, draft, log).

That's the whole job. Everything below is **optional polish**.

---

## Optional polish (small HTML edits)

These add the surveillance "HUD" flourishes that CSS alone can't place. All optional.

### 1. Make the season number glow mint in the nav
In `index.html`, change:
```html
<a class="nav-logo" href="index.html">BIG BROTHER 28</a>
```
to:
```html
<a class="nav-logo" href="index.html">BIG BROTHER<span style="color:#5fffc2"> 28</span></a>
```

### 2. Hero "REC / CAM 01 / timestamp" HUD bar
Inside `.hero`, right after `<div class="hero">`, add:
```html
<div class="hero-hud">
  <span class="rec"><i></i>REC</span>
  <span>CAM 01 · LIVE FEED · HOUSE</span>
  <span id="hud-clock">--·--·--  --:--:--</span>
</div>
```
The `style.css` already styles `.hero-hud`. To make the clock tick, add this to your
hero `<script>` in `index.html`:
```js
setInterval(() => {
  const el = document.getElementById('hud-clock');
  if (!el) return;
  const d = new Date(), p = n => String(n).padStart(2,'0');
  el.textContent = `${p(d.getMonth()+1)}·${p(d.getDate())}·${String(d.getFullYear()).slice(2)}  ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}, 1000);
```

### 3. Bring back the pool photo as a night-vision cam feed (optional)
The hero currently uses a dark gradient. To layer your `pool-bg.jpg` back in as a
desaturated CCTV feed, add to the `.hero` rule in `style.css`:
```css
.hero {
  background-image:
    linear-gradient(rgba(8,40,30,0.5), rgba(6,9,13,0.96)),
    url('pool-bg.jpg');
  background-size: cover;
  background-position: center;
}
```

---

## How the theme maps to your data
- **Scoring grid** renders from `data.json → scoring` (all 17 rows). Negative values
  (`Nominated for Eviction`) auto-color red via the `.pts-neg` class your `app.js` adds.
- **Owner cards** use each owner's `color` as the top accent bar (`--owner-color`), kept
  from your original design. Rank 1 gets the mint glow + filled badge.
- **Status badges** (`active / evicted / jury / winner`) and **event types**
  (`hoh / veto / nominated / …`) are all themed to the surveillance palette.

## Palette reference
| Token | Hex | Use |
|---|---|---|
| Signal mint | `#5fffc2` | primary accent, glow, links |
| Mint dim | `#7fb9ad` | secondary text |
| REC red | `#ff5147` | negatives, admin, evicted |
| Gold | `#ffd93d` | winner / 1st place |
| Violet | `#a78bfa` | jury / resurrection |
| BG | `#06080b` | page |

Fonts: **Bebas Neue** (big numbers/titles), **Space Mono** (HUD labels), **Inter** (body).
