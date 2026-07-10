// ══════════════════════════════════════════════════════════
//  BB28 LIVE DATA SYNC  ·  live.js
//
//  Subscribes to the Firebase Realtime Database and broadcasts
//  every change to gameData as a 'bb28-live-data' window event,
//  so plain (non-module) page scripts can react instantly
//  without polling or refreshing.
// ══════════════════════════════════════════════════════════
import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import { getDatabase, ref, onValue } from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-database.js';

// Named app so we don't collide with chat.js's default Firebase app
const app = initializeApp(
  { databaseURL: 'https://bb28-fantasy-default-rtdb.firebaseio.com' },
  'bb28-live'
);
const db = getDatabase(app);

onValue(ref(db, 'gameData'), snap => {
  const data = snap.val();
  if (data && data.owners) {
    window.dispatchEvent(new CustomEvent('bb28-live-data', { detail: data }));
  }
});
