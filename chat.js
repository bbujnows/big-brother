// ══════════════════════════════════════════════════════════
//  BB28 LIVE CHAT  ·  chat.js
//
//  SETUP: Replace the 7 REPLACE_* values below with your
//  Firebase project config. See Firebase console →
//  Project Settings → Your apps → Config snippet.
// ══════════════════════════════════════════════════════════
import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import { getDatabase, ref, push, query, orderByChild, limitToLast, onChildAdded, remove } from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-database.js';

const firebaseConfig = {
  apiKey:            'AIzaSyBtZZ7groyieF-kMsvY4ASuZpJ6psQbwak',
  authDomain:        'bb28-fantasy.firebaseapp.com',
  databaseURL:       'https://bb28-fantasy-default-rtdb.firebaseio.com',
  projectId:         'bb28-fantasy',
  storageBucket:     'bb28-fantasy.firebasestorage.app',
  messagingSenderId: '165892895546',
  appId:             '1:165892895546:web:935f2cdd7611307b8d1603',
};

const app = initializeApp(firebaseConfig);
const db  = getDatabase(app);

const TOPICS = [
  { id: 'episode',   label: 'EPISODE'    },
  { id: 'afterdark', label: 'AFTER DARK' },
  { id: 'draft',     label: 'DRAFT'      },
  { id: 'general',   label: 'GENERAL'    },
];

let currentTopic = 'episode';
let unsubscribe  = null;
let isOpen       = false;

// ── Notifications ───────────────────────────────────────
const BOOT_TIME  = Date.now();
let unreadCount  = 0;
let titleFlasher = null;
const origTitle  = document.title;

function myName() {
  return (localStorage.getItem('bb28-chat-name') || '').trim().toLowerCase();
}

// Watch every topic in the background; announce posts from other people
function startBackgroundListeners() {
  TOPICS.forEach(t => {
    const q = query(ref(db, `posts/${t.id}`), orderByChild('timestamp'), limitToLast(1));
    onChildAdded(q, snap => {
      const msg = snap.val();
      if (!msg || (msg.timestamp || 0) <= BOOT_TIME) return;          // old news
      if ((msg.author || '').trim().toLowerCase() === myName()) return; // own post
      if (isOpen && t.id === currentTopic) return;                     // already reading it
      notifyNewMessage(msg.author, t.label);
    });
  });
}

function notifyNewMessage(author, topicLabel) {
  // 1. Unread badge + pulsing tab
  unreadCount++;
  const badge = document.getElementById('chat-tab-badge');
  const tab   = document.getElementById('chat-tab');
  if (badge) { badge.textContent = unreadCount > 9 ? '9+' : unreadCount; badge.classList.remove('hidden'); }
  if (tab) tab.classList.add('unread');

  // 2. Production announcement toast
  showAnnouncement(`${author} has posted to ${topicLabel}`);

  // 3. Feed-switch boop
  playBoop();

  // 4. Flash the browser tab title if the page is hidden
  if (document.hidden && !titleFlasher) {
    let on = false;
    titleFlasher = setInterval(() => {
      document.title = on ? origTitle : '🔴 NEW FEED ACTIVITY';
      on = !on;
    }, 1200);
  }
}

function clearNotifications() {
  unreadCount = 0;
  document.getElementById('chat-tab-badge')?.classList.add('hidden');
  document.getElementById('chat-tab')?.classList.remove('unread');
  stopTitleFlash();
}

function stopTitleFlash() {
  if (titleFlasher) { clearInterval(titleFlasher); titleFlasher = null; document.title = origTitle; }
}

// "HOUSEGUESTS, THIS IS BIG BROTHER" style toast
function showAnnouncement(text) {
  document.getElementById('bb-announce')?.remove();
  const el = document.createElement('div');
  el.id = 'bb-announce';
  el.innerHTML = `<span class="bb-announce-rec"></span><span class="bb-announce-hdr">📢 HOUSEGUESTS —</span> ${esc(text.toUpperCase())}`;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 400); }, 4500);
}

// Two-tone live-feed switch blip, synthesized (no audio file).
// Browsers allow sound only after the user's first click on the page;
// before that this silently does nothing.
let _audioCtx = null;
function playBoop() {
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (_audioCtx.state === 'suspended') { _audioCtx.resume(); }
    [[520, 0], [390, 0.14]].forEach(([freq, delay]) => {
      const osc  = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      const t = _audioCtx.currentTime + delay;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.05, t + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      osc.connect(gain).connect(_audioCtx.destination);
      osc.start(t);
      osc.stop(t + 0.14);
    });
  } catch (e) { /* sound blocked — badge and toast still fire */ }
}

// ── Inject drawer HTML into body ────────────────────────
function injectDrawer() {
  const topicBtns = TOPICS.map((t, i) =>
    `<button class="chat-topic-btn${i === 0 ? ' active' : ''}" data-topic="${t.id}">${t.label}</button>`
  ).join('');

  document.body.insertAdjacentHTML('beforeend', `
    <div id="chat-tab" role="button" tabindex="0" aria-label="Open live chat">
      <span id="chat-tab-badge" class="hidden"></span>
      <span class="chat-tab-dot"></span>
      <span class="chat-tab-lbl">LIVE</span>
    </div>

    <aside id="chat-drawer" aria-hidden="true" aria-label="Live Chat">
      <div class="chat-hdr">
        <span class="rec"><i></i>COMM&nbsp;LINK</span>
        <span class="chat-hdr-sub">LIVE FEED · HOUSE</span>
        <button id="chat-close" aria-label="Close chat">✕</button>
      </div>
      <div id="chat-topics">${topicBtns}</div>
      <div id="chat-msgs"></div>
      <div class="chat-footer">
        <input id="chat-name" placeholder="YOUR NAME" maxlength="20" autocomplete="off" spellcheck="false">
        <div class="chat-compose">
          <textarea id="chat-text" placeholder="POST TO LIVE FEED…" rows="2" maxlength="500"></textarea>
          <button id="chat-send">SEND</button>
        </div>
      </div>
    </aside>
  `);
}

