/**
 * POST /api/admin-logout — clears the session cookie
 */

import { clearCookieHeader, json } from '../_shared/auth.js';

export async function onRequestPost() {
  return json({ ok: true }, 200, { 'Set-Cookie': clearCookieHeader() });
}
