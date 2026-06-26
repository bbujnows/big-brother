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

// ── Inject drawer HTML into body ────────────────────────
function injectDrawer() {
  const topicBtns = TOPICS.map((t, i) =>
    `<button class="chat-topic-btn${i === 0 ? ' active' : ''}" data-topic="${t.id}">${t.label}</button>`
  ).join('');

  document.body.insertAdjacentHTML('beforeend', `
    <div id="chat-tab" role="button" tabindex="0" aria-label="Open live chat">
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
  if (isOpen) loadMessages(currentTopic);
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

  const saved = localStorage.getItem('bb28-chat-name');
  if (saved) document.getElementById('chat-name').value = saved;

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
});
