'use strict';
/**
 * Browser drive bridge — Unix socket NDJSON, modelled on provider_bridge.js.
 *
 * Voice (the Python-side BrowserEngine) dials this socket to drive the hardened
 * window in browser_window.js: it connects, presents the per-launch token,
 * sends one newline-delimited JSON drive frame, and receives one reply frame.
 * The credential story here is deliberately absent: there is no API key,
 * secret, or cookie on this channel. The token authorises "speak the drive
 * protocol" to this process, nothing more, and it is minted per launch.
 * The URL in a frame is the only thing that leaves the machine, and
 * `resolve_http_url` on the Python side is the only place it becomes bytes —
 * the window itself refuses non-http(s) targets in its handlers.
 *
 * One fixed set of ops — identical to the wire contract in browser_ipc.py
 * AND the window's handlers — keeps the three surfaces aligned. Anything
 * outside that set is refused with a fixed sentence: an unknown op closes
 * the socket with no reply carrying page data, mirroring handleDrive.
 */
const crypto = require('crypto');
const fs = require('fs');
const net = require('net');

const MAX_FRAME_BYTES = 256 * 1024;
const IDLE_TIMEOUT_MS = 30_000;
const PROBE_TIMEOUT_MS = 500;

const OP_NAMES = new Set([
  'open',
  'navigate',
  'back',
  'forward',
  'refresh',
  'snapshot',
  'click',
  'type',
  'select',
  'scroll',
  'wait',
  'stop',
  'state',
  'page_text',
]);

const BAD_TOKEN_DETAIL = 'The request token was not accepted.';
const MALFORMED_FRAME_DETAIL = 'The request frame was not a JSON object.';
const UNKNOWN_OP_DETAIL = 'The request named an operation this bridge does not serve.';

/**
 * Is a live process answering on this path?
 *
 * A Unix socket file survives a crash; connecting to one that nobody listens on gives
 * ECONNREFUSED. Connecting is the only reliable test — a file's presence proves nothing.
 */
function probeLive(socketPath) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const socket = net.connect({ path: socketPath });
    const done = (live) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try { socket.destroy(); } catch { /* already gone */ }
      resolve(live);
    };
    timer = setTimeout(() => done(false), PROBE_TIMEOUT_MS);
    socket.once('connect', () => done(true));
    socket.once('error', () => done(false));
  });
}

/**
 * Remove a stale socket, refuse a live one.
 *
 * A non-socket at this path is somebody else's file: deleting it because it happens to
 * share a name would be destructive, so that case is an error, not a cleanup.
 */
async function clearStaleSocket(socketPath) {
  let stat;
  try {
    stat = fs.lstatSync(socketPath);
  } catch (error) {
    if (error && error.code === 'ENOENT') return;
    throw error;
  }
  if (!stat.isSocket()) {
    throw new Error(`The browser bridge path exists and is not a socket: ${socketPath}`);
  }
  if (await probeLive(socketPath)) {
    throw new Error('A browser bridge is already listening on this socket.');
  }
  try {
    fs.unlinkSync(socketPath);
  } catch (error) {
    if (!error || error.code !== 'ENOENT') throw error;
  }
}

/**
 * Start the drive bridge.
 *
 * @param {object} options
 * @param {string} options.socketPath  absolute path; caller supplies app.getPath()
 * @param {string} options.token       per-launch secret; never stored by this module
 * @param {object} options.drive       { handleDrive(frame) -> Promise<reply> } from
 *                                      browser_window.js
 * @param {Function} [options.log]     called with fixed, key-free sentences only
 * @returns {Promise<{socketPath:string, close:Function, stats:Function}>}
 */
