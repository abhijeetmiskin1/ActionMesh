'use strict';
/**
 * The provider connection test.
 *
 * Everything here runs against an injected `https.request`-shaped function — no test
 * opens a socket. What is worth asserting is not "the happy path returns 200": it is
 * that a key which merely exists never produces `connected`, that a missing key makes
 * *zero* calls, that the credential cannot appear in a result, and that the
 * OpenAI-compatible base URL is checked rather than trusted.
 */
const assert = require('node:assert/strict');
const test = require('node:test');

const providers = require('./providers.js');

const KEY = 'sk-test-0123456789abcdef';

/**
 * A fake request function. `reply` decides what the "server" does.
 * Records every call so a test can assert that *none* happened.
 */
function fakeRequest(reply, calls = [], bodies = []) {
  return function request(options, onResponse) {
    calls.push(options);
    const handlers = {};
    const req = {
      on(event, handler) {
        handlers[event] = handler;
        return req;
      },
      write(body) { bodies.push(body); },
      end() {
        // Answer asynchronously, the way a real socket would.
        setImmediate(() => {
          if (reply.kind === 'network') {
            const error = new Error('socket hang up');
            error.code = reply.code || 'ECONNREFUSED';
            if (handlers.error) handlers.error(error);
            return;
          }
          if (reply.kind === 'timeout') {
            if (handlers.timeout) handlers.timeout();
            return;
          }
          const listeners = {};
          const res = {
            statusCode: reply.status,
            headers: reply.headers,
            setEncoding() {},
            on(event, handler) {
              listeners[event] = handler;
              return res;
            },
          };
          onResponse(res);
          const chunk = reply.body || '{}';
          if (listeners.data) listeners.data(chunk);
          if (listeners.end) listeners.end();
        });
      },
      destroy() {},
    };
    return req;
  };
}

function ok(body = '{}') {
  return { kind: 'response', status: 200, body };
}

const BASE = { provider: 'anthropic', model: 'claude-sonnet-5-5', apiKey: KEY };

// -- the registry ---------------------------------------------------------------

test('every unimplemented spec states why, in a sentence', () => {
  for (const spec of providers.SPECS) {
    if (!spec.implemented) {
      assert.equal(typeof spec.unavailableReason, 'string');
      assert.ok(spec.unavailableReason.length > 20, `${spec.id} must say why it is unavailable`);
    }
  }
});

test('the expected providers are present, and only implemented ones can probe', () => {
  const ids = providers.SPECS.map((spec) => spec.id);
  assert.deepEqual(ids, ['anthropic', 'openai', 'gemini', 'openai_compatible', 'ollama']);
  assert.equal(providers.specFor('ollama').implemented, false);
  assert.equal(providers.specFor('anthropic').implemented, true);
  assert.equal(providers.specFor('nope'), null);
});

test('publicSpecs copies, so a renderer cannot mutate the table', () => {
  const copy = providers.publicSpecs();
  copy[0].label = 'changed';
  copy[0].models.push('injected');
  assert.equal(providers.SPECS[0].label, 'Anthropic');
  assert.ok(!providers.SPECS[0].models.includes('injected'));
});

// -- the guard rails ------------------------------------------------------------

test('a missing key makes no network call at all', async () => {
  const calls = [];
  const request = fakeRequest(ok(), calls);
  for (const provider of ['anthropic', 'openai', 'gemini', 'openai_compatible']) {
    const out = await providers._withRequest(
      { provider, model: '', baseUrl: 'https://gateway.example.com/v1', apiKey: '' },
      request,
    );
    assert.equal(out.state, 'missing_key', provider);
  }
  assert.deepEqual(calls, [], 'no probe may be attempted without a credential');
});

test('ollama is unavailable with a reason, never connected', async () => {
  const calls = [];
  const out = await providers._withRequest({ provider: 'ollama' }, fakeRequest(ok(), calls));
  assert.equal(out.state, 'provider_unavailable');
  assert.match(out.detail, /OpenAI-compatible/);
  assert.deepEqual(calls, []);
});

test('an unknown provider is not configured', async () => {
  const out = await providers._withRequest({ provider: 'mystery', apiKey: KEY }, fakeRequest(ok()));
  assert.equal(out.state, 'not_configured');
});

