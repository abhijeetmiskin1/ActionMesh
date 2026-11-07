'use strict';
/**
 * AI provider adapters — main process only.
 *
 * The API key never leaves this process. It is read from the OS keyring by
 * `SettingsStore.secret()` and handed to `test()` as an argument; nothing here writes
 * it anywhere, returns it, or puts it in an error message. The renderer receives the
 * provider id, the model, and a *state* — never a credential and never a raw response.
 *
 * Two functions make network calls. `test()` is the "Test Connection" probe, called by
 * the button in the provider panel. `generate()` is the real completion call the
 * VoiceOS planner makes through the model bridge. A provider is never marked connected
 * because a key exists: the state is `missing_key` until a call is made, and
 * `connected` only on a 2xx from the provider's own endpoint.
 *
 * `generate()` returns the model's *proposal* as untrusted data. Nothing here decides
 * whether a proposal is allowed — the caller in Python validates every action name and
 * argument against the real registry. This file's only authority is over the transport.
 *
 * Two rules keep this from becoming a general-purpose HTTP client:
 *   * the destination is chosen from a closed table, never from the renderer — the one
 *     exception is a compatible endpoint: HTTPS on 443 or loopback HTTP,
 *     no userinfo or fragment; localhost is pinned to 127.0.0.1;
 *   * responses are read to a fixed 4 KiB ceiling and only the status decides the
 *     state. A provider's error body can echo the request, so its text is never
 *     surfaced; every `detail` below is a sentence written in this file.
 *
 * This module reads no settings and touches no disk. Everything it needs arrives as an
 * argument, which is also what makes it testable without a network.
 */
const https = require('https');
const http = require('http');
const crypto = require('crypto');

const MAX_BODY = 4096;
/** Generation answers are JSON documents, not a status line, so they get a bigger cap. */
const MAX_GENERATION_BODY = 256 * 1024;
const DEFAULT_TIMEOUT_S = 12;
/** A model needs longer to think than a connection probe does. */
const GENERATION_TIMEOUT_S = 20;
const MIN_TIMEOUT_S = 1;
const MAX_TIMEOUT_S = 60;
const DEFAULT_MAX_TOKENS = 1024;
const MIN_MAX_TOKENS = 16;
const MAX_MAX_TOKENS = 8192;
const MAX_PROMPT_CHARS = 32 * 1024;
const ALLOWED_PORT = 443;

const SPECS = [
  {
    id: 'anthropic',
    label: 'Anthropic',
    implemented: true,
    requiresKey: true,
    requiresBaseUrl: false,
    defaultModel: 'claude-sonnet-5-5',
    models: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'],
    unavailableReason: '',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    implemented: true,
    requiresKey: true,
    requiresBaseUrl: false,
    defaultModel: 'gpt-4o',
    models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1'],
    unavailableReason: '',
  },
  {
    id: 'gemini',
    label: 'Google Gemini',
    implemented: true,
    requiresKey: true,
    requiresBaseUrl: false,
    defaultModel: 'gemini-2.0-flash',
    models: ['gemini-2.0-flash', 'gemini-1.5-pro', 'gemini-1.5-flash'],
    unavailableReason: '',
  },
  {
    id: 'openai_compatible',
    label: 'OpenAI-compatible endpoint',
    implemented: true,
    requiresKey: true,
    requiresBaseUrl: true,
    defaultModel: '',
    models: [],
    unavailableReason: '',
  },
  {
    id: 'ollama',
    label: 'Ollama (local)',
    implemented: false,
    requiresKey: false,
    requiresBaseUrl: false,
    defaultModel: '',
    models: [],
    // Said plainly rather than half-wired: a probe that only checks a port would report
    // "connected" for a server that cannot answer a completion.
    unavailableReason:
      'No local runtime probe is wired in this build; use an OpenAI-compatible endpoint pointed at your Ollama server instead.',
  },
];

/**
 * Media purposes are deliberately not the text adapters. They are closed tables of
 * vendors this build knows about; router images and speech transcription have adapters. A metered
 * image or audio call is not made up because the chat adapter works. The entries exist
 * so the bars can say which vendors are planned; choosing one is refused until a real
 * adapter and a real probe exist.
 */
const MEDIA_SPECS = {
  image: [
    { id: 'router_image', label: '9router / compatible images', implemented: true,
      requiresKey: true, requiresBaseUrl: true, defaultModel: '', models: [], unavailableReason: '' },
    {
      id: 'openai_images',
      label: 'OpenAI Images',
      implemented: false,
      requiresKey: true,
      requiresBaseUrl: false,
      defaultModel: '',
      models: [],
      unavailableReason: 'Not wired in this build. An image adapter and a real probe must exist before a key is accepted.',
    },
    {
      id: 'stability',
      label: 'Stability AI',
      implemented: false,
      requiresKey: true,
      requiresBaseUrl: false,
      defaultModel: '',
      models: [],
      unavailableReason: 'Not wired in this build. An image adapter and a real probe must exist before a key is accepted.',
    },
    {
      id: 'replicate',
      label: 'Replicate',
      implemented: false,
      requiresKey: true,
      requiresBaseUrl: false,
      defaultModel: '',
      models: [],
      unavailableReason: 'Not wired in this build. An image adapter and a real probe must exist before a key is accepted.',
    },
  ],
  audio: [
    {
      id: 'router_audio', label: '9router / compatible speech-to-text',
      implemented: true, requiresKey: true, requiresBaseUrl: true,
      defaultModel: '', models: [], unavailableReason: '',
    },
    {
      id: 'elevenlabs',
      label: 'ElevenLabs',
      implemented: false,
      requiresKey: true,
      requiresBaseUrl: false,
      defaultModel: '',
      models: [],
      unavailableReason: 'Not wired in this build. An audio adapter and a real probe must exist before a key is accepted.',
    },
    {
      id: 'deepgram',
      label: 'Deepgram',
      implemented: false,
      requiresKey: true,
      requiresBaseUrl: false,
      defaultModel: '',
      models: [],
      unavailableReason: 'Not wired in this build. An audio adapter and a real probe must exist before a key is accepted.',
    },
    {
      id: 'openai_tts',
      label: 'OpenAI TTS',
      implemented: false,
      requiresKey: true,
      requiresBaseUrl: false,
      defaultModel: '',
      models: [],
      unavailableReason: 'Not wired in this build. An audio adapter and a real probe must exist before a key is accepted.',
    },
  ],
};

const TEXT_PURPOSES = ['chat', 'reasoning'];

/** The closed table for a purpose. Unknown purposes get no specs at all. */
function specsFor(purpose) {
  const p = String(purpose || '').trim().toLowerCase();
  if (Object.prototype.hasOwnProperty.call(MEDIA_SPECS, p)) return MEDIA_SPECS[p];
  if (!p || TEXT_PURPOSES.includes(p)) return SPECS;
  return [];
}

/**
 * The specs with no secret in them. A shallow copy per call so a renderer-side mutation
 * cannot reach the module's own table. Omitted purpose means the text table (the
 * pre-C42 behaviour), because the planner only knows the text adapters.
 */
function publicSpecs(purpose) {
  return specsFor(purpose).map((spec) => ({ ...spec, models: [...spec.models] }));
}

function specFor(id, purpose) {
  const wanted = String(id || '').trim();
  return specsFor(purpose).find((spec) => spec.id === wanted) || null;
}

/** A short sentence for a state; the only thing the renderer ever shows. */
const STATE_TEXT = {
  not_configured: 'Not configured',
  missing_key: 'Missing API key',
  connecting: 'Connecting…',
  connected: 'Connected',
  invalid_credentials: 'Invalid credentials',
  provider_unavailable: 'Provider unavailable',
  timeout: 'Timeout',
  rate_limited: 'Rate limited',
};

function result(state, detail) {
  return { state, text: STATE_TEXT[state] || state, detail: detail || '' };
}

function clampTimeoutSeconds(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return DEFAULT_TIMEOUT_S;
  return Math.min(Math.max(seconds, MIN_TIMEOUT_S), MAX_TIMEOUT_S);
}

/**
 * Whether the key can be put in a header at all.
 *
 * A value containing CR, LF or another control character is not a key — it is a header
 * injection attempt, or a paste that went wrong. It is refused here so it can never
 * reach Node's header writer, and the sentence says nothing about the value.
 */
function keyIsHeaderSafe(apiKey) {
  return !/[\u0000-\u001f\u007f]/.test(apiKey);
}

/**
 * One request: HTTPS on 443 or numeric loopback HTTP, bounded, no redirects.
 *
 * `requestImpl` exists for the tests: injecting an https.request-shaped function is the
 * only way to prove the classification table without touching the network. `agent:
 * false` gives every probe its own connection, so a probe never reuses a pooled socket
 * and the transport has exactly the lifecycle this file describes.
 */
function post(url, headers, body, timeoutSeconds, requestImpl, maxBody) {
  return requestJson('POST', url, headers, body, timeoutSeconds, requestImpl, maxBody);
}

function requestJson(method, url, headers, body, timeoutSeconds, requestImpl, maxBody) {
  const timeoutMs = Math.round(clampTimeoutSeconds(timeoutSeconds) * 1000);
  const ceiling = Number.isFinite(maxBody) && maxBody > 0 ? maxBody : MAX_BODY;
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const done = (value) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve(value);
    };

    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      done({ kind: 'invalid_url', status: 0 });
      return;
    }
    if (parsed.protocol !== 'https:' && !localHttp(parsed)) {
      done({ kind: 'invalid_url', status: 0 });
      return;
    }
    if (parsed.username || parsed.password) {
      done({ kind: 'invalid_url', status: 0 });
      return;
    }
    if (!localHttp(parsed) && parsed.port && Number(parsed.port) !== ALLOWED_PORT) {
      done({ kind: 'invalid_url', status: 0 });
      return;
    }
    if (parsed.hash) {
      done({ kind: 'invalid_url', status: 0 });
      return;
    }

    const payload = method === 'GET' ? null
      : Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body), 'utf8');
    let req;
    try {
      const transport = requestImpl || (localHttp(parsed) ? http.request : https.request);
      req = transport({
        method,
        protocol: parsed.protocol,
        // Pin localhost to numeric loopback: never resolve it through external DNS.
        hostname: parsed.hostname === 'localhost' ? '127.0.0.1' : parsed.hostname.replace(/^\[|\]$/g, ''),
        port: parsed.port ? Number(parsed.port) : (localHttp(parsed) ? 80 : ALLOWED_PORT),
        path: `${parsed.pathname}${parsed.search}`,
        headers: {
          ...(payload ? { 'content-type': 'application/json' } : {}),
          ...headers,
          ...(payload ? { 'content-length': payload.length } : {}),
        },
        timeout: timeoutMs,
        agent: false,
      }, (res) => {
        let text = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          // Read at most `ceiling` and discard the rest. A probe's body is never
          // surfaced; the cap exists so a hostile or enormous response cannot be
          // buffered. Generation keeps its body, but under the same rule.
          if (text.length < ceiling) text += chunk.slice(0, ceiling - text.length);
        });
        res.on('end', () => done({ kind: 'response', status: res.statusCode || 0, body: text,
          headers: res.headers || {} }));
        res.on('error', () => done({ kind: 'network', status: 0 }));
      });
    } catch {
      done({ kind: 'network', status: 0 });
      return;
    }

    // A hard deadline as well as the socket idle timeout: a peer that dribbles a byte
    // per second would otherwise keep resetting `timeout` indefinitely.
    timer = setTimeout(() => {
      try { req.destroy(); } catch { /* already gone */ }
      done({ kind: 'timeout', status: 0 });
    }, timeoutMs + 250);

    req.on('timeout', () => {
      try { req.destroy(); } catch { /* already gone */ }
      done({ kind: 'timeout', status: 0 });
    });
    req.on('error', (error) => {
      const code = error && error.code;
      if (code === 'ETIMEDOUT' || code === 'ABORT_ERR') {
        done({ kind: 'timeout', status: 0 });
      } else {
        done({ kind: 'network', status: 0 });
      }
    });
    try {
      if (payload) req.write(payload);
      req.end();
    } catch {
      done({ kind: 'network', status: 0 });
    }
  });
}

/** Status → state. The body is never consulted, so it can never be echoed. */
function classify(probe) {
  if (probe.kind === 'timeout') {
    return result('timeout', 'The provider did not answer within the timeout.');
  }
  if (probe.kind === 'network') {
    return result('provider_unavailable', 'Could not reach the provider. Check the network or the endpoint.');
  }
  // Includes a base URL with the wrong scheme, port or userinfo.
  if (probe.kind === 'invalid_url') {
    return result('provider_unavailable', 'The endpoint must be an https URL on port 443.');
  }

  const status = probe.status;
  if (status >= 200 && status < 300) return result('connected', 'The provider accepted the request.');
  if (status === 401 || status === 403) return result('invalid_credentials', 'The provider rejected the API key.');
  if (status === 429) return result('rate_limited', 'The provider is rate limiting this key. Try again shortly.');
  if (status >= 300 && status < 400) {
    // No redirects are followed, by design: a redirect is how a checked destination
    // becomes an unchecked one.
    return result('provider_unavailable', 'The provider answered with a redirect, which this app does not follow.');
  }
  if (status >= 500) return result('provider_unavailable', `The provider reported a server error (${status}).`);
  if (status === 404) return result('provider_unavailable', 'The model or endpoint was not found.');
  if (status === 400 || status === 422) {
    // The model id is the usual cause of a 400 here, and saying so is more useful than
    // replaying a provider body that may quote the request.
    return result('provider_unavailable', `The provider rejected the request (${status}). Check the model id.`);
  }
  return result('provider_unavailable', `The provider answered with status ${status}.`);
}

/**
 * Validate an OpenAI-compatible base URL and build the completions endpoint.
 * Returns `{ url }` or `{ error }`, and never a URL it has not checked.
 */
function localHttp(parsed) {
  return parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname);
}

function compatibleEndpoint(baseUrl) {
  let parsed;
  try {
    parsed = new URL(baseUrl);
  } catch {
    return { error: 'The base URL is not a valid URL.' };
  }
  if (parsed.protocol !== 'https:' && !localHttp(parsed)) return { error: 'Use https, or http on localhost/loopback only.' };
  if (parsed.username || parsed.password) {
    return { error: 'The base URL must not contain credentials.' };
  }
  if (!localHttp(parsed) && parsed.port && Number(parsed.port) !== ALLOWED_PORT) {
    return { error: 'The base URL must use port 443.' };
  }
  if (parsed.hash) return { error: 'The base URL must not contain a fragment.' };
  // Query strings are dropped rather than carried: this builder decides the path.
  const path = parsed.pathname.replace(/\/+$/, '');
  return { url: `${parsed.origin}${path}/chat/completions` };
}

/**
 * Ask the provider to accept one minimal request.
 *
 * @param {{provider:string, model:string, baseUrl:string, apiKey:string, timeoutSeconds:number}} config
 * @param {{request?:Function}} [options]
 * @returns {Promise<{state:string, text:string, detail:string}>}
 */
async function test(config, options = {}) {
  const source = config && typeof config === 'object' ? config : {};
  const provider = String(source.provider || '').trim();
  const model = String(source.model || '').trim();
  const baseUrl = String(source.baseUrl || '').trim();
  const apiKey = String(source.apiKey || '').trim();
  const requestImpl = (options && options.request) || null;

  const spec = specFor(provider, source.purpose);
  if (!spec) return result('not_configured', 'Choose a provider first.');
  if (!spec.implemented) {
    return result('provider_unavailable', spec.unavailableReason || 'This provider is not implemented.');
  }

  // No key, no call. This is both the honest state and the one that stops a
  // half-configured provider from ever reaching the network. Checked before the model
  // so a missing credential is always what the header reports, whatever else is blank.
  if (spec.requiresKey && !apiKey) {
    return result('missing_key', 'No API key is stored for this provider.');
  }
  if (apiKey && !keyIsHeaderSafe(apiKey)) {
    return result('not_configured', 'The stored API key contains characters that cannot be sent in a header.');
  }

  const chosen = model || spec.defaultModel;
  if (!chosen) return result('not_configured', 'Choose a model first.');

  let probe;
  if (provider === 'anthropic') {
    probe = await post(
      'https://api.anthropic.com/v1/messages',
      { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
      { model: chosen, max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] },
      source.timeoutSeconds,
      requestImpl,
    );
  } else if (provider === 'openai') {
    probe = await post(
      'https://api.openai.com/v1/chat/completions',
      { authorization: `Bearer ${apiKey}` },
      { model: chosen, max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] },
      source.timeoutSeconds,
      requestImpl,
    );
  } else if (provider === 'gemini') {
    const url = 'https://generativelanguage.googleapis.com/v1beta/models/'
      + `${encodeURIComponent(chosen)}:generateContent?key=${encodeURIComponent(apiKey)}`;
    probe = await post(
      url,
      {},
      { contents: [{ parts: [{ text: 'ping' }] }], generationConfig: { maxOutputTokens: 1 } },
      source.timeoutSeconds,
      requestImpl,
    );
  } else if (provider === 'router_image') {
    const image = await generateImage(source, 'A plain blue square on a white background.', options);
    return image.ok ? result('connected', 'The image endpoint generated a bounded raster image.')
      : result('provider_unavailable', image.detail);
  } else if (provider === 'router_audio') {
    const answer = await speechProbe(source, options);
    return answer;
  } else if (provider === 'openai_compatible') {
    if (!baseUrl) return result('not_configured', 'Enter the endpoint base URL.');
    const endpoint = compatibleEndpoint(baseUrl);
    if (endpoint.error) return result('provider_unavailable', endpoint.error);
    probe = await post(
      endpoint.url,
      { authorization: `Bearer ${apiKey}` },
      { model: chosen, max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] },
      source.timeoutSeconds,
      requestImpl,
    );
  } else {
    // Unreachable while SPECS.implemented and this table agree. A provider marked
    // implemented without a request shape must fail loudly, not silently.
    return result('provider_unavailable', 'This provider is not implemented.');
  }

  return classify(probe);
}

async function generateImage(config, prompt, options = {}) {
  if (!config || config.provider !== 'router_image') return { ok: false, detail: 'Configure the Image lane with a compatible image provider first.' };
  const apiKey = String(config.apiKey || '').trim();
  const model = String(config.model || '').trim();
  if (!apiKey || !keyIsHeaderSafe(apiKey)) return { ok: false, detail: 'Save a valid Image API key in AI Providers.' };
  if (!model || model.length > 120) return { ok: false, detail: 'Choose an Image generation model.' };
  if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 4000) return { ok: false, detail: 'Enter an image description up to 4000 characters.' };
  const endpoint = compatibleEndpoint(config.baseUrl);
  if (endpoint.error) return { ok: false, detail: endpoint.error };
  const probe = await post(endpoint.url.replace(/\/chat\/completions$/, '/images/generations'),
    { authorization: `Bearer ${apiKey}` },
    { model, prompt: prompt.trim(), n: 1, size: '512x512', width: 512, height: 512, response_format: 'b64_json' },
    config.timeoutSeconds || 60, options.request || null, 5 * 1024 * 1024);
  const status = classify(probe);
  if (status.state !== 'connected') return { ok: false, detail: status.detail };
  let envelope;
  try { envelope = JSON.parse(probe.body); } catch { return { ok: false, detail: 'The image provider returned invalid JSON.' }; }
  const data = envelope && Array.isArray(envelope.data) && envelope.data[0];
  const base64 = data && data.b64_json;
  if (typeof base64 !== 'string' || !base64.length || base64.length > 4 * 1024 * 1024
      || !/^[A-Za-z0-9+/]+={0,2}$/.test(base64)) return { ok: false, detail: 'The provider must return a bounded inline image, rather than a remote URL.' };
  const bytes = Buffer.from(base64, 'base64');
  let mime;
  if (bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) mime = 'image/png';
  else if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) mime = 'image/jpeg';
  else if (bytes.subarray(0, 4).toString() === 'RIFF' && bytes.subarray(8, 12).toString() === 'WEBP') mime = 'image/webp';
  else return { ok: false, detail: 'The provider did not return a supported raster image.' };
  return { ok: true, dataUrl: `data:${mime};base64,${bytes.toString('base64')}`, model };
}

const MAX_AUDIO_BYTES = 2 * 1024 * 1024;

function githubPublicRows(data) {
  if (!Array.isArray(data?.items)) return null;
  return data.items.filter((row) => row?.private === false
    && typeof row.full_name === 'string'
    && /^[\w-]{1,39}\/[\w.-]{1,100}$/.test(row.full_name)
    && !row.full_name.endsWith('/.') && !row.full_name.endsWith('/..')).slice(0, 5)
    .map((row) => ({
      title: row.full_name, url: `https://github.com/${row.full_name}`,
      snippet: typeof row.description === 'string' ? row.description.slice(0, 220) : '',
    }));
}

