const ADMIN_PASSWORD = 'bbseason28';
let _adminData = null;

function checkPw() {
  const val = document.getElementById('pw-input').value;
  if (val === ADMIN_PASSWORD) {
    document.getElementById('login-panel').classList.add('hidden');
    document.getElementById('admin-content').classList.remove('hidden');
    loadAdminData();
  } else {
    document.getElementById('pw-error').classList.remove('hidden');
  }
}

async function initAdmin() {
  document.getElementById('pw-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') checkPw();
  });
}

async function loadAdminData() {
  // Admin always reads live data — never the test-mode sessionStorage snapshot
  try {
    const res = await fetch(`data.json?nocache=${Date.now()}`);
    _adminData = await res.json();
  } catch (e) {
    _adminData = await loadData();
  }
  refreshAdminUI();
}

function refreshAdminUI() {
  const data = _adminData;

  // Draft status
  const dl = document.getElementById('draft-status-label');
  if (dl) dl.textContent = data.draftStatus;

  // Houseguest list
  renderHgList();

  // Populate houseguest selects
  populateHgSelects();
}

function renderHgList() {
  const list = document.getElementById('hg-list');
  if (!list) return;
  const data = _adminData;

  if (data.houseguests.length === 0) {
    list.innerHTML = '<p style="color:var(--muted);font-size:.85rem;">No houseguests added yet.</p>';
    return;
  }

  list.innerHTML = `
    <div class="card card-sm">
      <table>
        <thead>
          <tr>
            <th>Name</th><th>Age</th><th>Hometown</th><th>Owner</th><th>Status</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${data.houseguests.map(hg => {
            const owner = data.owners.find(o => o.id === hg.ownerId);
            return `
              <tr>
                <td><strong>${hg.name}</strong></td>
                <td>${hg.age || '—'}</td>
                <td>${hg.hometown || '—'}</td>
                <td>${owner ? `<span style="color:${owner.color}">${owner.name}</span>` : '<span style="color:var(--muted)">—</span>'}</td>
                <td><span class="status-badge status-${hg.status}">${hg.status}</span></td>
                <td><button class="btn btn-danger btn-sm" onclick="removeHouseguest('${hg.id}')">Remove</button></td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
}

function populateHgSelects() {
  const data = _adminData;
  ['ev-hg', 'st-hg'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '<option value="">— select —</option>';
    data.houseguests.forEach(hg => {
      const opt = document.createElement('option');
      opt.value = hg.id;
      opt.textContent = hg.name;
      sel.appendChild(opt);
    });
  });
}

// ── HOUSEGUESTS ───────────────────────────────────────────────────────
async function addHouseguest() {
  const name = document.getElementById('hg-name').value.trim();
  if (!name) { showMsg('Name is required.', 'error'); return; }

  const age  = parseInt(document.getElementById('hg-age').value) || null;
  const hometown = document.getElementById('hg-hometown').value.trim() || null;
  const photo = document.getElementById('hg-photo').value.trim() || '';

  if (_adminData.houseguests.find(h => h.name.toLowerCase() === name.toLowerCase())) {
    showMsg(`${name} already exists.`, 'error'); return;
  }

  _adminData.houseguests.push({
    id: slugify(name), name, age, hometown, photo,
    status: 'active', ownerId: null, weekEvicted: null, events: []
  });

  const ok = await saveData(_adminData);
  if (ok) {
    showMsg(`${name} added!`);
    document.getElementById('hg-name').value = '';
    document.getElementById('hg-age').value = '';
    document.getElementById('hg-hometown').value = '';
    document.getElementById('hg-photo').value = '';
    invalidateCache();
    _adminData = await loadData();
    refreshAdminUI();
  }
}

async function bulkImport() {
  const raw = document.getElementById('bulk-names').value.trim();
  if (!raw) return;
  const names = raw.split('\n').map(n => n.trim()).filter(Boolean);
  let added = 0;
  names.forEach(name => {
    if (!_adminData.houseguests.find(h => h.name.toLowerCase() === name.toLowerCase())) {
      _adminData.houseguests.push({
        id: slugify(name), name, age: null, hometown: null, photo: '',
        status: 'active', ownerId: null, weekEvicted: null, events: []
      });
      added++;
    }
  });
  const ok = await saveData(_adminData);
  if (ok) {
    showMsg(`${added} houseguest(s) imported!`);
    document.getElementById('bulk-names').value = '';
    invalidateCache();
    _adminData = await loadData();
    refreshAdminUI();
  }
}

async function removeHouseguest(id) {
  if (!confirm('Remove this houseguest? Their events will also be removed.')) return;
  _adminData.houseguests = _adminData.houseguests.filter(h => h.id !== id);
  const ok = await saveData(_adminData);
  if (ok) {
    showMsg('Houseguest removed.');
    invalidateCache();
    _adminData = await loadData();
    refreshAdminUI();
  }
}

// ── DRAFT CONTROL ─────────────────────────────────────────────────────
async function openDraft() {
  if (_adminData.houseguests.length === 0) {
    showMsg('Add houseguests before opening the draft.', 'error'); return;
  }
  _adminData.draftStatus = 'open';
  _adminData.draftOrder = _adminData.owners.map(o => o.id); // default order; wheel sets real order
  _adminData.currentPickIndex = 0;
  const ok = await saveData(_adminData);
  if (ok) { showMsg('Draft opened!'); document.getElementById('draft-status-label').textContent = 'open'; }
}

