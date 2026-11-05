// Classic-script shared-lexical-scope check.
// index.html loads these files WITHOUT type="module", so they all evaluate in
// ONE global lexical scope. A top-level const/let/function declared twice is a
// SyntaxError that stops the LATER file from loading at all.
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const UI = '/home/abhi/Documents/Action/desktop/ui';
const files = [
  'voice.js',
  'onboarding.js',
  'history.js',
  'activity.js',
  'workbench.js',
  'connections.js',
  'savedtasks.js',
  'onboarding_flow.js',
  'app.js',
];

// Minimal DOM stub. This check is about LEXICAL SCOPE — whether two classic
// scripts can coexist — not about rendering. app.js registers its boot hook at
// load time, so it needs a document to attach to and nothing more.
const noop = () => {};
const stubElement = () => ({
  addEventListener: noop,
  removeEventListener: noop,
  setAttribute: noop,
  getAttribute: () => null,
  appendChild: noop,
  removeChild: noop,
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  style: {},
  dataset: {},
  children: [],
  childNodes: [],
});
const documentStub = {
  addEventListener: noop,
  removeEventListener: noop,
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: stubElement,
  createTextNode: () => stubElement(),
  body: stubElement(),
  documentElement: stubElement(),
  readyState: 'loading',
};

const context = vm.createContext({
  window: {},
  document: documentStub,
  navigator: { clipboard: null, userAgent: 'scope-smoke' },
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  console,
  setTimeout: () => 0,
  clearTimeout: noop,
  setInterval: () => 0,
  clearInterval: noop,
  requestAnimationFrame: () => 0,
  URL: globalThis.URL,
  fetch: () => Promise.reject(new Error('n/a in smoke')),
});
context.globalThis = context;
context.window = context;

let failures = 0;
const results = [];
for (const file of files) {
  const full = path.join(UI, file);
  if (!fs.existsSync(full)) {
    results.push(`MISSING   ${file}`);
    failures += 1;
    continue;
  }
  const source = fs.readFileSync(full, 'utf8');
  try {
    new vm.Script(source, { filename: file }).runInContext(context);
    results.push(`EVAL OK   ${file}`);
    // Each file's public surface must be reachable from the shared global.
    const exported = Object.keys(context.window).filter((k) => /^(ActionMesh|actionmesh)/.test(k));
    if (exported.length) results.push(`          exports: ${exported.join(', ')}`);
  } catch (err) {
    failures += 1;
    results.push(`EVAL FAIL ${file} -> ${err.name}: ${err.message}`);
  }
}
console.log(results.join('\n'));
console.log(failures === 0 ? '\nALL SCRIPTS EVALUATE' : `\n${failures} SCRIPT(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);