// ── Open / close ────────────────────────────────────────
function toggleChat() {
  isOpen = !isOpen;
  const drawer = document.getElementById('chat-drawer');
  const tab    = document.getElementById('chat-tab');
  drawer.classList.toggle('open', isOpen);
  drawer.setAttribute('aria-hidden', String(!isOpen));
  tab.classList.toggle('hidden', isOpen);
  if (isOpen) { loadMessages(currentTopic); clearNotifications(); }
}

// ── Switch topic tab ────────────────────────────────────
function switchTopic(id) {
  if (id === currentTopic) return;
  currentTopic = id;
  document.querySelectorAll('.chat-topic-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.topic === id)
  );
  loadMessages(id);
}

// ── Load messages via Firebase realtime listener ────────
function loadMessages(topicId) {
  if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  document.getElementById('chat-msgs').innerHTML = '';
  const q = query(
    ref(db, `posts/${topicId}`),
    orderByChild('timestamp'),
    limitToLast(120)
  );
  unsubscribe = onChildAdded(q, snap => {
    appendMessage(snap);
    scrollBottom();
  });
}

// ── Render one message bubble ───────────────────────────
function appendMessage(snap) {
  const msg = snap.val();
  const key = snap.key;
  const d   = new Date(msg.timestamp || 0);
  const pad = n => String(n).padStart(2, '0');
  const ts  = `${pad(d.getMonth()+1)}·${pad(d.getDate())}·${String(d.getFullYear()).slice(2)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;

  const el = document.createElement('div');
  el.className = 'chat-msg';
  el.dataset.key = key;
  el.innerHTML = `
    <div class="chat-msg-meta">
      <span class="chat-msg-author">${esc(msg.author)}</span>
      <span class="chat-msg-time">${ts}</span>
      <button class="chat-msg-del" title="Admin delete">✕</button>
    </div>
    <div class="chat-msg-body">${esc(msg.text)}</div>
  `;
  el.querySelector('.chat-msg-del').addEventListener('click', () => deleteMsg(key));
  document.getElementById('chat-msgs').appendChild(el);
}

// ── Auto-scroll if already near bottom ──────────────────
function scrollBottom() {
  const el = document.getElementById('chat-msgs');
  if (!el) return;
  if (el.scrollHeight - el.scrollTop <= el.clientHeight + 80) {
    el.scrollTop = el.scrollHeight;
  }
}

// ── Send a post ─────────────────────────────────────────
function sendMessage() {
  const nameEl = document.getElementById('chat-name');
  const textEl = document.getElementById('chat-text');
  const name   = nameEl.value.trim();
  const text   = textEl.value.trim();

  if (!name) { nameEl.focus(); nameEl.classList.add('error'); return; }
  nameEl.classList.remove('error');
  if (!text) { textEl.focus(); return; }

  localStorage.setItem('bb28-chat-name', name);
  push(ref(db, `posts/${currentTopic}`), { author: name, text, timestamp: Date.now() });
  textEl.value = '';
  textEl.focus();
}

// ── Admin delete ────────────────────────────────────────
function deleteMsg(key) {
  const pw = prompt('Admin password:');
  if (pw === 'bbseason28') {
    remove(ref(db, `posts/${currentTopic}/${key}`));
    document.querySelector(`.chat-msg[data-key="${CSS.escape(key)}"]`)?.remove();
  }
}

// ── Escape HTML (also converts newlines to <br>) ────────
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/\n/g, '<br>');
}

// ── Bootstrap ───────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  injectDrawer();

  // Name pre-fill: ?name= in the URL wins (bookmark-friendly for browsers
  // that clear site data on exit), then last saved name
  const nameEl   = document.getElementById('chat-name');
  const urlName  = new URLSearchParams(location.search).get('name');
  const saved    = localStorage.getItem('bb28-chat-name');
  if (urlName) {
    nameEl.value = urlName.slice(0, 20);
    localStorage.setItem('bb28-chat-name', nameEl.value);
  } else if (saved) {
    nameEl.value = saved;
  }
  // Save as soon as it's typed, not only on send
  nameEl.addEventListener('input', () => {
    const v = nameEl.value.trim();
    if (v) localStorage.setItem('bb28-chat-name', v);
  });

  const tab = document.getElementById('chat-tab');
  tab.addEventListener('click', toggleChat);
  tab.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleChat(); }
  });

  document.getElementById('chat-close').addEventListener('click', toggleChat);
  document.getElementById('chat-topics').addEventListener('click', e => {
    if (e.target.matches('.chat-topic-btn')) switchTopic(e.target.dataset.topic);
  });
  document.getElementById('chat-send').addEventListener('click', sendMessage);
  document.getElementById('chat-text').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  startBackgroundListeners();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) stopTitleFlash(); });
  // Unlock audio on the first interaction so later boops can play
  document.addEventListener('click', () => playBoopUnlock(), { once: true });
});

// Prime the AudioContext silently on first click (browser autoplay rule)
function playBoopUnlock() {
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (_audioCtx.state === 'suspended') _audioCtx.resume();
  } catch (e) { /* no audio support */ }
}