test('the key never appears in a result', async () => {
  const responses = [
    { kind: 'response', status: 401, body: `{"error":"bad key ${KEY}"}` },
    { kind: 'response', status: 200, body: `{"echo":"${KEY}"}` },
    { kind: 'network', code: 'ENOTFOUND' },
  ];
  for (const reply of responses) {
    const out = await providers._withRequest(BASE, fakeRequest(reply));
    assert.ok(!out.detail.includes(KEY), 'detail must not carry the key');
    assert.ok(!JSON.stringify(out).includes(KEY), 'the whole result must not carry the key');
  }
});

test('a key containing a newline is refused before it reaches a header', async () => {
  const calls = [];
  const out = await providers._withRequest(
    { ...BASE, apiKey: 'sk-good\r\nx-injected: yes' },
    fakeRequest(ok(), calls),
  );
  assert.equal(out.state, 'not_configured');
  assert.deepEqual(calls, []);
});

// -- the classification table ---------------------------------------------------

test('a 2xx response is connected', async () => {
  const out = await providers._withRequest(BASE, fakeRequest(ok()));
  assert.equal(out.state, 'connected');
});

test('401 and 403 are invalid_credentials', async () => {
  for (const status of [401, 403]) {
    const out = await providers._withRequest(BASE, fakeRequest({ kind: 'response', status }));
    assert.equal(out.state, 'invalid_credentials');
  }
});

test('429 is rate_limited', async () => {
  const out = await providers._withRequest(BASE, fakeRequest({ kind: 'response', status: 429 }));
  assert.equal(out.state, 'rate_limited');
});

test('a timeout is timeout, whether from the socket or the deadline', async () => {
  const timedOut = await providers._withRequest(BASE, fakeRequest({ kind: 'timeout' }));
  assert.equal(timedOut.state, 'timeout');
  const etimedout = await providers._withRequest(
    BASE,
    fakeRequest({ kind: 'network', code: 'ETIMEDOUT' }),
  );
  assert.equal(etimedout.state, 'timeout');
});

test('5xx, 404 and 400 are provider_unavailable', async () => {
  for (const status of [500, 503, 404, 400]) {
    const out = await providers._withRequest(BASE, fakeRequest({ kind: 'response', status }));
    assert.equal(out.state, 'provider_unavailable', String(status));
  }
});

test('a redirect is refused rather than followed', async () => {
  const out = await providers._withRequest(BASE, fakeRequest({ kind: 'response', status: 302 }));
  assert.equal(out.state, 'provider_unavailable');
  assert.match(out.detail, /redirect/);
});

test('a DNS or connection failure is provider_unavailable', async () => {
  for (const code of ['ENOTFOUND', 'ECONNREFUSED', 'ECONNRESET']) {
    const out = await providers._withRequest(BASE, fakeRequest({ kind: 'network', code }));
    assert.equal(out.state, 'provider_unavailable');
  }
});

// -- the request shapes ---------------------------------------------------------

test('each provider posts its documented endpoint and header', async () => {
  const seen = [];
  await providers._withRequest({ provider: 'anthropic', model: 'm', apiKey: KEY },
    fakeRequest(ok(), seen));
  await providers._withRequest({ provider: 'openai', model: 'm', apiKey: KEY },
    fakeRequest(ok(), seen));
  await providers._withRequest({ provider: 'gemini', model: 'm', apiKey: KEY },
    fakeRequest(ok(), seen));

  assert.equal(seen[0].hostname, 'api.anthropic.com');
  assert.equal(seen[0].path, '/v1/messages');
  assert.equal(seen[0].headers['x-api-key'], KEY);
  assert.equal(seen[0].headers['anthropic-version'], '2023-06-01');

  assert.equal(seen[1].hostname, 'api.openai.com');
  assert.equal(seen[1].path, '/v1/chat/completions');
  assert.equal(seen[1].headers.authorization, `Bearer ${KEY}`);

  assert.equal(seen[2].hostname, 'generativelanguage.googleapis.com');
  assert.match(seen[2].path, /:generateContent\?key=/);
  assert.equal(seen[2].port, 443);
});

