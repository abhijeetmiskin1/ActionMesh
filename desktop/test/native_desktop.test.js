'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function harness(t, { packaged = true, allowDesktop = true, allowMedia = false, probe = { status: 0, stdout: '{"available":true}' } } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'actionmesh-native-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const resources = path.join(root, 'resources');
  const runtime = path.join(resources, 'runtime');
  fs.mkdirSync(path.join(runtime, 'python/bin'), { recursive: true });
  fs.writeFileSync(path.join(runtime, 'python/bin/python3'), 'fixture');
  fs.mkdirSync(path.join(runtime, 'app'), { recursive: true });
  const calls = [];
  const filename = path.resolve(__dirname, '../app/backend.js');
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), {
    require: (name) => name === 'child_process'
      ? { ...require(name), spawnSync: (...args) => { calls.push(args); return probe; } }
      : require(name),
    module, exports: module.exports, process: { ...process, platform: 'linux' },
    console, __dirname: path.dirname(filename), Buffer, setTimeout, clearTimeout, fetch,
  });
  const backend = new module.exports.Backend({ dataDir: path.join(root, 'profile'),
    appRoot: root, isPackaged: packaged, resourcesPath: resources, key: 'ephemeral-test-key',
    settings: { agentEnabled: true, allowDesktop, allowMedia } });
  backend.prepare();
  return { backend, calls, runtime };
}

test('packaged native bindings are enabled only after a fixed host probe', (t) => {
  const { backend, calls, runtime } = harness(t);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/usr/bin/python3');
  assert.deepEqual(Array.from(calls[0][1]), ['-I', path.join(runtime, 'app/actionmesh/agent/device_screen.py'), '--native-probe']);
  assert.equal(calls[0][2].timeout, 3000);
  assert.equal(Object.keys(calls[0][2].env).length, 0);
  assert.equal(backend.env(12345, '/fixture').ACTIONMESH_NATIVE_DESKTOP, '1');
});

test('failed or malformed binding probes leave packaged desktop unavailable', (t) => {
  for (const probe of [{ status: 1, stdout: '' }, { status: 0, stdout: 'bad' }, { status: 0, stdout: '{"available":false}' }]) {
    const { backend } = harness(t, { probe });
    assert.equal(backend.env(12345, '/fixture').ACTIONMESH_NATIVE_DESKTOP, '0');
  }
});

test('desktop disabled and development runtimes do not probe or enable the host helper', (t) => {
  for (const options of [{ allowDesktop: false }, { packaged: false }]) {
    const { backend, calls } = harness(t, options);
    assert.equal(calls.length, 0);
    assert.equal(backend.env(12345, '/fixture').ACTIONMESH_NATIVE_DESKTOP, '0');
  }
});


test('media host bindings are independent of desktop access', (t) => {
  const { backend, calls, runtime } = harness(t, { allowDesktop: false, allowMedia: true });
  assert.equal(calls.length, 1);
  assert.deepEqual(Array.from(calls[0][1]), ['-I', path.join(runtime, 'app/actionmesh/agent/mpris.py'), '--native-probe']);
  const environment = backend.env(12345, '/fixture');
  assert.equal(environment.ACTIONMESH_NATIVE_MEDIA, '1');
  assert.equal(environment.ACTIONMESH_NATIVE_DESKTOP, '0');
  assert.equal(environment.ACTIONMESH_ALLOW_DESKTOP, '0');
});
