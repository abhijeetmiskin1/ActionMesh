'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function navigationWaiter() {
  const module = { exports: {} };
  const source = fs.readFileSync(path.join(__dirname, '../app/browser_window.js'), 'utf8');
  vm.runInNewContext(source, { module, exports: module.exports,
    require: () => ({}), setTimeout, clearTimeout });
  return module.exports._waitForNavigation;
}

test('history navigation waits for load completion instead of returning the old page', async () => {
  const wait = navigationWaiter();
  const wc = new EventEmitter();
  let resolved = false;
  const promise = wait(wc, () => {}).then((answer) => { resolved = true; return answer; });
  await new Promise(setImmediate);
  assert.equal(resolved, false);
  wc.emit('did-stop-loading');
  assert.equal(await promise, true);
  assert.equal(wc.listenerCount('destroyed'), 0);
});

test('navigation timeout and destruction return failure and release event handlers', async () => {
  const wait = navigationWaiter();
  const timeout = new EventEmitter();
  assert.equal(await wait(timeout, () => {}, 5), false);
  assert.equal(timeout.listenerCount('did-stop-loading'), 0);
  const destroyed = new EventEmitter();
  const pending = wait(destroyed, () => {});
  destroyed.emit('destroyed');
  assert.equal(await pending, false);
  assert.equal(destroyed.listenerCount('did-stop-loading'), 0);
});

test('synchronous navigation exceptions release both handlers', async () => {
  const wait = navigationWaiter();
  const wc = new EventEmitter();
  await assert.rejects(wait(wc, () => { throw new Error('navigation refused'); }), /refused/);
  assert.equal(wc.listenerCount('did-stop-loading'), 0);
  assert.equal(wc.listenerCount('destroyed'), 0);
});