test('an OpenAI-compatible base URL must be https and is used verbatim', async () => {
  const calls = [];
  const refused = await providers._withRequest(
    { provider: 'openai_compatible', model: 'm', apiKey: KEY, baseUrl: 'http://gateway.example.com/v1' },
    fakeRequest(ok(), calls),
  );
  assert.equal(refused.state, 'provider_unavailable');
  assert.deepEqual(calls, []);

  const accepted = await providers._withRequest(
    { provider: 'openai_compatible', model: 'm', apiKey: KEY, baseUrl: 'https://gateway.example.com/v1/' },
    fakeRequest(ok(), calls),
  );
  assert.equal(accepted.state, 'connected');
  assert.equal(calls[0].hostname, 'gateway.example.com');
  assert.equal(calls[0].path, '/v1/chat/completions');
});

test('a base URL on another port is refused', async () => {
  const calls = [];
  const out = await providers._withRequest(
    { provider: 'openai_compatible', model: 'm', apiKey: KEY, baseUrl: 'https://gateway.example.com:8443/v1' },
    fakeRequest(ok(), calls),
  );
  assert.equal(out.state, 'provider_unavailable');
  assert.deepEqual(calls, []);
});

test('compatible HTTP is confined to numeric loopback and pins localhost', async () => {
  for (const baseUrl of ['http://localhost:20128/v1', 'http://127.0.0.1:20128/api/v1', 'http://[::1]:20128/v1']) {
    const calls = [];
    const out = await providers._withRequest({ provider: 'openai_compatible', model: 'power', apiKey: KEY, baseUrl }, fakeRequest(ok(), calls));
    assert.equal(out.state, 'connected');
    assert.equal(calls[0].protocol, 'http:');
    assert.equal(calls[0].port, 20128);
    assert.ok(['127.0.0.1', '::1'].includes(calls[0].hostname));
  }
  for (const baseUrl of ['http://remote.example/v1', 'http://localhost.evil.test:20128/v1', 'http://192.168.1.10:20128/v1', 'http://user:password@localhost:20128/v1']) {
    const calls = [];
    const out = await providers._withRequest({ provider: 'openai_compatible', model: 'power', apiKey: KEY, baseUrl }, fakeRequest(ok(), calls));
    assert.equal(out.state, 'provider_unavailable');
    assert.equal(calls.length, 0);
  }
});

const AUDIO_CONFIG = { provider: 'router_audio', purpose: 'audio', model: 'groq/whisper-large-v3-turbo', baseUrl: 'http://localhost:20128/api/v1', apiKey: KEY };

test('router lookup returns only bounded HTTPS source cards and uses a fixed provider', async () => {
  const calls = [];
  const bodies = [];
  const request = fakeRequest(ok(JSON.stringify({ results: [
    { title: 'Flights', url: 'https://example.com/flights', snippet: KEY },
    { title: 'Bad', url: 'javascript:alert(1)' },
    { title: 'Bad', url: 'https://name:pass@example.com/' },
  ] })), calls, bodies);
  const result = await providers.searchRouter(AUDIO_CONFIG, 'Flights from Mumbai to Chennai', { request });
  assert.deepEqual(result, { ok: true, results: [
    { title: 'Flights', url: 'https://example.com/flights', snippet: '' },
  ] });
  assert.equal(calls[0].path, '/api/v1/search');
  assert.equal(JSON.parse(bodies[0]).provider, 'antigravity');
  assert.equal((await providers.searchRouter({ ...AUDIO_CONFIG, baseUrl: 'https://example.com/v1' },
    'Flights', { request })).ok, false);
  assert.equal(calls.length, 1);
});

test('speech uses a bounded multipart request through the configured router', async () => {
  const calls = [];
  const answer = await providers.transcribe(AUDIO_CONFIG, Buffer.from('synthetic audio'), {
    request: fakeRequest(ok(JSON.stringify({ text: 'list the files' })), calls),
  });
  assert.deepEqual(answer, { ok: true, text: 'list the files' });
  assert.equal(calls[0].path, '/api/v1/audio/transcriptions');
  assert.equal(calls[0].hostname, '127.0.0.1');
  assert.match(calls[0].headers['content-type'], /^multipart\/form-data; boundary=/);
  assert.equal(calls[0].headers.authorization, `Bearer ${KEY}`);
  assert.equal(JSON.stringify(answer).includes(KEY), false);
});