async function resetDraft() {
  if (!confirm('Reset the draft? All team assignments will be cleared.')) return;
  _adminData.draftStatus = 'pending';
  _adminData.draftOrder = [];
  _adminData.currentPickIndex = 0;
  _adminData.houseguests.forEach(h => { h.ownerId = null; });
  const ok = await saveData(_adminData);
  if (ok) {
    sessionStorage.removeItem('bb28_test_data');
    showMsg('Draft reset.');
    document.getElementById('draft-status-label').textContent = 'pending';
  }
}

function clearTestDataAdmin() {
  sessionStorage.removeItem('bb28_test_data');
  _dataCache = null;
  showMsg('Test data cleared — site is back to live data.');
  document.getElementById('global-test-bar')?.remove();
}

async function undoLastPick() {
  if (_adminData.currentPickIndex <= 0) { showMsg('No picks to undo.', 'error'); return; }
  const seq = buildPickSequence(_adminData.draftOrder, _adminData.houseguests.length);
  const lastPickOwner = seq[_adminData.currentPickIndex - 1];
  // Find the last houseguest assigned to that owner (last pick)
  const owned = _adminData.houseguests.filter(h => h.ownerId === lastPickOwner);
  if (owned.length > 0) owned[owned.length - 1].ownerId = null;
  _adminData.currentPickIndex--;
  if (_adminData.draftStatus === 'complete') _adminData.draftStatus = 'open';
  const ok = await saveData(_adminData);
  if (ok) { showMsg('Last pick undone.'); invalidateCache(); _adminData = await loadData(); refreshAdminUI(); }
}

// ── EVENTS ────────────────────────────────────────────────────────────
function autoFillPoints() {
  const type = document.getElementById('ev-type').value;
  if (!type || !_adminData) return;
  const pts = getPointsForType(_adminData, type);
  document.getElementById('ev-points').value = pts;
}

async function addEvent() {
  const week   = parseInt(document.getElementById('ev-week').value);
  const hgId   = document.getElementById('ev-hg').value;
  const type   = document.getElementById('ev-type').value;
  const points = parseInt(document.getElementById('ev-points').value);
  const note   = document.getElementById('ev-note').value.trim();

  if (!week || !hgId || !type || isNaN(points)) {
    showMsg('Fill in all required fields.', 'error'); return;
  }

  const hg = _adminData.houseguests.find(h => h.id === hgId);
  if (!hg) return;

  const description = note || (_adminData.scoring[type] ? _adminData.scoring[type].label : type);
  hg.events = hg.events || [];
  hg.events.push({ week, type, points, description, addedAt: new Date().toISOString() });

  // Also add to episodes log
  let ep = _adminData.episodes.find(e => e.week === week);
  if (!ep) {
    ep = { week, airDate: '', events: [] };
    _adminData.episodes.push(ep);
    _adminData.episodes.sort((a,b) => a.week - b.week);
  }
  ep.events = ep.events || [];
  ep.events.push({ type, houseguestId: hgId, points, description });

  const ok = await saveData(_adminData);
  if (ok) {
    showMsg(`Event logged for ${hg.name}: ${description} (${points > 0 ? '+' : ''}${points})`);
    document.getElementById('ev-week').value = '';
    document.getElementById('ev-hg').value = '';
    document.getElementById('ev-type').value = '';
    document.getElementById('ev-points').value = '';
    document.getElementById('ev-note').value = '';
    invalidateCache();
    _adminData = await loadData();
    refreshAdminUI();
  }
}

// ── STATUS UPDATE ─────────────────────────────────────────────────────
async function updateStatus() {
  const hgId  = document.getElementById('st-hg').value;
  const status = document.getElementById('st-status').value;
  const week  = parseInt(document.getElementById('st-week').value) || null;

  if (!hgId) { showMsg('Select a houseguest.', 'error'); return; }

  const hg = _adminData.houseguests.find(h => h.id === hgId);
  if (!hg) return;
  hg.status = status;
  if (week) hg.weekEvicted = week;

  const ok = await saveData(_adminData);
  if (ok) {
    showMsg(`${hg.name} updated to "${status}".`);
    invalidateCache();
    _adminData = await loadData();
    refreshAdminUI();
  }
}

// ── DANGER ZONE ───────────────────────────────────────────────────────
async function clearAllHouseguests() {
  if (!confirm('Delete ALL houseguests and events? This cannot be undone.')) return;
  _adminData.houseguests = [];
  _adminData.episodes = [];
  _adminData.draftStatus = 'pending';
  _adminData.draftOrder = [];
  _adminData.currentPickIndex = 0;
  const ok = await saveData(_adminData);
  if (ok) { showMsg('All houseguests cleared.'); invalidateCache(); _adminData = await loadData(); refreshAdminUI(); }
}

async function clearAllEvents() {
  if (!confirm('Clear all episode events? Points will reset.')) return;
  _adminData.houseguests.forEach(h => { h.events = []; });
  _adminData.episodes = [];
  const ok = await saveData(_adminData);
  if (ok) { showMsg('All events cleared.'); invalidateCache(); _adminData = await loadData(); refreshAdminUI(); }
}
