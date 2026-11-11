'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ui = path.join(__dirname, '..', 'ui');
const html = fs.readFileSync(path.join(ui, 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(ui, 'app.js'), 'utf8');

test('Projects is a primary workspace with registration, real status, goals, and history controls', () => {
  assert.match(html, /data-view="projects"/);
  for (const id of [
    'projectRegisterForm', 'projectPath', 'projectList', 'projectStatus',
    'projectGoalForm', 'projectGoalList', 'projectGoalEvents',
    'projectAllEvents', 'projectOlderEvents',
    'projectPickFolder',
    'projectServiceForm', 'projectServiceName', 'projectServiceCommand',
    'projectServicePort', 'projectServiceTimeout', 'projectServiceList', 'projectGoalService', 'projectGoalRecovery',
    'projectGoalCaptureHash', 'projectGoalHashBaseline',
    'projectGoalProcessExpected', 'projectGoalProcessExpectedLabel',
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(app, /POST\('\/api\/v1\/projects'/);
  assert.match(app, /bridge\.pickProjectFolder\(\)/);
  assert.match(app, /GET\(`\/api\/v1\/projects\/\$\{encodeURIComponent\(active\.project_id\)\}\/status`\)/);
  assert.match(app, /POST\('\/api\/v1\/goals'/);
  assert.match(html, /value="file_sha256"/);
  assert.match(html, /value="managed_process"/);
  assert.match(app, /files\/hash/);
  assert.match(app, /Create process goal/);
  assert.match(app, /baseline_execution_id/);
  assert.match(app, /\/services`/);
  assert.match(app, /\/recover`/);
  assert.match(app, /\/observe`/);
  assert.match(app, /\/monitor`/);
  assert.match(app, /Disable monitoring/);
  assert.match(app, /\/services\/\$\{encodeURIComponent\(service\.service_id\)\}\/actions/);
  assert.match(app, /Stop managed process/);
  assert.match(app, /timeout_ms: Number\(byId\('projectServiceTimeout'\)\.value\)/);
  assert.match(app, /PATCH\(`\/api\/v1\/goals\/\$\{encodeURIComponent\(state\.editingGoal\)\}`/);
  assert.match(app, /GET\(`\/api\/v1\/goals\/\$\{encodeURIComponent\(goalId\)\}\/events/);
  assert.match(app, /GET\(`\/api\/v1\/events\?project_id=/);
  assert.match(app, /case 'projects': return refreshProjects\(\)/);
});

test('Project and goal records render as text, never executable markup', () => {
  assert.match(app, /name\.textContent = title/);
  assert.match(app, /detail\.textContent = subtitle/);
  assert.match(app, /byId\('projectStatus'\)\.textContent = projectStatusData\.verified/);
  assert.doesNotMatch(app, /project(?:Goal|Workspace)?[\s\S]{0,250}\.innerHTML\s*=/);
});