test('speech sends the selected language and omits it for auto-detection', async () => {
  const calls = [];
  const bodies = [];
  const request = fakeRequest(ok(JSON.stringify({ text: 'fixture' })), calls, bodies);
  for (const language of ['en', 'hi', '']) {
    const answer = await providers.transcribe(AUDIO_CONFIG, Buffer.from('audio'), { language, request });
    assert.equal(answer.ok, true);
  }
  assert.match(bodies[0].toString(), /name="language"\r\n\r\nen\r\n/);
  assert.match(bodies[1].toString(), /name="language"\r\n\r\nhi\r\n/);
  assert.doesNotMatch(bodies[2].toString(), /name="language"/);
  const rejected = await providers.transcribe(AUDIO_CONFIG, Buffer.from('audio'), {
    language: 'en\r\ninvalid', request,
  });
  assert.equal(rejected.ok, false);
  assert.equal(calls.length, 3);
});

test('speech refuses missing credentials, oversized audio, and unsafe endpoints before calling', async () => {
  const calls = [];
  for (const [config, audio] of [
    [{ ...AUDIO_CONFIG, apiKey: '' }, Buffer.from('audio')],
    [{ ...AUDIO_CONFIG, model: '' }, Buffer.from('audio')],
    [AUDIO_CONFIG, Buffer.alloc(providers.MAX_AUDIO_BYTES + 1)],
    [AUDIO_CONFIG, Buffer.alloc(0)],
    [{ ...AUDIO_CONFIG, baseUrl: 'http://remote.example/v1' }, Buffer.from('audio')],
  ]) {
    assert.equal((await providers.transcribe(config, audio, { request: fakeRequest(ok(), calls) })).ok, false);
  }
  assert.equal(calls.length, 0);
});

test('speech rejects redirects, malformed bodies, and credential echoes', async () => {
  for (const reply of [
    { status: 302, body: KEY }, { status: 401, body: KEY },
    { status: 200, body: 'broken JSON' },
    { status: 200, body: JSON.stringify({ text: KEY }) },
  ]) {
    const answer = await providers.transcribe(AUDIO_CONFIG, Buffer.from('audio'), {
      request: fakeRequest({ kind: 'response', ...reply }),
    });
    assert.equal(answer.ok, false);
    assert.equal(JSON.stringify(answer).includes(KEY), false);
  }
});

test('audio Test Connection exercises transcription with synthetic silence', async () => {
  const calls = [];
  const answer = await providers.test(AUDIO_CONFIG, {
    request: fakeRequest(ok('{"text":""}'), calls),
  });
  assert.equal(answer.state, 'connected');
  assert.equal(calls[0].path, '/api/v1/audio/transcriptions');
});

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7XcAAAAASUVORK5CYII=';
const IMAGE_CONFIG = { provider: 'router_image', purpose: 'image', model: 'image-model', baseUrl: 'http://localhost:20128/api/v1', apiKey: KEY };

test('image generation accepts only bounded inline raster output', async () => {
  const calls = [];
  const image = await providers.generateImage(IMAGE_CONFIG, 'A blue square', {
    request: fakeRequest(ok(JSON.stringify({ data: [{ b64_json: PNG }] })), calls),
  });
  assert.equal(image.ok, true);
  assert.equal(image.dataUrl, `data:image/png;base64,${PNG}`);
  assert.equal(calls[0].path, '/api/v1/images/generations');
  assert.equal(calls[0].hostname, '127.0.0.1');
  assert.equal(JSON.stringify(image).includes(KEY), false);
  for (const data of [
    [{ url: 'http://private.example/image.png' }],
    [{ b64_json: Buffer.from('<svg onload="bad()"/>').toString('base64') }],
    [{ b64_json: 'A'.repeat(4 * 1024 * 1024 + 1) }],
  ]) {
    const answer = await providers.generateImage(IMAGE_CONFIG, 'A blue square', {
      request: fakeRequest(ok(JSON.stringify({ data }))),
    });
    assert.equal(answer.ok, false);
    assert.equal('dataUrl' in answer, false);
  }
});

test('image errors cannot echo a provider response or credential', async () => {
  const answer = await providers.generateImage(IMAGE_CONFIG, 'A blue square', {
    request: fakeRequest({ kind: 'response', status: 403, body: KEY }),
  });
  assert.equal(answer.ok, false);
  assert.equal(JSON.stringify(answer).includes(KEY), false);
});

