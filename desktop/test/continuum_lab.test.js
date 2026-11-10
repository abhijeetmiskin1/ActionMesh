'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ui = path.join(__dirname, '..', 'ui');
const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(ui, 'app.js'), 'utf8');

test('Continuum Lab exposes the four live workspaces and retains GoalGuard', () => {
  for (const tab of ['mission', 'chaos', 'proof', 'intent', 'goalguard']) {
    assert.match(html, new RegExp(`data-lab-tab="${tab}"`));
    assert.match(html, new RegExp(`data-lab-panel="${tab}"`));
  }
  assert.match(html, /id="continuumForm"/);
  assert.match(html, /id="continuumStop"/);
});

test('Mission Control uses live ActionMesh records and cancellation route', () => {
  assert.match(app, /GET\('\/ready'\).*GET\('\/continuum'\).*GET\('\/approvals'\)/s);
  assert.match(app, /GET\('\/executions\?limit=30'\)/);
  assert.match(app, /POST\(`\/executions\/\$\{encodeURIComponent\(item\.execution_id\)\}\/cancel`\)/);
  assert.match(html, /Emergency Stop requests cancellation/);
});

test('ChaosStage and IntentCanvas remain on the existing Continuum action path', () => {
  assert.match(app, /labPost\('\/continuum\/stop-demo'\)/);
  assert.match(app, /labPost\('\/continuum\/check'\)/);
  assert.match(app, /POST\('\/continuum', \{ description:/);
  assert.match(app, /fixed local Continuum service/);
});

test('ProofLens labels the healthy demo claim as untrusted fixture evidence', () => {
  assert.match(app, /Controlled dashboard claim/);
  assert.match(app, /fixed demo fixture claim · not an observer/);
  assert.match(html, /are not counted as fully independent sources/);
});
