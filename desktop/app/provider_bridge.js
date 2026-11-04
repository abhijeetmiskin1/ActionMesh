'use strict';
/**
 * Provider bridge — a Unix domain socket inside the main process.
 *
 * VoiceOS runs in a separate Python process. It needs to ask a model for a plan, but it
 * must never receive the provider API key: keyboard/argv/file leaks aside, the whole
 * point of keeping the credential in the OS keyring is that exactly one process holds
 * it. So the Python side connects here, presents a per-launch token that authorises
 * "talk to this socket" (nothing more), and receives only the model's parsed answer.
 *
 * The shape of the guard rails:
 *
 *   * one request per connection, newline-delimited JSON, 256 KiB ceiling — a frame
 *     over the limit is counted and the socket destroyed, not buffered;
 *   * the token is compared with `crypto.timingSafeEqual`, after an explicit length
 *     check so the compare can never throw; a mismatch closes without a reply;
 *   * settings and the key are read *inside* each request, so changing the provider in
 *     the UI takes effect on the next call with no restart and no cached credential;
 *   * every `detail` sentence is written in this file. A provider's error body can
 *     quote the request, so provider text is never relayed — a guard that echoes its
 *     input is not a guard;
 *   * nothing here calls `console.log`. The optional `log` hook receives fixed,
 *     key-free lifecycle sentences only.
 *
 * Status is about *verified* state, not configured state: a key on disk is not a
 * provider that answers. The caller (main.js) already keeps the last verified outcome,
 * so `getStatus` is injected and preferred; absent it, this module falls back to the
 * same conservative inference, and a generate that actually reached the provider is
 * recorded through `noteTestResult` if one is supplied.
 */
const crypto = require('crypto');
const fs = require('fs');
const net = require('net');

const MAX_FRAME_BYTES = 256 * 1024;
const IDLE_TIMEOUT_MS = 30_000;
const PROBE_TIMEOUT_MS = 500;

// The closed vocabulary from the contract. Anything a provider returns outside it is
// reported as `provider_unavailable` rather than passed through.
const KNOWN_STATES = new Set([
  'ok',
  'missing_key',
  'not_configured',
  'provider_unavailable',
  'invalid_credentials',
  'rate_limited',
  'timeout',
  'malformed',
]);

// Every sentence the peer can ever see. Written here so no provider body can reach it.
const STATE_DETAIL = {
  not_configured: 'No provider is configured for this app.',
  missing_key: 'No API key is stored for this provider.',
  provider_unavailable: 'The provider could not answer the request.',
  invalid_credentials: 'The provider rejected the stored API key.',
  rate_limited: 'The provider is rate limiting this key. Try again shortly.',
  timeout: 'The provider did not answer within the timeout.',
  malformed: 'The provider did not return the expected JSON object.',
};

const MALFORMED_FRAME_DETAIL = 'The request frame was not a JSON object.';
const UNKNOWN_OP_DETAIL = 'The request named an operation this bridge does not serve.';
const BAD_PURPOSE_DETAIL = 'The request must name purpose "plan" or "diagnose".';
const BAD_PROMPT_DETAIL = 'The request must carry both a system and a user string.';
const NO_GENERATE_DETAIL = 'Model generation is not available in this build.';
const SETTINGS_UNREADABLE_DETAIL = 'The stored provider settings could not be read.';
const BAD_TOKEN_DETAIL = 'The request token was not accepted.';

//: Which lane answers a VoiceOS request. Planning and diagnosis are both reasoning
//: work; the chat lane is the fallback because it is the one a pre-C42 setup is
//: already configured in, and because a person who set up "the AI" once expects the
//: planner to keep using it until they say otherwise. A chat lane the user marked
//: default goes first (`purposeDefaultLane`); otherwise Reasoning wins whenever it is
//: configured, so changing Reasoning changes the planner and only the planner.
const DEFAULT_LANES = ['chat', 'reasoning'];
const REASONING_LANES = ['reasoning', 'chat'];

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
    throw new Error(`The provider bridge path exists and is not a socket: ${socketPath}`);
  }
  if (await probeLive(socketPath)) {
    throw new Error('A provider bridge is already listening on this socket.');
  }
  try {
    fs.unlinkSync(socketPath);
  } catch (error) {
    if (!error || error.code !== 'ENOENT') throw error;
  }
}