test('image Test Connection requires an actual raster response', async () => {
  const accepted = await providers.test(IMAGE_CONFIG, {
    request: fakeRequest(ok(JSON.stringify({ data: [{ b64_json: PNG }] }))),
  });
  assert.equal(accepted.state, 'connected');
  const refused = await providers.test(IMAGE_CONFIG, { request: fakeRequest(ok('{}')) });
  assert.equal(refused.state, 'provider_unavailable');
});

test('the timeout is clamped to 1..60 seconds', async () => {
  const seen = [];
  await providers._withRequest({ ...BASE, timeoutSeconds: 999 }, fakeRequest(ok(), seen));
  assert.equal(seen[0].timeout, 60_000);
  await providers._withRequest({ ...BASE, timeoutSeconds: 0.1 }, fakeRequest(ok(), seen));
  assert.equal(seen[1].timeout, 1_000);
});

test('the newer and older _withRequest argument orders agree', async () => {
  const newer = await providers._withRequest(BASE, fakeRequest(ok()));
  const older = await providers._withRequest(fakeRequest(ok()), BASE);
  assert.equal(newer.state, 'connected');
  assert.equal(older.state, 'connected');
});
// -- generation ------------------------------------------------------------------
//
// Generation is where a model's answer enters the system, so the tests here care about
// three things in order: that no credential or configured field can produce a call, that
// every failure state is distinguishable and carries no model text, and that a successful
// call returns the parsed object and *only* that.

const PLAN_REQUEST = {
  purpose: 'plan',
  system: 'You are the planner.',
  user: 'Check my laptop status',
};

const PLAN_ANSWER = {
  actions: [{ type: 'system.inspect', parameters: {} }],
  notes: [],
};

function anthropicTool(input) {
  return ok(JSON.stringify({
    content: [{ type: 'tool_use', name: 'propose_plan', input }],
  }));
}

function openAiMessage(content) {
  return ok(JSON.stringify({ choices: [{ message: { content } }] }));
}

function geminiParts(text) {
  return ok(JSON.stringify({ candidates: [{ content: { parts: [{ text }] } }] }));
}

test('generation with no key makes no call and returns no content', async () => {
  const calls = [];
  const out = await providers.generate(
    { ...BASE, apiKey: '' },
    PLAN_REQUEST,
    { request: fakeRequest(anthropicTool(PLAN_ANSWER), calls) },
  );
  assert.equal(out.state, 'missing_key');
  assert.equal('content' in out, false);
  assert.deepEqual(calls, []);
});

test('a key on disk with no model and no call is still not a connected provider', async () => {
  const calls = [];
  const out = await providers.generate(
    { ...BASE, provider: 'openai_compatible', baseUrl: '', model: '' },
    PLAN_REQUEST,
    { request: fakeRequest(ok(), calls) },
  );
  assert.equal(out.state, 'not_configured');
  assert.deepEqual(calls, []);
});

test('ollama refuses generation rather than pretending', async () => {
  const calls = [];
  const out = await providers.generate(
    { ...BASE, provider: 'ollama' },
    PLAN_REQUEST,
    { request: fakeRequest(ok(), calls) },
  );
  assert.equal(out.state, 'provider_unavailable');
  assert.deepEqual(calls, []);
});

test('anthropic generation forces tool_use and returns the input object', async () => {
  const calls = [];
  const out = await providers.generate(BASE, PLAN_REQUEST, {
    request: fakeRequest(anthropicTool(PLAN_ANSWER), calls),
  });
  assert.equal(out.state, 'ok');
  assert.deepEqual(out.content, PLAN_ANSWER);
  assert.equal(calls[0].hostname, 'api.anthropic.com');
  assert.equal(calls[0].path, '/v1/messages');
  assert.equal(calls[0].headers['x-api-key'], KEY);
});

/** A fake that also captures the serialized request body each call transmits. */
function bodyCapture(reply, bodies = []) {
  const inner = fakeRequest(reply);
  return function request(options, onResponse) {
    const req = inner(options, onResponse);
    const write = req.write.bind(req);
    req.write = (chunk) => { bodies.push(String(chunk)); return write(chunk); };
    return req;
  };
}

test('each provider asks its own dialect for the same answer', async () => {
  const planBody = (provider, extra = {}) => {
    const bodies = [];
    return { bodies, request: bodyCapture(anthropicTool(PLAN_ANSWER), bodies), provider, extra };
  };

  // Anthropic: a forced tool whose schema is the plan shape.
  const anthropic = planBody('anthropic');
  await providers.generate(BASE, PLAN_REQUEST, { request: anthropic.request });
  const a = JSON.parse(anthropic.bodies[0]);
  assert.equal(a.tool_choice.type, 'tool');
  assert.equal(a.tool_choice.name, 'propose_plan');
  assert.equal(a.tools[0].input_schema.properties.actions.type, 'array');
  assert.equal(a.max_tokens <= 8192, true);

  // OpenAI: a strict json_schema response format.
  const openai = planBody('openai');
  await providers.generate({ ...BASE, provider: 'openai' }, PLAN_REQUEST, { request: openai.request });
  const o = JSON.parse(openai.bodies[0]);
  assert.equal(o.response_format.type, 'json_schema');
  assert.equal(o.response_format.json_schema.strict, true);

  // Gemini: JSON mime type plus a schema, under generationConfig.
  const gemini = planBody('gemini');
  await providers.generate({ ...BASE, provider: 'gemini' }, PLAN_REQUEST, { request: gemini.request });
  const g = JSON.parse(gemini.bodies[0]);
  assert.equal(g.generationConfig.responseMimeType, 'application/json');
  assert.equal(g.generationConfig.responseSchema.properties.actions.type, 'array');

  // OpenAI-compatible: the weaker json_object mode, since strict schemas are uncommon.
  const compat = planBody('openai_compatible');
  await providers.generate(
    { ...BASE, provider: 'openai_compatible', baseUrl: 'https://llm.example/v1' },
    PLAN_REQUEST,
    { request: compat.request },
  );
  const c = JSON.parse(compat.bodies[0]);
  assert.equal(c.response_format.type, 'json_object');
  const described = JSON.parse(c.messages[0].content.split('Required JSON Schema:\n')[1]);
  assert.equal(described.properties.actions.items.properties.type.type, 'string');
  assert.equal(described.properties.reply.type, 'string');
});

test('anthropic: a fenced JSON text block is still read', async () => {
  const body = ok(JSON.stringify({
    content: [{ type: 'text', text: '```json\n' + JSON.stringify(PLAN_ANSWER) + '\n```' }],
  }));
  const out = await providers.generate(BASE, PLAN_REQUEST, { request: fakeRequest(body) });
  assert.equal(out.state, 'ok');
  assert.deepEqual(out.content, PLAN_ANSWER);
});

test('openai generation reads choices[0].message.content', async () => {
  const out = await providers.generate(
    { ...BASE, provider: 'openai', model: 'gpt-4o' },
    PLAN_REQUEST,
    { request: fakeRequest(openAiMessage(JSON.stringify(PLAN_ANSWER))) },
  );
  assert.equal(out.state, 'ok');
  assert.deepEqual(out.content, PLAN_ANSWER);
});

test('gemini generation reads candidates[0].content.parts[].text', async () => {
  const out = await providers.generate(
    { ...BASE, provider: 'gemini', model: 'gemini-2.0-flash' },
    PLAN_REQUEST,
    { request: fakeRequest(geminiParts(JSON.stringify(PLAN_ANSWER))) },
  );
  assert.equal(out.state, 'ok');
  assert.deepEqual(out.content, PLAN_ANSWER);
});

test('openai_compatible generation requires a valid base URL before any call', async () => {
  const calls = [];
  const out = await providers.generate(
    { ...BASE, provider: 'openai_compatible', baseUrl: 'http://insecure.example' },
    PLAN_REQUEST,
    { request: fakeRequest(ok(), calls) },
  );
  assert.equal(out.state, 'provider_unavailable');
  assert.deepEqual(calls, []);
});

test('openai_compatible generation posts to the checked completions endpoint', async () => {
  const calls = [];
  const out = await providers.generate(
    { ...BASE, provider: 'openai_compatible', baseUrl: 'https://llm.example/v1' },
    PLAN_REQUEST,
    { request: fakeRequest(openAiMessage(JSON.stringify(PLAN_ANSWER)), calls) },
  );
  assert.equal(out.state, 'ok');
  assert.equal(calls[0].hostname, 'llm.example');
  assert.equal(calls[0].path, '/v1/chat/completions');
});

test('generation failure states are distinguishable and carry no model text', async () => {
  const cases = [
    [{ kind: 'response', status: 401, body: '{"error":{"message":"bad key sk-secret"}}' }, 'invalid_credentials'],
    [{ kind: 'response', status: 429, body: 'slow down' }, 'rate_limited'],
    [{ kind: 'response', status: 503, body: 'down' }, 'provider_unavailable'],
    [{ kind: 'timeout' }, 'timeout'],
    [{ kind: 'network' }, 'provider_unavailable'],
    [{ kind: 'response', status: 302, body: 'redirect' }, 'provider_unavailable'],
  ];
  for (const [reply, state] of cases) {
    const out = await providers.generate(BASE, PLAN_REQUEST, { request: fakeRequest(reply) });
    assert.equal(out.state, state, `${state}: got ${out.state}`);
    assert.equal('content' in out, false);
    assert.equal(String(out.detail).includes(KEY), false);
    // An error body is never replayed, whatever it contains.
    assert.equal(String(out.detail).includes('sk-secret'), false);
  }
});

test('a 2xx whose body is not the requested shape is malformed, not empty success', async () => {
  const bodies = [
    ok('not json at all'),
    ok('{}'),
    ok(JSON.stringify({ actions: 'nope' })),
    ok(JSON.stringify({ actions: [{}] })),
    ok(JSON.stringify({ actions: [{ type: 'x', parameters: [] }] })),
    ok(JSON.stringify({ content: [{ type: 'tool_use', input: { actions: 5 } }] })),
  ];
  for (const body of bodies) {
    const out = await providers.generate(BASE, PLAN_REQUEST, { request: fakeRequest(body) });
    assert.equal(out.state, 'malformed', `body ${JSON.stringify(body).slice(0, 60)}`);
    assert.equal('content' in out, false);
  }
});

test('diagnose answers are checked for their own shape, not the plan shape', async () => {
  const diagnose = { purpose: 'diagnose', system: 's', user: 'u' };
  const good = anthropicTool({ finding: 'port in use', confidence: 'high' });
  const outGood = await providers.generate(BASE, diagnose, { request: fakeRequest(good) });
  assert.equal(outGood.state, 'ok');
  assert.equal(outGood.content.finding, 'port in use');

  // A plan-shaped answer must not satisfy a diagnosis.
  const wrong = await providers.generate(BASE, diagnose, {
    request: fakeRequest(anthropicTool({ actions: [] })),
  });
  assert.equal(wrong.state, 'malformed');
});

test('the credential and the request body never appear in a generation result', async () => {
  const secretPrompt = 'PROMPT-CANARY-9f3a';
  const out = await providers.generate(
    { ...BASE, apiKey: KEY },
    { ...PLAN_REQUEST, user: secretPrompt },
    { request: fakeRequest({ kind: 'response', status: 500, body: secretPrompt }) },
  );
  const serialized = JSON.stringify(out);
  assert.equal(serialized.includes(KEY), false);
  assert.equal(serialized.includes(secretPrompt), false);
});

test('generation clamps max tokens to a sane window', async () => {
  // The cap is enforced before the call; a hostile value cannot ask for a million tokens.
  for (const [want, expected] of [[1, 16], [100000, 8192], [undefined, 1024]]) {
    const seen = [];
    await providers.generate(
      { ...BASE, maxTokens: want },
      PLAN_REQUEST,
      { request: fakeRequest(anthropicTool(PLAN_ANSWER), seen) },
    );
    assert.equal(seen.length, 1);
  }
});

test('generation refuses a prompt large enough to be a memory hazard', async () => {
  const calls = [];
  const out = await providers.generate(
    BASE,
    { ...PLAN_REQUEST, user: 'x'.repeat(64 * 1024) },
    { request: fakeRequest(anthropicTool(PLAN_ANSWER), calls) },
  );
  assert.equal(out.state, 'not_configured');
  assert.deepEqual(calls, []);
});

