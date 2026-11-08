'use strict';

const assert = require('node:assert/strict');
const { registerHooks } = require('node:module');
const test = require('node:test');
const { pathToFileURL } = require('node:url');

const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'electron') {
      return { url: pathToFileURL(require.resolve('./electron_stub')).href, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});
const { handleDrive, ensureWindow } = require('../app/browser_window');
hooks.deregister();

test('aborted navigation cannot report the old page and stop discards the window', async (t) => {
  const window = ensureWindow();
  window.getTitle = () => 'Previous page';
  window.webContents.getURL = () => 'https://example.com/';
  t.mock.method(window, 'loadURL', async () => { throw new Error('ERR_ABORTED'); });
  window.destroy = () => window.close();

  for (const op of ['open', 'navigate']) {
    const reply = await handleDrive({ op, url: 'https://example.org/' });
    assert.equal(reply.status, 'error');
    assert.equal(reply.url, undefined);
    assert.equal(reply.title, undefined);
  }
  assert.equal((await handleDrive({ op: 'stop' })).status, 'ok');
  assert.equal(window.isDestroyed(), true);
  assert.equal((await handleDrive({ op: 'state' })).status, 'unavailable');
  assert.notEqual(ensureWindow(), window);
});
