let _draftData = null;

async function initDraft() {
  _draftData = await loadData();
  const { draftStatus, houseguests } = _draftData;

  hide('state-no-cast'); hide('state-wheel'); hide('state-draft'); hide('state-complete');

  if (houseguests.length === 0) {
    show('state-no-cast');
    return;
  }

  if (draftStatus === 'pending') {
    show('state-wheel');
    drawWheel(_draftData.owners);
    return;
  }

  if (draftStatus === 'open') {
    show('state-draft');
    renderDraftUI();
    return;
  }

  if (draftStatus === 'complete') {
    show('state-complete');
    renderTeams();
  }
}

// ── WHEEL ─────────────────────────────────────────────────────────────
let wheelAngle = 0;
let spinning = false;

function drawWheel(owners, highlightIndex = -1) {
  const canvas = document.getElementById('wheelCanvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2, r = W / 2 - 8;
  const n = owners.length;
  const slice = (2 * Math.PI) / n;

  ctx.clearRect(0, 0, W, H);

  owners.forEach((owner, i) => {
    const start = wheelAngle + i * slice - Math.PI / 2;
    const end = start + slice;

    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, end);
    ctx.closePath();
    ctx.fillStyle = i === highlightIndex ? '#fff' : owner.color;
    ctx.globalAlpha = i === highlightIndex ? 1 : 0.85;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = '#0a0a14';
    ctx.lineWidth = 3;
    ctx.stroke();

    // Label
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(start + slice / 2);
    ctx.textAlign = 'right';
    ctx.fillStyle = '#0a0a14';
    ctx.font = 'bold 16px Segoe UI, system-ui, sans-serif';
    ctx.fillText(owner.name, r - 18, 6);
    ctx.restore();
  });

  // Center circle
  ctx.beginPath();
  ctx.arc(cx, cy, 22, 0, 2 * Math.PI);
  ctx.fillStyle = '#0a0a14';
  ctx.fill();
  ctx.strokeStyle = '#1c1c3a';
  ctx.lineWidth = 2;
  ctx.stroke();
}

function spinWheel() {
  if (spinning) return;
  spinning = true;
  document.getElementById('spinBtn').disabled = true;
  document.getElementById('wheelResult').textContent = '';
  document.getElementById('confirm-order').classList.add('hidden');

  const owners = _draftData.owners;
  const n = owners.length;
  const slice = (2 * Math.PI) / n;

  // Random extra spins + random stop position
  const extraSpins = 5 + Math.floor(Math.random() * 5);
  const randomStop = Math.random() * 2 * Math.PI;
  const totalRotation = extraSpins * 2 * Math.PI + randomStop;
  const duration = 3500;
  const start = performance.now();
  const startAngle = wheelAngle;

  function ease(t) { return 1 - Math.pow(1 - t, 4); }

  function frame(now) {
    const elapsed = now - start;
    const t = Math.min(elapsed / duration, 1);
    wheelAngle = startAngle + totalRotation * ease(t);
    drawWheel(owners);

    if (t < 1) {
      requestAnimationFrame(frame);
    } else {
      wheelAngle = startAngle + totalRotation;
      // The pointer is at the top (12 o'clock = -π/2 from positive x-axis)
      // Find which segment is at the top
      const normalizedAngle = (((-Math.PI / 2) - wheelAngle) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI);
      const winnerIndex = Math.floor(normalizedAngle / slice) % n;
      drawWheel(owners, winnerIndex);
      onSpinComplete(winnerIndex);
    }
  }

  requestAnimationFrame(frame);
}

function onSpinComplete(winnerIndex) {
  spinning = false;
  const owners = _draftData.owners;
  const winner = owners[winnerIndex];
  const others = owners.filter((_, i) => i !== winnerIndex);
  // Shuffle remaining order
  const shuffled = [...others].sort(() => Math.random() - 0.5);
  const draftOrder = [winner, ...shuffled];

  document.getElementById('wheelResult').textContent = `🎉 ${winner.name} picks first!`;

  const orderDisplay = document.getElementById('order-display');
  orderDisplay.innerHTML = draftOrder.map((o, i) => `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);">
      <span style="font-size:.75rem;color:var(--muted);width:24px;">${i+1}.</span>
      <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:${o.color};flex-shrink:0;"></span>
      <strong>${o.name}</strong>
    </div>`).join('');

  document.getElementById('confirm-order').classList.remove('hidden');
  window._pendingDraftOrder = draftOrder.map(o => o.id);
}

async function confirmOrder() {
  const order = window._pendingDraftOrder;
  if (!order) return;
  _draftData.draftOrder = order;
  _draftData.draftStatus = 'open';
  _draftData.currentPickIndex = 0;
  const ok = await saveData(_draftData);
  if (ok) {
    invalidateCache();
    window.location.reload();
  }
}