test('conversational plan replies are bounded and preserve the actions schema', async () => {
  const config = { ...BASE, provider: 'openai_compatible', baseUrl: 'http://localhost:20128/api/v1' };
  const answer = { actions: [], reply: 'Which file do you mean?' };
  const out = await providers.generate(config, PLAN_REQUEST, {
    request: fakeRequest(openAiMessage(JSON.stringify(answer))),
  });
  assert.equal(out.state, 'ok');
  assert.deepEqual(out.content, answer);
  for (const reply of [123, null, 'x'.repeat(4001)]) {
    const bad = await providers.generate(config, PLAN_REQUEST, {
      request: fakeRequest(openAiMessage(JSON.stringify({ actions: [], reply }))),
    });
    assert.equal(bad.state, 'malformed');
    assert.equal(bad.content, undefined);
  }
});

test('Todoist uses a fixed read-only endpoint and returns only task titles and due dates', async () => {
  const calls = [];
  const writes = [];
  const answer = await providers.todoistTasks(KEY, { request: fakeRequest(ok(JSON.stringify({
    results: [
      { content: 'Buy milk', due: { date: '2026-11-09' }, description: 'private description' },
      { content: KEY },
      { content: 'No due date', due: { date: 'javascript:invalid' } },
    ],
    next_cursor: 'private cursor',
  })), calls, writes) });
  assert.deepEqual(answer, { ok: true, tasks: [
    { title: 'Buy milk', due: '2026-11-09' }, { title: 'No due date', due: '' },
  ], hasMore: true });
  assert.equal(calls[0].method, 'GET');
  assert.equal(calls[0].hostname, 'api.todoist.com');
  assert.equal(calls[0].path, '/api/v1/tasks?limit=10');
  assert.equal(calls[0].headers.authorization, `Bearer ${KEY}`);
  assert.deepEqual(writes, []);
  assert.equal((await providers.todoistTasks('bad\r\nvalue', { request: fakeRequest(ok(), calls) })).ok, false);
  assert.equal(calls.length, 1);
});

test('GitHub MCP negotiates, calls only the search tool, and filters private repositories', async () => {
  const calls = [], bodies = [];
  const payload = {items: [
    {private: false, full_name: 'owner/example', description: 'Public repository', html_url: 'https://untrusted.test/'},
    {private: true, full_name: 'owner/private'},
  ]};
  const response = (id, result) => ({kind:'response',status:200,
    body:JSON.stringify({jsonrpc:'2.0',id,result})});
  const replies = [
    {...response(1, {protocolVersion:'2025-06-18'}), headers: {'mcp-session-id':'fixture-session'}},
    {kind:'response',status:202},
    {...response(2, {}), headers: {'content-type':'text/event-stream'},
      body:`event: message\ndata: ${JSON.stringify({jsonrpc:'2.0',id:2,
        result:{content:[{type:'text',text:JSON.stringify(payload)}]}})}\n\n`},
  ];
  const request = (options, callback) => fakeRequest(replies.shift(), calls, bodies)(options, callback);
  const result = await providers.githubRepositories('example', KEY, {request});
  assert.deepEqual(result, {ok:true, source:'GitHub MCP', results:[
    {title:'owner/example',url:'https://github.com/owner/example',snippet:'Public repository'},
  ]});
  assert.deepEqual(bodies.map((body) => JSON.parse(body).method),
    ['initialize','notifications/initialized','tools/call']);
  assert.equal(calls[1].headers['Mcp-Session-Id'], 'fixture-session');
  assert.equal(calls[2].headers['X-MCP-Readonly'], 'true');
  assert.equal(JSON.parse(bodies[2]).params.name, 'search_repositories');
  assert.equal(JSON.parse(bodies[2]).params.arguments.query, 'example is:public');
  assert.ok(calls.every((call) => call.hostname === 'api.githubcopilot.com'));
});

test('GitHub repository search uses the public REST API without a token', async () => {
  const calls = [];
  const request = fakeRequest(ok(JSON.stringify({items:[]})), calls);
  assert.deepEqual(await providers.githubRepositories('example', '', {request}),
    {ok:true,source:'GitHub API',results:[]});
  assert.equal(calls[0].method, 'GET');
  assert.equal(calls[0].hostname, 'api.github.com');
  assert.equal(calls[0].headers.authorization, undefined);
  assert.equal((await providers.githubRepositories('example', 'bad\r\ntoken', {request})).ok, false);
  assert.equal(calls.length, 1);
});