function mcpResult(response, id) {
  if (response.kind !== 'response' || response.status !== 200) return null;
  const frames = String(response.headers['content-type'] || '').includes('text/event-stream')
    ? response.body.split(/\r?\n\r?\n/).map((frame) => frame.split(/\r?\n/)
      .filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n'))
    : [response.body];
  for (const frame of frames) {
    try {
      const message = JSON.parse(frame);
      if (message?.jsonrpc === '2.0' && message.id === id) return message.result || null;
    } catch { /* ignore non-JSON events */ }
  }
  return null;
}

async function githubRepositories(query, token = '', options = {}) {
  if (typeof query !== 'string' || !query.trim() || query.length > 180 || /[\x00-\x1f\x7f]/.test(query)) {
    return { ok: false, detail: 'Use a short GitHub repository search query.' };
  }
  if (typeof token !== 'string' || (token && !/^[\x21-\x7e]{16,512}$/.test(token))) {
    return { ok: false, detail: 'Use a valid GitHub token.' };
  }
  const request = options.request || null;
  const queryText = `${query.trim()} is:public`;
  let data;
  if (!token) {
    const url = `https://api.github.com/search/repositories?${new URLSearchParams({ q: queryText, per_page: '5' })}`;
    const reply = await requestJson('GET', url, { 'user-agent': 'ActionMesh', accept: 'application/vnd.github+json' },
      null, 8, request, MAX_GENERATION_BODY);
    if (reply.kind !== 'response' || reply.status !== 200) {
      return { ok: false, detail: 'GitHub public search is unavailable or rate limited.' };
    }
    try { data = JSON.parse(reply.body); } catch { /* checked below */ }
  } else {
    const endpoint = 'https://api.githubcopilot.com/mcp/';
    const headers = { authorization: `Bearer ${token}`, accept: 'application/json, text/event-stream',
      'user-agent': 'ActionMesh', 'X-MCP-Readonly': 'true', 'X-MCP-Tools': 'search_repositories' };
    const rpc = (method, params, id) => post(endpoint, headers,
      { jsonrpc: '2.0', method, ...(params ? { params } : {}), ...(id ? { id } : {}) },
      8, request, MAX_GENERATION_BODY);
    const init = await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {},
      clientInfo: { name: 'ActionMesh', version: '0.1.0' } }, 1);
    const connection = mcpResult(init, 1);
    if (!['2025-03-26', '2025-06-18', '2025-11-25'].includes(connection?.protocolVersion)) {
      return { ok: false, detail: 'Could not initialize GitHub MCP. Check the token and account access.' };
    }
    headers['MCP-Protocol-Version'] = connection.protocolVersion;
    const session = init.headers['mcp-session-id'];
    if (session !== undefined) {
      if (typeof session !== 'string' || !/^[\x21-\x7e]{1,256}$/.test(session)) {
        return { ok: false, detail: 'GitHub MCP returned an invalid session.' };
      }
      headers['Mcp-Session-Id'] = session;
    }
    const notification = await rpc('notifications/initialized');
    if (notification.status < 200 || notification.status >= 300) {
      return { ok: false, detail: 'GitHub MCP could not complete initialization.' };
    }
    const result = mcpResult(await rpc('tools/call', {
      name: 'search_repositories', arguments: { query: queryText, perPage: 5 },
    }, 2), 2);
    if (result?.isError) return { ok: false, detail: 'GitHub MCP refused the repository search.' };
    data = result?.structuredContent;
    if (!data && Array.isArray(result?.content)) {
      const text = result.content.find((item) => item?.type === 'text')?.text;
      try { data = JSON.parse(text); } catch { /* checked below */ }
    }
  }
  const results = githubPublicRows(data);
  if (!results) return { ok: false, detail: 'GitHub returned an unreadable repository list.' };
  return { ok: true, source: token ? 'GitHub MCP' : 'GitHub API',
    results: token ? results.filter((row) => !Object.values(row).some((text) => text.includes(token))) : results };
}

async function todoistTasks(token, options = {}) {
  if (typeof token !== 'string' || !/^[\x21-\x7e]{16,512}$/.test(token)) {
    return { ok: false, detail: 'Save a valid Todoist API token first.' };
  }
  const reply = await requestJson('GET', 'https://api.todoist.com/api/v1/tasks?limit=10',
    { authorization: `Bearer ${token}`, accept: 'application/json' }, null, 10,
    options.request || null, 128 * 1024);
  const status = classify(reply);
  if (status.state !== 'connected') return { ok: false, detail: status.detail };
  let data;
  try { data = JSON.parse(reply.body); } catch {
    return { ok: false, detail: 'Todoist returned an unreadable task list.' };
  }
  if (!Array.isArray(data?.results)) return { ok: false, detail: 'Todoist returned an invalid task list.' };
  const tasks = data.results.slice(0, 10)
    .filter((task) => typeof task?.content === 'string' && task.content.trim() && !task.content.includes(token))
    .map((task) => ({
      title: task.content.trim().slice(0, 180),
      due: typeof task.due?.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(task.due.date)
        ? task.due.date : '',
    }));
  return { ok: true, tasks, hasMore: Boolean(data.next_cursor) || data.results.length > 10 };
}

async function searchRouter(config, query, options = {}) {
  const endpoint = compatibleEndpoint(config.baseUrl);
  const apiKey = String(config.apiKey || '').trim();
  if (endpoint.error || !localHttp(new URL(endpoint.url)) || !apiKey || !keyIsHeaderSafe(apiKey)) {
    return { ok: false, detail: 'Web lookup needs the local 9router Chat endpoint and key.' };
  }
  if (typeof query !== 'string' || !query.trim() || query.length > 250 || /[\u0000-\u001f\u007f]/.test(query)) {
    return { ok: false, detail: 'Use a short public search query.' };
  }
  const reply = await post(endpoint.url.replace(/\/chat\/completions$/, '/search'),
    { authorization: `Bearer ${apiKey}` },
    { provider: 'antigravity', query: query.trim(), max_results: 5 }, 15,
    options.request || null, MAX_GENERATION_BODY);
  if (reply.kind !== 'response' || reply.status !== 200) {
    return { ok: false, detail: '9router could not complete the web lookup. Check its web search connection.' };
  }
  let data;
  try { data = JSON.parse(reply.body); } catch {
    return { ok: false, detail: '9router returned an unreadable web lookup response.' };
  }
  const safeText = (value, limit) => typeof value === 'string' && !value.includes(apiKey)
    ? value.trim().slice(0, limit) : '';
  const results = [];
  for (const row of (Array.isArray(data?.results) ? data.results.slice(0, 5) : [])) {
    const url = safeText(row?.url, 2048);
    let parsed;
    try { parsed = new URL(url); } catch { continue; }
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) continue;
    results.push({ url: parsed.href, title: safeText(row.title, 160) || parsed.hostname,
      snippet: safeText(row.snippet, 280) });
  }
  return { ok: true, results };
}

async function transcribe(config, audio, options = {}) {
  if (!config || config.provider !== 'router_audio') return { ok: false, detail: 'Configure the Audio lane with 9router speech-to-text first.' };
  const apiKey = String(config.apiKey || '').trim();
  if (!apiKey || !keyIsHeaderSafe(apiKey)) return { ok: false, detail: 'Save a valid Audio API key in AI Providers.' };
  const model = String(config.model || '').trim();
  if (!model || model.length > 120 || /[\r\n]/.test(model)) return { ok: false, detail: 'Choose an Audio transcription model.' };
  if (!Buffer.isBuffer(audio) || !audio.length || audio.length > MAX_AUDIO_BYTES) return { ok: false, detail: 'The recording is empty or exceeds the 2 MiB limit.' };
  const endpoint = compatibleEndpoint(config.baseUrl);
  if (endpoint.error) return { ok: false, detail: endpoint.error };
  const language = options.language ?? '';
  if (typeof language !== 'string' || (language && !/^[a-z]{2}$/.test(language))) {
    return { ok: false, detail: 'Choose a valid speech input language.' };
  }
  const boundary = `actionmesh-${crypto.randomBytes(16).toString('hex')}`;
  const mime = options.probe ? 'audio/wav' : 'audio/webm';
  const extension = options.probe ? 'wav' : 'webm';
  const body = Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n${model}\r\n`
      + `--${boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n`
      + (language ? `--${boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n${language}\r\n` : '')
      + `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="utterance.${extension}"\r\nContent-Type: ${mime}\r\n\r\n`),
    audio, Buffer.from(`\r\n--${boundary}--\r\n`),
  ]);
  const probe = await post(endpoint.url.replace(/\/chat\/completions$/, '/audio/transcriptions'),
    { authorization: `Bearer ${apiKey}`, 'content-type': `multipart/form-data; boundary=${boundary}` },
    body, config.timeoutSeconds || 45, options.request || null, MAX_GENERATION_BODY);
  const status = classify(probe);
  if (status.state !== 'connected') return { ok: false, detail: status.detail, state: status.state };
  let data;
  try { data = JSON.parse(probe.body); } catch { return { ok: false, detail: 'The speech provider returned invalid JSON.' }; }
  if (typeof data.text !== 'string' || data.text.length > 16000) return { ok: false, detail: 'The speech provider returned no valid transcript.' };
  if (data.text.includes(apiKey)) return { ok: false, detail: 'The speech provider returned an unsafe response.' };
  return { ok: true, text: data.text.trim() };
}

async function speechProbe(config, options = {}) {
  // 100 ms of synthetic silence; never record a user's microphone for a test.
  const wav = Buffer.alloc(44 + 3200);
  wav.write('RIFF', 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(3200, 40);
  const answer = await transcribe(config, wav, { ...options, probe: true });
  return answer.ok ? result('connected', 'The transcription endpoint accepted a synthetic silence probe.')
    : result(answer.state || 'provider_unavailable', answer.detail);
}

/**
 * The two answer shapes the planner asks for. Declared once here and rendered into each
 * provider's own schema dialect below, so all four adapters ask for the same document.
 *
 * `additionalProperties: false` matters: it stops a model from smuggling extra keys into
 * the payload, which keeps the downstream validator's job exactly one shape.
 */
const SCHEMAS = {
  plan: {
    type: 'object',
    properties: {
      actions: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            type: { type: 'string' },
            parameters: { type: 'object' },
          },
          required: ['type'],
          additionalProperties: false,
        },
      },
      reply: { type: 'string', description: 'Answer or clarification when actions is empty; at most 4000 characters.' },
      notes: { type: 'array', items: { type: 'string' } },
    },
    required: ['actions'],
    additionalProperties: false,
  },
  diagnose: {
    type: 'object',
    properties: {
      finding: { type: 'string' },
      likely_cause: { type: 'string' },
      fixes: { type: 'array', items: { type: 'string' } },
      confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
      unanswered: { type: 'string' },
    },
    required: ['finding'],
    additionalProperties: false,
  },
};

/** Gemini spells a subset of JSON Schema differently and rejects the rest. */
function geminiSchema(schema) {
  if (Array.isArray(schema)) return schema.map(geminiSchema);
  if (!schema || typeof schema !== 'object') return schema;
  const out = {};
  for (const [key, value] of Object.entries(schema)) {
    if (key === 'additionalProperties') continue;
    out[key] = key === 'properties' || key === 'items' ? geminiSchema(value) : value;
  }
  return out;
}

/**
 * Shallow validation of the parsed answer — shape only.
 *
 * This is deliberately not the authority check. It answers "did the model produce the
 * document we asked for", never "is this action allowed". Everything that decides
 * permission, risk or device scope lives in Python, on the other side of the bridge.
 */
function shapeError(purpose, content) {
  if (!content || typeof content !== 'object' || Array.isArray(content)) {
    return 'The model did not return a JSON object.';
  }
  if (purpose === 'diagnose') {
    if (typeof content.finding !== 'string') return 'The model did not return a finding.';
    return '';
  }
  if (!Array.isArray(content.actions)) return 'The model did not return a list of actions.';
  if (content.reply !== undefined && (typeof content.reply !== 'string' || content.reply.length > 4000)) return 'The conversational reply is invalid or too long.';
  for (const entry of content.actions) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return 'The model returned an action that is not an object.';
    }
    if (typeof entry.type !== 'string' || !entry.type.trim()) {
      return 'The model returned an action with no type.';
    }
    if (entry.parameters !== undefined
      && (typeof entry.parameters !== 'object' || entry.parameters === null
        || Array.isArray(entry.parameters))) {
      return 'The model returned action parameters that are not an object.';
    }
  }
  return '';
}

/** Read the text payload out of each provider's success envelope. */
const EXTRACT = {
  anthropic(body) {
    const blocks = body && Array.isArray(body.content) ? body.content : [];
    const tool = blocks.find((block) => block && block.type === 'tool_use');
    if (tool && tool.input && typeof tool.input === 'object') return JSON.stringify(tool.input);
    // A provider that ignored `tool_choice` may answer in prose; try the text block as
    // JSON rather than discarding a possibly-correct plan.
    const text = blocks.find((block) => block && block.type === 'text');
    return text && typeof text.text === 'string' ? text.text : '';
  },
  openai(body) {
    const choice = body && Array.isArray(body.choices) ? body.choices[0] : null;
    const message = choice && choice.message;
    return message && typeof message.content === 'string' ? message.content : '';
  },
  gemini(body) {
    const candidates = body && Array.isArray(body.candidates) ? body.candidates : [];
    const parts = candidates[0] && candidates[0].content && candidates[0].content.parts;
    if (!Array.isArray(parts)) return '';
    return parts
      .map((part) => (part && typeof part.text === 'string' ? part.text : ''))
      .join('');
  },
};

/**
 * Parse a model's text into the object we asked for.
 * Models routinely wrap JSON in a ```json fence despite being told not to.
 */
function parseAnswer(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return { error: 'The model returned an empty answer.' };
  const candidates = [trimmed];
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) candidates.push(fenced[1].trim());
  const firstBrace = trimmed.indexOf('{');
  const lastBrace = trimmed.lastIndexOf('}');
  if (firstBrace >= 0 && lastBrace > firstBrace) {
    candidates.push(trimmed.slice(firstBrace, lastBrace + 1));
  }
  for (const candidate of candidates) {
    try {
      return { value: JSON.parse(candidate) };
    } catch {
      /* try the next extraction */
    }
  }
  return { error: 'The model did not return valid JSON.' };
}

function generationTimeout(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return GENERATION_TIMEOUT_S;
  return Math.min(Math.max(seconds, MIN_TIMEOUT_S), MAX_TIMEOUT_S);
}

function clampMaxTokens(value) {
  const tokens = Number(value);
  if (!Number.isFinite(tokens)) return DEFAULT_MAX_TOKENS;
  return Math.min(Math.max(Math.round(tokens), MIN_MAX_TOKENS), MAX_MAX_TOKENS);
}

function generationResult(state, detail, content) {
  const out = result(state, detail);
  // `content` is present only when the call actually produced a usable answer. A caller
  // that reads `content` without checking `state` gets `undefined`, not an empty plan.
  if (state === 'ok') out.content = content;
  return out;
}

/**
 * Ask the configured provider for one structured answer.
 *
 * @param {{provider:string, model:string, baseUrl:string, apiKey:string,
 *          timeoutSeconds:number, maxTokens:number}} config
 * @param {{purpose:'plan'|'diagnose', system:string, user:string}} request
 * @param {{request?:Function}} [options]  https.request-shaped, injected by tests
 * @returns {Promise<{state:string, text:string, detail:string, content?:object}>}
 */
async function generate(config, request, options = {}) {
  const source = config && typeof config === 'object' ? config : {};
  const want = request && typeof request === 'object' ? request : {};
  const provider = String(source.provider || '').trim();
  const model = String(source.model || '').trim();
  const baseUrl = String(source.baseUrl || '').trim();
  const apiKey = String(source.apiKey || '').trim();
  const requestImpl = (options && options.request) || null;

  const spec = specFor(provider, source.purpose);
  if (!spec) return generationResult('not_configured', 'Choose a provider first.');
  if (!spec.implemented) {
    return generationResult(
      'provider_unavailable',
      spec.unavailableReason || 'This provider is not implemented.',
    );
  }

  const purpose = want.purpose === 'diagnose' ? 'diagnose' : 'plan';
  const system = String(want.system || '');
  const user = String(want.user || '');
  if (!user.trim()) return generationResult('not_configured', 'There is nothing to ask the model.');
  if (system.length > MAX_PROMPT_CHARS || user.length > MAX_PROMPT_CHARS) {
    return generationResult('not_configured', 'The prompt is too large to send.');
  }

  // Same rule as the probe, and for the same reason: no key, no call. Checked before the
  // model so a missing credential is always what the UI reports.
  if (spec.requiresKey && !apiKey) {
    return generationResult('missing_key', 'No API key is stored for this provider.');
  }
  if (apiKey && !keyIsHeaderSafe(apiKey)) {
    return generationResult(
      'not_configured',
      'The stored API key contains characters that cannot be sent in a header.',
    );
  }

  const chosen = model || spec.defaultModel;
  if (!chosen) return generationResult('not_configured', 'Choose a model first.');

  const schema = SCHEMAS[purpose];
  const maxTokens = clampMaxTokens(source.maxTokens);
  const timeoutSeconds = generationTimeout(source.timeoutSeconds);
  const systemText = system
    + '\n\nAnswer with a single JSON object that matches the required schema,'
    + ' and nothing else.'
    + (provider === 'openai_compatible' ? '\nRequired JSON Schema:\n' + JSON.stringify(schema) : '');

  let probe;
  if (provider === 'anthropic') {
    probe = await post(
      'https://api.anthropic.com/v1/messages',
      { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
      {
        model: chosen,
        max_tokens: maxTokens,
        system: systemText,
        messages: [{ role: 'user', content: user }],
        tools: [{
          name: purpose === 'diagnose' ? 'diagnose' : 'propose_plan',
          description: 'Return the structured answer.',
          input_schema: schema,
        }],
        tool_choice: {
          type: 'tool',
          name: purpose === 'diagnose' ? 'diagnose' : 'propose_plan',
        },
      },
      timeoutSeconds,
      requestImpl,
      MAX_GENERATION_BODY,
    );
  } else if (provider === 'openai') {
    probe = await post(
      'https://api.openai.com/v1/chat/completions',
      { authorization: `Bearer ${apiKey}` },
      {
        model: chosen,
        max_tokens: maxTokens,
        messages: [
          { role: 'system', content: systemText },
          { role: 'user', content: user },
        ],
        response_format: {
          type: 'json_schema',
          json_schema: { name: 'actionmesh_answer', strict: true, schema },
        },
      },
      timeoutSeconds,
      requestImpl,
      MAX_GENERATION_BODY,
    );
  } else if (provider === 'gemini') {
    const url = 'https://generativelanguage.googleapis.com/v1beta/models/'
      + `${encodeURIComponent(chosen)}:generateContent?key=${encodeURIComponent(apiKey)}`;
    probe = await post(
      url,
      {},
      {
        systemInstruction: { parts: [{ text: systemText }] },
        contents: [{ role: 'user', parts: [{ text: user }] }],
        generationConfig: {
          maxOutputTokens: maxTokens,
          responseMimeType: 'application/json',
          responseSchema: geminiSchema(schema),
        },
      },
      timeoutSeconds,
      requestImpl,
      MAX_GENERATION_BODY,
    );
  } else if (provider === 'openai_compatible') {
    if (!baseUrl) return generationResult('not_configured', 'Enter the endpoint base URL.');
    const endpoint = compatibleEndpoint(baseUrl);
    if (endpoint.error) return generationResult('provider_unavailable', endpoint.error);
    probe = await post(
      endpoint.url,
      { authorization: `Bearer ${apiKey}` },
      {
        model: chosen,
        max_tokens: maxTokens,
        messages: [
          { role: 'system', content: systemText },
          { role: 'user', content: user },
        ],
        stream: false,
        response_format: { type: 'json_object' },
      },
      timeoutSeconds,
      requestImpl,
      MAX_GENERATION_BODY,
    );
  } else {
    return generationResult('provider_unavailable', 'This provider is not implemented.');
  }

  if (probe.kind !== 'response') {
    // The transport failed; reuse the same classification as the probe so the UI has one
    // vocabulary for "timeout" and "unreachable" whether it was testing or planning.
    const verdict = classify(probe);
    return generationResult(verdict.state, verdict.detail);
  }

  const status = probe.status;
  if (status < 200 || status >= 300) {
    const verdict = classify(probe);
    // 2xx is the only status whose body is read. An error body can echo the request, so
    // its text is discarded and only the status-derived sentence is surfaced.
    return generationResult(verdict.state, verdict.detail);
  }

  const extractor = provider === 'gemini'
    ? EXTRACT.gemini
    // An OpenAI-compatible server answers the OpenAI chat envelope, so it reads with the
    // same extractor — the difference between the two is the endpoint and the schema
    // dialing, not the answer shape.
    : provider === 'openai_compatible'
      ? EXTRACT.openai
      : EXTRACT[provider];
  if (typeof extractor !== 'function') {
    return generationResult('provider_unavailable', 'This provider is not implemented.');
  }

  let envelope;
  try {
    envelope = JSON.parse(probe.body);
  } catch {
    return generationResult('malformed', 'The provider returned a body that was not JSON.');
  }

  const answer = parseAnswer(extractor(envelope));
  if (answer.error) return generationResult('malformed', answer.error);
  const shape = shapeError(purpose, answer.value);
  if (shape) return generationResult('malformed', shape);

  return generationResult('ok', '', answer.value);
}

/**
 * Test seam: run `test()` with an injected request function.
 *
 * Accepts either `_withRequest(config, requestImpl)` or the older
 * `_withRequest(requestImpl, config)` order, so a caller cannot silently get the
 * arguments the wrong way round.
 */
function _withRequest(first, second) {
  if (typeof first === 'function') return test(second, { request: first });
  return test(first, { request: second });
}

module.exports = {
  SPECS,
  MEDIA_SPECS,
  publicSpecs,
  specFor,
  test,
  generate,
  transcribe,
  searchRouter,
  todoistTasks,
  githubRepositories,
  generateImage,
  MAX_AUDIO_BYTES,
  _withRequest,
  STATE_TEXT,
  MAX_BODY,
  MAX_GENERATION_BODY,
  DEFAULT_TIMEOUT_S,
  GENERATION_TIMEOUT_S,
  compatibleEndpoint,
};