/**
 * Start the bridge.
 *
 * @param {object} options
 * @param {string} options.socketPath  absolute path; the caller supplies app.getPath()
 * @param {string} options.token       per-launch secret; never stored by this module
 * @param {object} options.settings    SettingsStore-shaped: get / secret / hasSecret
 * @param {object} options.providers   module with generate(config, request)
 * @param {Function} [options.log]     called with fixed, key-free sentences only
 * @param {Function} [options.getStatus] main.js's verified-state view (preferred)
 * @param {Function} [options.noteTestResult] main.js's cache sink (optional)
 * @returns {Promise<{socketPath:string, close:Function, noteTestResult:Function, stats:Function}>}
 */
async function startBridge(options) {
  const source = options && typeof options === 'object' ? options : {};
  const socketPath = String(source.socketPath || '');
  const token = String(source.token || '');
  const settings = source.settings;
  const providers = source.providers;
  const getStatus = typeof source.getStatus === 'function' ? source.getStatus : null;
  const noteTestResult = typeof source.noteTestResult === 'function' ? source.noteTestResult : null;

  if (!socketPath) throw new Error('The provider bridge needs a socket path.');
  if (!token) throw new Error('The provider bridge needs a token.');
  if (!settings || typeof settings.get !== 'function' || typeof settings.secret !== 'function') {
    throw new Error('The provider bridge needs a settings store.');
  }
  if (!providers || typeof providers !== 'object') {
    throw new Error('The provider bridge needs the providers module.');
  }

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
  // The last state a real call established. Kept even when main.js supplies getStatus,
  // because a generate through this socket is itself verification.
  let verified = null;
  let closed = false;

  function note(message) {
    if (typeof source.log !== 'function') return;
    try { source.log(message); } catch { /* logging must never break serving */ }
  }

  function recordVerified(state) {
    verified = { state };
    if (!noteTestResult) return;
    try { noteTestResult(state); } catch { /* main owns its cache; a failure is theirs */ }
  }

  /**
   * The lanes to resolve for a planning request, in order.
   *
   * `providerDefault` is the "Set as default" switch from the chat card. It is read
   * here — and only here — because this is the one consumer with a notion of "the AI
   * this app uses". Reading it is what makes the button an action rather than a badge:
   * with it on, a configured chat lane goes ahead of a configured reasoning lane for
   * the planner. It does not change anything else, and it grants nothing.
   */
  function planningLanes() {
    let chatIsDefault = false;
    try {
      chatIsDefault = Boolean(settings.get('providerDefault'));
    } catch {
      chatIsDefault = false;
    }
    return chatIsDefault ? DEFAULT_LANES : REASONING_LANES;
  }

  /**
   * The per-lane configuration VoiceOS should use, resolved in order.
   *
   * A lane is "set" when it names a provider. Its key falls back to the legacy chat
   * secret (``providerApiKey``) only for the chat lane itself — that is the pre-C42
   * slot, and it belongs to chat. Every other lane reads only its own secret, so a
   * key entered for Reasoning can never be spent by Image or by Chat.
   *
   * Returns the first lane that names a provider, plus whether that lane is the
   * legacy chat one, so the status frame and the generate path report the same thing.
   */
  function laneFor(lanes) {
    for (const lane of lanes) {
      let providerId = '';
      let model = '';
      let endpoint = '';
      let timeout = 0;
      let key = '';
      try {
        providerId = String(settings.get(`purpose_${lane}_provider`) || '');
        model = String(settings.get(`purpose_${lane}_model`) || '');
        endpoint = String(settings.get(`purpose_${lane}_endpoint`) || '');
        timeout = Number(settings.get(`purpose_${lane}_timeout`)) || 0;
        key = String(settings.secret(`${lane}ApiKey`) || '');
        // The chat lane is where a pre-C42 setup lives: main.js's `purposeSecretKey`
        // treats the legacy provider key as chat's own when chat has no key of its
        // own, and the bridge has to read the same slot or the two surfaces disagree
        // about whether the planner is about to have a credential.
        if (lane === 'chat' && !key) key = String(settings.secret('providerApiKey') || '');
      } catch {
        // Unreadable settings are not a configuration: report the fail-closed answer
        // rather than a half-filled lane.
        return { lane, provider: '', model: '', endpoint: '', timeout: 0, apiKey: '', configured: false, legacy: false };
      }
      if (providerId) {
        // A lane that names a provider but has no credential cannot answer, and
        // stopping here would strand the planner on it forever while a perfectly
        // usable Reasoning lane sat configured next to it. Missing keys fall through
        // to the next lane; the *last* attempt is what the refusal reports, so the
        // sentence the user sees is about the lane that was actually tried.
        const spec = specFor(providerId);
        const keyless = Boolean(spec) && spec.requiresKey === false;
        const usable = Boolean(spec) && spec.implemented && (keyless || Boolean(key));
        if (!usable && lanes.indexOf(lane) < lanes.length - 1) continue;
        return {
          lane,
          provider: providerId,
          model,
          endpoint,
          timeout,
          apiKey: key,
          configured: true,
          legacy: false,
        };
      }
    }
    // Nothing in this build's lanes has ever been set. The legacy single-provider
    // keys are the chat lane in everything but name: keeping them working is the
    // whole point of not migrating them behind the user's back.
    let providerId = '';
    let model = '';
    let endpoint = '';
    let timeout = 0;
    let key = '';
    try {
      providerId = String(settings.get('providerName') || '');
      model = String(settings.get('providerModel') || '');
      endpoint = String(settings.get('providerEndpoint') || '');
      timeout = Number(settings.get('providerTimeout')) || 0;
      key = String(settings.secret('providerApiKey') || '');
    } catch {
      return { lane: 'legacy', provider: '', model: '', endpoint: '', timeout: 0, apiKey: '', configured: false, legacy: true };
    }
    return {
      lane: 'legacy',
      provider: providerId,
      model,
      endpoint,
      timeout,
      apiKey: key,
      configured: Boolean(providerId),
      legacy: true,
    };
  }

  function specFor(providerId) {
    try {
      return typeof providers.specFor === 'function' ? providers.specFor(providerId) : null;
    } catch {
      return null;
    }
  }

  function generateIsAvailable() {
    return typeof providers.generate === 'function';
  }

  /** The conservative inference used only when main.js does not inject `getStatus`. */
  function derivedState(spec, hasKey) {
    if (!spec) return 'not_configured';
    if (!spec.implemented) return 'provider_unavailable';
    if (spec.requiresKey !== false && !hasKey) return 'missing_key';
    // Configured and keyed, but nothing has been verified. Do not claim more.
    return 'not_configured';
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

  async function statusFrame() {
    // The lane the next plan would use, not a fixed one: this frame is what
    // /voiceos/status reports as "will a model answer", so it has to be resolved by
    // the same rule the generate path uses below.
    const lane = laneFor(planningLanes());
    const providerId = lane.provider;
    const spec = specFor(providerId);
    const effectiveModel = lane.model || (spec && spec.defaultModel ? String(spec.defaultModel) : '');
    const hasKey = Boolean(lane.apiKey);
    // `implemented` describes the chosen provider, so an unchosen one is not implemented.
    const implemented = Boolean(spec) && Boolean(spec.implemented) && generateIsAvailable();
    const configured = Boolean(spec) && Boolean(effectiveModel);

    // Prefer main.js's verified-state cache: it is the one that knows about a Test
    // Connection the user pressed. Our own record covers calls made through the socket.
    let state = '';
    if (getStatus) {
      try {
        const info = getStatus(lane.lane);
        if (info && typeof info.state === 'string' && info.state) state = info.state;
      } catch { /* fall through to what this module knows */ }
    }
    if (!state && verified) state = verified.state;
    if (!state) state = derivedState(spec, hasKey);

    return {
      ok: true,
      configured,
      provider: providerId,
      model: effectiveModel,
      implemented,
      hasKey,
      // Which lane answered. Safe metadata the shell uses to say "the planner is on
      // Reasoning" rather than making the user guess which card the planner reads.
      purpose: lane.lane,
      state,
      // The sentence the peer shows when it falls back because of this state. Kept in
      // the shell: it is the same table the header bar uses, so the two surfaces cannot
      // tell the user different stories about the same fact.
      detail: STATE_DETAIL[state] || '',
    };
  }

  async function generateFrame(request) {
    const purpose = request.purpose;
    if (purpose !== 'plan' && purpose !== 'diagnose') {
      counters.malformed += 1;
      return { ok: false, state: 'malformed', detail: BAD_PURPOSE_DETAIL };
    }
    if (typeof request.system !== 'string' || typeof request.user !== 'string') {
      counters.malformed += 1;
      return { ok: false, state: 'malformed', detail: BAD_PROMPT_DETAIL };
    }

    // Live reads, every request. A cached config would keep serving the previous
    // provider after the user changed it — and would hold a copy of the key longer
    // than necessary. The lane is resolved by the same rule the status frame uses, so
    // what the UI reports and what the planner does cannot drift apart.
    const lane = laneFor(planningLanes());
    if (!lane.configured) {
      recordVerified('not_configured');
      return { ok: false, state: 'not_configured', detail: STATE_DETAIL.not_configured };
    }
    const providerId = lane.provider;
    const model = lane.model;
    const baseUrl = lane.endpoint;
    const apiKey = lane.apiKey;

    // No key, no call — and no exception to the key-free reply. Asked only for a
    // provider that requires one, so a keyless local endpoint is not mislabelled.
    const spec = specFor(providerId);
    const requiresKey = !spec || spec.requiresKey !== false;
    if (requiresKey && !apiKey) {
      recordVerified('missing_key');
      return { ok: false, state: 'missing_key', detail: STATE_DETAIL.missing_key };
    }
    if (!generateIsAvailable()) {
      recordVerified('provider_unavailable');
      return { ok: false, state: 'provider_unavailable', detail: NO_GENERATE_DETAIL };
    }

    const config = {
      provider: providerId,
      model,
      baseUrl,
      apiKey,
      timeoutSeconds: typeof request.timeout_s === 'number' && Number.isFinite(request.timeout_s)
        ? request.timeout_s
        : lane.timeout > 0
          ? lane.timeout
          : undefined,
    };

    let outcome;
    try {
      outcome = await providers.generate(config, {
        purpose,
        system: request.system,
        user: request.user,
      });
    } catch {
      // A thrown error can carry anything the transport put in it. Say only what this
      // file says.
      recordVerified('provider_unavailable');
      return { ok: false, state: 'provider_unavailable', detail: STATE_DETAIL.provider_unavailable };
    }

    const state = outcome && typeof outcome.state === 'string' ? outcome.state : '';
    if (state === 'ok') {
      const content = outcome.content;
      if (!content || typeof content !== 'object' || Array.isArray(content)) {
        recordVerified('malformed');
        return { ok: false, state: 'malformed', detail: STATE_DETAIL.malformed };
      }
      recordVerified('connected');
      return { ok: true, content };
    }
    if (KNOWN_STATES.has(state)) {
      recordVerified(state);
      return {
        ok: false,
        state,
        detail: STATE_DETAIL[state] || STATE_DETAIL.provider_unavailable,
      };
    }
    // Anything outside the contract, including a missing state, is not trusted.
    recordVerified('provider_unavailable');
    return { ok: false, state: 'provider_unavailable', detail: STATE_DETAIL.provider_unavailable };
  }

  function onConnection(socket) {
    if (closed) {
      socket.destroy();
      return;
    }
    counters.connections += 1;
    sockets.add(socket);

    let settled = false;
    let buffer = Buffer.alloc(0);
    let idleTimer = setTimeout(onIdle, IDLE_TIMEOUT_MS);

    function clearIdle() {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = null;
    }

    function release() {
      if (settled) return;
      settled = true;
      clearIdle();
      sockets.delete(socket);
    }

    /** Close without a reply: used for auth failure and oversized frames. */
    function drop() {
      release();
      try { socket.destroy(); } catch { /* already gone */ }
    }

    /** Answer with exactly one frame, then close. */
    function finish(frame) {
      if (settled) return;
      release();
      let line;
      try {
        line = `${JSON.stringify(frame)}\n`;
      } catch {
        line = `${JSON.stringify({ ok: false, state: 'provider_unavailable', detail: STATE_DETAIL.provider_unavailable })}\n`;
      }
      try {
        socket.end(Buffer.from(line, 'utf8'));
      } catch {
        try { socket.destroy(); } catch { /* already gone */ }
      }
    }

    function onIdle() {
      counters.timeouts += 1;
      drop();
    }

    function resetIdle() {
      clearIdle();
      idleTimer = setTimeout(onIdle, IDLE_TIMEOUT_MS);
    }

    async function handleFrame(raw) {
      counters.frames += 1;
      let request;
      try {
        request = JSON.parse(raw.toString('utf8'));
      } catch {
        counters.malformed += 1;
        finish({ ok: false, state: 'malformed', detail: MALFORMED_FRAME_DETAIL });
        return;
      }
      if (!request || typeof request !== 'object' || Array.isArray(request)) {
        counters.malformed += 1;
        finish({ ok: false, state: 'malformed', detail: MALFORMED_FRAME_DETAIL });
        return;
      }

      // Authentication before anything else. A mismatch gets no frame at all: a reply,
      // even a refusal, would tell a caller its frame was structurally accepted.
      if (!authorised(request.token)) {
        counters.authFailures += 1;
        drop();
        return;
      }

      if (request.op === 'status') {
        const frame = await statusFrame();
        if (settled) return;
        counters.served += 1;
        finish(frame);
        return;
      }
      if (request.op === 'generate') {
        const frame = await generateFrame(request);
        if (settled) return;
        counters.served += 1;
        finish(frame);
        return;
      }

      counters.unknownOp += 1;
      finish({ ok: false, state: 'malformed', detail: UNKNOWN_OP_DETAIL });
    }

    socket.on('data', (chunk) => {
      if (settled) return;
      resetIdle();

      // Check before concatenating so a hostile peer cannot make us allocate without
      // bound just to find that out afterwards.
      if (buffer.length > MAX_FRAME_BYTES) {
        counters.oversized += 1;
        drop();
        return;
      }
      buffer = Buffer.concat([buffer, chunk]);

      const newline = buffer.indexOf(0x0a);
      if (newline === -1) {
        if (buffer.length > MAX_FRAME_BYTES) {
          counters.oversized += 1;
          drop();
        }
        return;
      }
      const line = buffer.subarray(0, newline);
      if (line.length > MAX_FRAME_BYTES) {
        counters.oversized += 1;
        drop();
        return;
      }
      // One request per connection: everything after the first newline is discarded.
      handleFrame(line).catch(() => {
        counters.malformed += 1;
        finish({ ok: false, state: 'malformed', detail: MALFORMED_FRAME_DETAIL });
      });
    });

    socket.on('error', () => drop());
    socket.on('close', () => {
      release();
    });
  }

  await clearStaleSocket(socketPath);

  const server = net.createServer(onConnection);
  await new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once('error', onError);
    server.listen(socketPath, () => {
      server.removeListener('error', onError);
      resolve();
    });
  });

  try {
    // 0600 after listen: the bind creates the file with the umask, which is not a
    // promise. Only this user may open it.
    fs.chmodSync(socketPath, 0o600);
  } catch (error) {
    try { server.close(); } catch { /* never listening */ }
    try { fs.unlinkSync(socketPath); } catch { /* nothing to remove */ }
    throw new Error('The provider bridge socket could not be made private.');
  }

  // A post-listen server error must not take the main process down with it.
  server.on('error', () => note('provider bridge socket error'));

  note(`provider bridge listening on ${socketPath}`);

  return {
    socketPath,
    /** Stop accepting, destroy live sockets, remove the socket file. */
    async close() {
      if (closed) return;
      closed = true;
      for (const socket of sockets) {
        try { socket.destroy(); } catch { /* already gone */ }
      }
      sockets.clear();
      await new Promise((resolve) => {
        try {
          server.close(() => resolve());
        } catch {
          resolve();
        }
      });
      try {
        fs.unlinkSync(socketPath);
      } catch (error) {
        if (!error || error.code !== 'ENOENT') note('provider bridge could not remove its socket file');
      }
      note('provider bridge closed');
    },
    /**
     * Push a verified state in, for main.js when it tests a connection itself.
     * `state` uses the UI vocabulary (`connected`, `missing_key`, ...).
     */
    noteTestResult(state) {
      if (typeof state === 'string' && state) {
        const mapped = state === 'ok' ? 'connected' : state;
        verified = { state: mapped };
      }
    },
    /** Counters only — never frame content. */
    stats() {
      return { ...counters };
    },
  };
}

module.exports = {
  startBridge,
  MAX_FRAME_BYTES,
  IDLE_TIMEOUT_MS,
  KNOWN_STATES,
};