// apply-event-fixes.mjs
// Pushes the corrected/enriched event records into the live Firestore
// `communityEvents` collection, keyed by id (merge: keeps submittedAt).
//
// Usage:
//   cd Knox-Pulse
//   npm install firebase
//   node scripts/apply-event-fixes.mjs            # dry run (prints, no writes)
//   node scripts/apply-event-fixes.mjs --write    # actually writes to Firestore
//
// Firestore rules already allow unauthenticated update on communityEvents,
// so no service-account key is needed.

import { readFileSync } from 'node:fs';
import { initializeApp } from 'firebase/app';
import { getFirestore, doc, setDoc } from 'firebase/firestore';

const firebaseConfig = {
  apiKey: 'AIzaSyBCb7L2HgVzhPlh_xSQCxuBipuEyZdpjF8',
  authDomain: 'knox-pulse.firebaseapp.com',
  projectId: 'knox-pulse',
  storageBucket: 'knox-pulse.firebasestorage.app',
  messagingSenderId: '512082235444',
  appId: '1:512082235444:web:86b5d56f8d9993cb193a61',
};

const WRITE = process.argv.includes('--write');
const events = JSON.parse(readFileSync(new URL('../data/events-corrected.json', import.meta.url)));

const app = initializeApp(firebaseConfig);
const db = getFirestore(app);

let ok = 0, fail = 0;
for (const e of events) {
  if (!e.id) { console.warn('skip (no id):', e.title); continue; }
  // Keep the live community schema fields the site reads. normalizeEvent() in
  // index.html canonicalizes on load, but we write clean values here too.
  const rec = {
    title: e.title, emoji: e.emoji || '📍', category: e.category,
    neighborhood: e.neighborhood || '', description: e.description || '',
    location: e.location || e.address || '', venueName: e.venue || '',
    image: e.image || e.coverPhoto || '',
    lat: e.lat ?? e.latitude ?? null, lng: e.lng ?? e.longitude ?? null,
    eventType: e.eventType,                 // 'one-time' | 'recurring' | 'venue'
    eventDate: e.eventDate || e.startDate || '',
    eventEndDate: e.eventEndDate || e.endDate || '',
    daysOfWeek: e.daysOfWeek || [],
    recurrenceRule: e.recurrenceRule || null,
    startTime: e.startTime || '',           // 24-hour "HH:MM"
    endTime: e.endTime || '',
    price: e.price || '', costScale: e.costScale || '',
    indoorOutdoor: e.indoorOutdoor || '', ageRestrictions: e.ageRestrictions || e.ages || '',
    timeOfDay: e.timeOfDay || '', season: e.season || '',
    strollerFriendly: !!(e.strollerFriendly || e.strollerAccessible),
    tags: e.tags || [], phone: e.phone || '', website: e.website || '',
    email: e.email || '', instagram: e.instagram || '',
    source: 'json-upload-corrected-2026-06-14',
  };
  if (!WRITE) { ok++; continue; }
  try {
    await setDoc(doc(db, 'communityEvents', e.id), rec, { merge: true });
    ok++;
    if (ok % 20 === 0) console.log(`  ...${ok} written`);
  } catch (err) { fail++; console.error('FAIL', e.id, err.message); }
}

console.log(WRITE
  ? `Done. ${ok} written, ${fail} failed.`
  : `Dry run: ${ok} records ready. Re-run with --write to apply.`);
process.exit(0);
