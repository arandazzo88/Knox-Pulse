/**
 * POST /api/admin-auth — verify password, issue session cookie
 *
 * Required Cloudflare env vars (Pages → Settings → Environment variables):
 *   ADMIN_PASSWORD_HASH   SHA-256 hex of the admin password
 *   JWT_SECRET            Random 32+ char string for signing tokens
 *
 * Optional KV binding (enables in-app password changes without redeploy):
 *   KP_STORE              KV namespace bound to this Pages project
 */

import { sha256hex, createJwt, getStoredHash, sessionCookieHeader, json } from '../_shared/auth.js';

// In-memory rate limit store — persists within a Worker isolate.
// Resets on cold start; good enough to stop automated brute-force.
const rl = new Map();
const MAX_ATTEMPTS = 5;
const WINDOW_MS    = 15 * 60 * 1000;  // 15 min sliding window
const LOCKOUT_MS   = 15 * 60 * 1000;  // 15 min lockout after max attempts

export async function onRequestPost(context) {
  const { request, env } = context;

  if (!env.ADMIN_PASSWORD_HASH || !env.JWT_SECRET) {
    return json({ error: 'Server auth not configured. Set ADMIN_PASSWORD_HASH and JWT_SECRET in Cloudflare.' }, 503);
  }

  // ── Rate limiting ───────────────────────────────────────────────────────────
  const ip  = request.headers.get('CF-Connecting-IP') || 'unknown';
  const now = Date.now();
  let rec   = rl.get(ip) || { count: 0, windowStart: now };

  if (rec.lockUntil && now < rec.lockUntil) {
    const mins = Math.ceil((rec.lockUntil - now) / 60000);
    return json({ error: `Too many failed attempts. Try again in ${mins} minute${mins !== 1 ? 's' : ''}.` }, 429);
  }

  if (now - rec.windowStart > WINDOW_MS) {
    rec = { count: 0, windowStart: now };
  }

  // ── Parse body ──────────────────────────────────────────────────────────────
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'Invalid request body.' }, 400); }

  const { password } = body || {};
  if (!password || typeof password !== 'string' || password.length > 256) {
    return json({ error: 'Password required.' }, 400);
  }

  // ── Verify password ─────────────────────────────────────────────────────────
  const [enteredHash, storedHash] = await Promise.all([
    sha256hex(password),
    getStoredHash(env),
  ]);

  if (!storedHash || enteredHash !== storedHash) {
    rec.count++;
    if (rec.count >= MAX_ATTEMPTS) {
      rec.lockUntil = now + LOCKOUT_MS;
      rl.set(ip, rec);
      return json({ error: 'Too many failed attempts. Locked out for 15 minutes.' }, 429);
    }
    rl.set(ip, rec);
    const left = MAX_ATTEMPTS - rec.count;
    return json({ error: `Incorrect password. ${left} attempt${left !== 1 ? 's' : ''} remaining.` }, 401);
  }

  // ── Success ─────────────────────────────────────────────────────────────────
  rl.delete(ip);
  const token = await createJwt(env.JWT_SECRET);

  return json({ ok: true }, 200, { 'Set-Cookie': sessionCookieHeader(token) });
}