// ── DRAFT UI ─────────────────────────────────────────────────────────
function renderDraftUI() {
  const data = _draftData;
  const seq = buildPickSequence(data.draftOrder, data.houseguests.length);
  const pickIndex = data.currentPickIndex;

  // Status bar
  const currentOwnerId = seq[pickIndex];
  const currentOwner = data.owners.find(o => o.id === currentOwnerId);
  const round = Math.floor(pickIndex / data.owners.length) + 1;

  document.getElementById('current-turn').innerHTML =
    `<span style="color:${currentOwner ? currentOwner.color : 'var(--gold)'}">${currentOwner ? currentOwner.name : '—'}</span> is on the clock`;
  document.getElementById('round-badge').textContent = `Round ${round}`;
  document.getElementById('draft-progress').textContent = `Pick ${pickIndex + 1} of ${data.houseguests.length}`;

  // Owner order cards
  const orderRow = document.getElementById('draft-order-row');
  orderRow.innerHTML = '';
  const picksPerOwner = {};
  data.owners.forEach(o => picksPerOwner[o.id] = data.houseguests.filter(h => h.ownerId === o.id).length);

  data.draftOrder.forEach(ownerId => {
    const owner = data.owners.find(o => o.id === ownerId);
    const card = document.createElement('div');
    card.className = `draft-order-card ${ownerId === currentOwnerId ? 'current' : ''}`;
    card.style.setProperty('--owner-color', owner ? owner.color : '#fff');
    card.innerHTML = `
      <div class="draft-order-num">Draft Order</div>
      <div class="draft-order-name" style="color:${owner ? owner.color : 'inherit'}">${owner ? owner.name : ownerId}</div>
      <div class="draft-picks-count">${picksPerOwner[ownerId] || 0} picks</div>`;
    orderRow.appendChild(card);
  });

  // Available houseguests
  const grid = document.getElementById('available-grid');
  grid.innerHTML = '';
  data.houseguests.forEach(hg => {
    const pickedOwner = hg.ownerId ? data.owners.find(o => o.id === hg.ownerId) : null;
    const card = document.createElement('div');
    card.className = `draft-hg-card ${hg.ownerId ? 'picked' : ''}`;
    if (!hg.ownerId) card.onclick = () => makePick(hg.id);

    card.innerHTML = `
      <div class="draft-hg-avatar">
        ${hg.photo ? `<img src="${hg.photo}" alt="${hg.name}">` : '👤'}
      </div>
      <div class="draft-hg-name">${hg.name}</div>
      <div class="draft-hg-meta">${hg.age ? hg.age + ' · ' : ''}${hg.hometown || ''}</div>
      ${pickedOwner
        ? `<div class="draft-picked-by" style="background:${pickedOwner.color}22;color:${pickedOwner.color}">${pickedOwner.name}</div>`
        : ''}`;
    grid.appendChild(card);
  });
}

async function makePick(houseguestId) {
  const data = _draftData;
  const seq = buildPickSequence(data.draftOrder, data.houseguests.length);
  const currentOwnerId = seq[data.currentPickIndex];

  const hg = data.houseguests.find(h => h.id === houseguestId);
  if (!hg || hg.ownerId) return;

  hg.ownerId = currentOwnerId;
  data.currentPickIndex++;

  if (data.currentPickIndex >= data.houseguests.length) {
    data.draftStatus = 'complete';
  }

  const ok = await saveData(data);
  if (ok) {
    invalidateCache();
    window.location.reload();
  }
}

// ── COMPLETE STATE ────────────────────────────────────────────────────
function renderTeams() {
  const data = _draftData;
  const container = document.getElementById('teams-display');
  data.owners.forEach(owner => {
    const players = data.houseguests.filter(h => h.ownerId === owner.id);
    const div = document.createElement('div');
    div.className = 'card';
    div.style.borderTop = `4px solid ${owner.color}`;
    div.innerHTML = `
      <h3 style="font-weight:800;margin-bottom:16px;">${owner.name}'s Team</h3>
      ${players.map(p => `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);">
          <div style="width:36px;height:36px;border-radius:50%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:1rem;overflow:hidden;flex-shrink:0;">
            ${p.photo ? `<img src="${p.photo}" style="width:100%;height:100%;object-fit:cover;border-radius:50%">` : '👤'}
          </div>
          <div>
            <div style="font-weight:600;font-size:.95rem;">${p.name}</div>
            <div style="font-size:.75rem;color:var(--muted);">${p.hometown || ''}</div>
          </div>
        </div>`).join('')}`;
    container.appendChild(div);
  });
}

// ── HELPERS ───────────────────────────────────────────────────────────
function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }
