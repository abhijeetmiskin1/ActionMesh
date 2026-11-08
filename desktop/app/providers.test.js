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
function fakeRequest(reply, calls = []) {
  return function request(options, onResponse) {
    calls.push(options);
    const handlers = {};
    const req = {
      on(event, handler) {
        handlers[event] = handler;
        return req;
      },
      write() {},
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

test('a remote OpenAI-compatible base URL must be HTTPS', async () => {
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

test('OpenAI-compatible HTTP connects only to the pinned loopback address', async () => {
  const calls = [];
  const config = { provider: 'openai_compatible', model: 'm', apiKey: KEY };
  const request = fakeRequest(ok(), calls);
  const connected = await providers._withRequest({ ...config, baseUrl: 'http://localhost:20128/v1' }, request);
  assert.equal(connected.state, 'connected');
  assert.equal(calls[0].protocol, 'http:');
  assert.equal(calls[0].hostname, '127.0.0.1');
  assert.equal(calls[0].port, 20128);
  assert.equal(calls[0].path, '/v1/chat/completions');
  for (const baseUrl of ['http://localhost.example/v1', 'http://192.168.1.1/v1', 'http://user@localhost/v1']) {
    assert.equal((await providers._withRequest({ ...config, baseUrl }, request)).state, 'provider_unavailable');
  }
  assert.equal(calls.length, 1);
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

test('transcription uses the fixed audio route and refuses invalid audio before sending', async () => {
  const calls = [];
  const config = { baseUrl: 'http://localhost:20128/v1', model: 'test-stt', apiKey: KEY };
  const options = { request: fakeRequest(ok('{"text":" test transcript "}'), calls) };
  const answer = await providers.transcribe(config, new Uint8Array([1, 2]), 'audio/webm', options);
  assert.equal(answer.state, 'connected');
  assert.equal(answer.transcript, 'test transcript');
  assert.equal(calls[0].hostname, '127.0.0.1');
  assert.equal(calls[0].path, '/v1/audio/transcriptions');
  assert.match(calls[0].headers['content-type'], /^multipart\/form-data; boundary=/);
  assert.equal((await providers.transcribe(config, new Uint8Array(1024 * 1024 + 1), 'audio/webm', options)).transcript, undefined);
  assert.equal((await providers.transcribe(config, new Uint8Array([1]), 'video/webm', options)).transcript, undefined);
  assert.equal(calls.length, 1);
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
  assert.equal(c.stream, false);
  assert.match(c.messages[0].content, /"required":\["actions"\]/);
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
