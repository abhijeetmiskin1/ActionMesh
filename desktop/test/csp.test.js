'use strict';
/**
 * P0-3 — the renderer page must stay locked down by its CSP meta.
 *
 * Read-only check of `desktop/ui/index.html` file content (no DOM, no
 * Electron): the Content-Security-Policy meta must pin
 *   default-src 'none', script-src 'self', connect-src 'none',
 *   form-action 'none'
 * and the page must contain no inline <script> without a src — every
 * script the page runs is a file shipped with the app, so `script-src
 * 'self'` is actually enforceable.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'ui', 'index.html'), 'utf8');

function cspDirectives(source) {
  const meta = source.match(
    /<meta[^>]*http-equiv=["']Content-Security-Policy["'][^>]*>/i,
  );
  assert.ok(meta, 'index.html has a Content-Security-Policy meta tag');
  // The content value itself contains single quotes ('none'), so a naive
  // ["']...["'] match would stop at the first one. Match the quote style
  // the attribute actually opens with.
  const content = meta[0].match(/\bcontent="([^"]*)"/i)
    || meta[0].match(/\bcontent='([^']*)'/i);
  assert.ok(content, 'the CSP meta carries a content attribute');
  const directives = new Map();
  for (const part of content[1].split(';')) {
    const tokens = part.trim().split(/\s+/).filter(Boolean);
    if (tokens.length > 0) directives.set(tokens[0], tokens.slice(1));
  }
  return directives;
}

test('CSP meta pins default-src, script-src, connect-src and form-action', () => {
  const directives = cspDirectives(html);
  assert.deepEqual(directives.get('default-src'), ["'none'"]);
  assert.deepEqual(directives.get('script-src'), ["'self'"]);
  assert.deepEqual(directives.get('connect-src'), ["'none'"]);
  assert.deepEqual(directives.get('form-action'), ["'none'"]);
});

test('index.html has no inline <script> without src', () => {
  const scriptTags = html.match(/<script\b[^>]*>/gi) || [];
  assert.ok(scriptTags.length > 0, 'the page loads its scripts by src');
  const inline = scriptTags.filter((tag) => !/\bsrc\s*=/.test(tag));
  assert.deepEqual(inline, [], `inline script tag(s) without src: ${JSON.stringify(inline)}`);
});
