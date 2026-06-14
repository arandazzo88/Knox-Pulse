/**
 * Shared auth helpers for Knox Pulse admin API.
 * All crypto runs in the Cloudflare Workers runtime via Web Crypto API.
 */

export const COOKIE_NAME = 'kp_admin_session';
export const SESSION_HOURS = 8;

// ── Password hashing ─────────────────────────────────────────────────────────

export async function sha256hex(str) {
  const buf = await crypto.subtle.digest('SHA-256', enc(str));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}

// ── JWT (HS256) ───────────────────────────────────────────────────────────────

export async function createJwt(secret) {
  const now = Math.floor(Date.now() / 1000);
  const header  = b64url(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const payload = b64url(JSON.stringify({ sub: 'admin', iat: now, exp: now + SESSION_HOURS * 3600 }));
  const msg = `${header}.${payload}`;
  const key = await importHmac(secret, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', key, enc(msg));
  return `${msg}.${b64url(sig)}`;
}

export async function verifyJwt(token, secret) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const [header, payload, sig] = parts;
    const key = await importHmac(secret, ['verify']);
    const valid = await crypto.subtle.verify('HMAC', key, b64urlDecode(sig), enc(`${header}.${payload}`));
    if (!valid) return null;
    const data = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/').padEnd(
      payload.length + (4 - payload.length % 4) % 4, '='
    )));
    if (data.exp < Math.floor(Date.now() / 1000)) return null;
    return data;
  } catch {
    return null;
  }
}

// ── Stored password resolution ────────────────────────────────────────────────
// KV override takes precedence over env var, so in-app password changes work
// without redeploying. If KP_STORE KV is not bound, env var is used.

export async function getStoredHash(env) {
  if (env.KP_STORE) {
    const kv = await env.KP_STORE.get('admin_pw_hash').catch(() => null);
    if (kv) return kv;
  }
  return env.ADMIN_PASSWORD_HASH || null;
}

// ── Cookie helpers ────────────────────────────────────────────────────────────

export function getSessionToken(request) {
  const header = request.headers.get('Cookie') || '';
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === COOKIE_NAME) return part.slice(eq + 1).trim();
  }
  return null;
}

export function sessionCookieHeader(token) {
  return `${COOKIE_NAME}=${token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${SESSION_HOURS * 3600}`;
}

export function clearCookieHeader() {
  return `${COOKIE_NAME}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0`;
}

// ── Response shorthand ────────────────────────────────────────────────────────

export function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
  });
}

// ── Internal helpers ──────────────────────────────────────────────────────────

async function importHmac(secret, usages) {
  return crypto.subtle.importKey('raw', enc(secret), { name: 'HMAC', hash: 'SHA-256' }, false, usages);
}

function enc(str) { return new TextEncoder().encode(str); }

function b64url(data) {
  const s = typeof data === 'string' ? data : String.fromCharCode(...new Uint8Array(data));
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

function b64urlDecode(str) {
  const b64 = str.replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(b64.padEnd(b64.length + (4 - b64.length % 4) % 4, '=')), c => c.charCodeAt(0));
}
