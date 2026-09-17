import crypto from 'node:crypto';

const OWNER = 'Photofiver';
const REPO = 'Stock-finder';
const WORKFLOW = 'blofin_live_manual.yml';
const TTL_MS = 2 * 60 * 1000;

function first(value) {
  return Array.isArray(value) ? value[0] : value;
}

function safeEqualHex(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  if (!/^[0-9a-f]{64}$/i.test(a) || !/^[0-9a-f]{64}$/i.test(b)) return false;
  const aa = Buffer.from(a, 'hex');
  const bb = Buffer.from(b, 'hex');
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).send('POST only');
  }

  const token = process.env.GH_APPROVE_TOKEN;
  if (!token) return res.status(500).send('Relay not configured');

  const q = req.query || {};
  const signalId = String(first(q.signal_id) || '');
  const inst = String(first(q.inst) || '');
  const side = String(first(q.side) || '');
  const signalCloseMs = String(first(q.signal_close_ms) || '');
  const createdAtMs = String(first(q.created_at_ms) || '');
  const expiresAtMs = String(first(q.expires_at_ms) || '');
  const signature = String(first(q.signature) || '');

  if (!signalId || !inst || !['LONG', 'SHORT'].includes(side)) {
    return res.status(400).send('Invalid signal');
  }

  const created = Number(createdAtMs);
  const expires = Number(expiresAtMs);
  const now = Date.now();
  if (!Number.isFinite(created) || !Number.isFinite(expires)) {
    return res.status(400).send('Invalid timestamp');
  }
  if (created <= 0 || expires <= created || expires - created > TTL_MS) {
    return res.status(400).send('Invalid approval window');
  }
  if (now > expires || now - created > TTL_MS || created - now > 30_000) {
    return res.status(410).send('Signal expired');
  }

  const canonical = [
    signalId,
    inst,
    side,
    signalCloseMs,
    createdAtMs,
    expiresAtMs,
  ].join('|');
  const expected = crypto.createHmac('sha256', token).update(canonical).digest('hex');
  if (!safeEqualHex(signature, expected)) {
    return res.status(403).send('Invalid signature');
  }

  const github = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'blofin-one-click-relay',
      },
      body: JSON.stringify({
        ref: 'main',
        inputs: {
          signal_id: signalId,
          inst,
          side,
          signal_close_ms: signalCloseMs,
          created_at_ms: createdAtMs,
          expires_at_ms: expiresAtMs,
          signature,
        },
      }),
    },
  );

  if (github.status !== 204) {
    return res.status(502).send(`GitHub dispatch failed (${github.status})`);
  }

  return res.status(200).send('Zatwierdzone');
}