async function startBridge(options) {
  const source = options && typeof options === 'object' ? options : {};
  const socketPath = String(source.socketPath || '');
  const token = String(source.token || '');
  const drive = source.drive;
  if (!socketPath) throw new Error('The browser bridge needs a socket path.');
  if (!token) throw new Error('The browser bridge needs a token.');
  if (!drive || typeof drive.handleDrive !== 'function') {
    throw new Error('The browser bridge needs a drive target with handleDrive.');
  }

  await clearStaleSocket(socketPath);

  // Compared on every request; held as bytes so the compare is on equal units.
  const expectedToken = Buffer.from(token, 'utf8');

  const sockets = new Set();
  const counters = {
    connections: 0,
    frames: 0,
    served: 0,
    authFailures: 0,
    oversized: 0,
    malformed: 0,
    unknownOp: 0,
    timeouts: 0,
  };
  let closing = false;

  function note(message) {
    if (typeof source.log !== 'function') return;
    try { source.log(message); } catch { /* logging must never break serving */ }
  }

  function authorised(candidate) {
    if (typeof candidate !== 'string') return false;
    const given = Buffer.from(candidate, 'utf8');
    // Length first: timingSafeEqual throws on different lengths, and a throw here
    // would be an information leak dressed as a crash.
    if (given.length !== expectedToken.length) return false;
    try {
      return crypto.timingSafeEqual(given, expectedToken);
    } catch {
      return false;
    }
  }

  const server = net.createServer((socket) => {
    counters.connections += 1;
    sockets.add(socket);
    let buffer = Buffer.alloc(0);
    let done = false;
    let idleTimer = setTimeout(() => {
      counters.timeouts += 1;
      socket.destroy();
    }, IDLE_TIMEOUT_MS);

    const finish = () => {
      if (done) return;
      done = true;
      if (idleTimer) clearTimeout(idleTimer);
      try { socket.end(); } catch { /* already gone */ }
    };

    socket.on('data', async (chunk) => {
      if (done) return;
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > MAX_FRAME_BYTES) {
        counters.oversized += 1;
        socket.destroy();
        return;
      }
      const newline = buffer.indexOf(0x0a);
      if (newline === -1) return; // wait for a full frame
      const line = buffer.slice(0, newline).toString('utf8');
      counters.frames += 1;
      let frame;
      try {
        frame = JSON.parse(line);
      } catch {
        counters.malformed += 1;
        socket.write(JSON.stringify({ ok: false, detail: MALFORMED_FRAME_DETAIL }) + '\n');
        finish();
        return;
      }
      if (!frame || typeof frame !== 'object') {
        counters.malformed += 1;
        socket.write(JSON.stringify({ ok: false, detail: MALFORMED_FRAME_DETAIL }) + '\n');
        finish();
        return;
      }
      if (!authorised(frame.token)) {
        counters.authFailures += 1;
        socket.write(JSON.stringify({ ok: false, detail: BAD_TOKEN_DETAIL }) + '\n');
        finish();
        return;
      }
      const op = frame.op;
      if (typeof op !== 'string' || !OP_NAMES.has(op)) {
        counters.unknownOp += 1;
        socket.write(JSON.stringify({ ok: false, detail: UNKNOWN_OP_DETAIL }) + '\n');
        finish();
        return;
      }
      // One frame per connection, then close. The window's reply is relayed
      // verbatim; it already carries only the fixed fields the contract allows.
      try {
        const reply = await drive.handleDrive(frame);
        counters.served += 1;
        socket.write(JSON.stringify(reply) + '\n');
      } catch {
        counters.malformed += 1;
        socket.write(JSON.stringify({ ok: false, detail: 'drive failed' }) + '\n');
      }
      finish();
    });

    socket.on('error', () => { /* peer gone; nothing to do */ });
    socket.on('close', () => {
      if (idleTimer) clearTimeout(idleTimer);
      sockets.delete(socket);
    });
  });

  server.on('error', (err) => {
    note(`browser bridge listen error: ${err && err.message}`);
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, () => {
      server.removeListener('error', reject);
      resolve();
    });
  });
  // File permissions: only the owning user may connect. The socket was created
  // with the process umask; 0600 matches the settings file's protection.
  try {
    fs.chmodSync(socketPath, 0o600);
  } catch { /* best effort — umask already restricted it */ }

  note('browser bridge listening');

  async function close() {
    if (closing) return;
    closing = true;
    for (const socket of sockets) {
      try { socket.destroy(); } catch { /* already gone */ }
    }
    sockets.clear();
    await new Promise((resolve) => server.close(() => resolve()));
    try {
      fs.unlinkSync(socketPath);
    } catch { /* already gone */ }
  }

  function stats() {
    return { ...counters };
  }

  return { socketPath, close, stats };
}

module.exports = { startBridge, MAX_FRAME_BYTES, IDLE_TIMEOUT_MS, OP_NAMES };
