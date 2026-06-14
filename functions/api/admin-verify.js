/**
 * GET /api/admin-verify — checks whether the caller has a valid session cookie
 * Returns 200 {ok:true} if valid, 401 otherwise.
 */

import { verifyJwt, getSessionToken, json } from '../_shared/auth.js';

export async function onRequestGet(context) {
  const { request, env } = context;

  if (!env.JWT_SECRET) return json({ error: 'Not configured.' }, 503);

  const token   = getSessionToken(request);
  if (!token)   return json({ error: 'No session.' }, 401);

  const payload = await verifyJwt(token, env.JWT_SECRET);
  if (!payload) return json({ error: 'Session expired or invalid.' }, 401);

  return json({ ok: true });
}
