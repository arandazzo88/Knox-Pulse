/**
 * POST /api/admin-change-pw — change the admin password (requires valid session)
 *
 * If KP_STORE KV is bound: stores the new hash in KV (takes precedence over env var).
 * If KP_STORE is not bound: returns the new hash so you can update ADMIN_PASSWORD_HASH
 *   in Cloudflare manually.
 */

import { sha256hex, verifyJwt, getSessionToken, getStoredHash, json } from '../_shared/auth.js';

export async function onRequestPost(context) {
  const { request, env } = context;

  if (!env.JWT_SECRET) return json({ error: 'Not configured.' }, 503);

  // Require valid session
  const token   = getSessionToken(request);
  const payload = token ? await verifyJwt(token, env.JWT_SECRET) : null;
  if (!payload) return json({ error: 'Not authenticated.' }, 401);

  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'Invalid request body.' }, 400); }

  const { password } = body || {};
  if (!password || typeof password !== 'string' || password.length < 8) {
    return json({ error: 'Password must be at least 8 characters.' }, 400);
  }

  const newHash = await sha256hex(password);

  if (env.KP_STORE) {
    await env.KP_STORE.put('admin_pw_hash', newHash);
    return json({ ok: true, message: 'Password updated.' });
  }

  // No KV — return hash so user can update env var manually
  return json({
    ok: true,
    action: 'manual',
    message: 'KP_STORE KV not bound. Update ADMIN_PASSWORD_HASH in Cloudflare environment variables to this value:',
    hash: newHash,
  });
}
