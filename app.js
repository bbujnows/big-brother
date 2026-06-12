const REPO = 'bbujnows/big-brother';
const DATA_PATH = 'data.json';

// Cache so pages don't re-fetch on every render
let _dataCache = null;

async function loadData() {
  if (_dataCache) return _dataCache;
  try {
    const res = await fetch(`${DATA_PATH}?nocache=${Date.now()}`);
    _dataCache = await res.json();
    return _dataCache;
  } catch (e) {
    console.error('Failed to load data.json', e);
    return { owners:[], houseguests:[], episodes:[], scoring:{}, draftStatus:'pending', draftOrder:[], currentPickIndex:0 };
  }
}

function invalidateCache() { _dataCache = null; }

// ── SCORING ──────────────────────────────────────────────────────────
function computeGuestScore(hg) {
  if (!hg.events || hg.events.length === 0) return 0;
  return hg.events.reduce((sum, e) => sum + (e.points || 0), 0);
}

function computeOwnerScores(data) {
  const scores = {};
  data.owners.forEach(o => scores[o.id] = 0);
  data.houseguests.forEach(hg => {
    if (!hg.ownerId) return;
    const pts = computeGuestScore(hg);
    scores[hg.ownerId] = (scores[hg.ownerId] || 0) + pts;
  });
  return scores;
}

function getPointsForType(data, type) {
  return data.scoring[type] ? data.scoring[type].points : 0;
}

// ── GITHUB API ───────────────────────────────────────────────────────
function getToken() {
  let tok = sessionStorage.getItem('gh_token');
  if (!tok) {
    tok = prompt('Enter your GitHub Personal Access Token to save changes:\n(Fine-grained token with Contents: Read & Write on bbujnows/big-brother)');
    if (!tok) return null;
    sessionStorage.setItem('gh_token', tok.trim());
  }
  return tok;
}

async function saveData(data) {
  const tok = getToken();
  if (!tok) return false;

  // 1. Get current file SHA
  let sha;
  try {
    const r = await fetch(`https://api.github.com/repos/${REPO}/contents/${DATA_PATH}`, {
      headers: { 'Authorization': `Bearer ${tok}`, 'Accept': 'application/vnd.github.v3+json' }
    });
    if (!r.ok) throw new Error(await r.text());
    sha = (await r.json()).sha;
  } catch (e) {
    showMsg('Could not connect to GitHub: ' + e.message, 'error');
    sessionStorage.removeItem('gh_token');
    return false;
  }

  // 2. Write updated file
  data.lastUpdated = new Date().toISOString().slice(0, 10);
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(data, null, 2))));
  try {
    const r = await fetch(`https://api.github.com/repos/${REPO}/contents/${DATA_PATH}`, {
      method: 'PUT',
      headers: {
        'Authorization': `Bearer ${tok}`,
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.github.v3+json'
      },
      body: JSON.stringify({ message: 'Admin update via BB28 site', content, sha })
    });
    if (!r.ok) throw new Error(await r.text());
    invalidateCache();
    return true;
  } catch (e) {
    showMsg('Save failed: ' + e.message, 'error');
    return false;
  }
}

// ── UTILITIES ────────────────────────────────────────────────────────
function showMsg(text, type = 'success') {
  const el = document.getElementById('msg');
  if (!el) return;
  el.className = `alert alert-${type}`;
  el.textContent = text;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 4000);
}

function slugify(name) {
  return 'hg_' + name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

// ── SNAKE DRAFT ORDER ────────────────────────────────────────────────
// Given draftOrder (array of 4 owner ids) and total houseguests,
// returns the full pick sequence as an array of owner ids.
function buildPickSequence(draftOrder, totalGuests) {
  const seq = [];
  const n = draftOrder.length;
  const rounds = Math.ceil(totalGuests / n);
  for (let r = 0; r < rounds; r++) {
    const order = r % 2 === 0 ? draftOrder : [...draftOrder].reverse();
    order.forEach(id => { if (seq.length < totalGuests) seq.push(id); });
  }
  return seq;
}
