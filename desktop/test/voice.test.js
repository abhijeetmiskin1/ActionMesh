'use strict';
/**
 * voice.js accessibility / honesty tests (Phase 2, Agent 5 — P2-3).
 *
 * No jsdom: reuses the repo's dom_harness shim. voice.js is an IIFE that
 * attaches to window.VoiceOSVoice, so it is loaded by evaluating its source
 * inside the harness window (same containment pattern as ui_render tests).
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const { install } = require('./dom_harness.js');

/** Load voice.js into a fresh harness window and return { page, api }. */
function loadVoice() {
  const page = install({});
  const win = page.window;
  const source = fs.readFileSync(path.join(__dirname, '..', 'ui', 'voice.js'), 'utf8');
  // voice.js reads window.SpeechRecognition / window.speechSynthesis at load;
  // the harness window has neither, so `supported`/`textToSpeechSupported`
  // are false — exactly the headless shape we want to test.
  const run = new Function('window', 'document', 'setTimeout', 'clearTimeout', `${source}\nreturn window.VoiceOSVoice;`);
  const api = run(win, page.document, win.setTimeout, win.clearTimeout);
  return { page, api };
}

// ---------------------------------------------------------------------------
// P2-3: live region for execution status
// ---------------------------------------------------------------------------

test('voice status region has aria-live (ensureLiveRegion)', () => {
  const { page, api } = loadVoice();
  assert.equal(typeof api.ensureLiveRegion, 'function', 'voice.js exposes ensureLiveRegion');
  const node = api.ensureLiveRegion(page.document);
  assert.ok(node, 'a live region node is returned');
  assert.equal(node.getAttribute('aria-live'), 'polite');
  assert.equal(node.getAttribute('aria-atomic'), 'true');
  assert.equal(node.getAttribute('role'), 'status');
  // It reuses the page's existing status element (#voiceState from
  // index.html, loaded by the harness) rather than inventing markup.
  assert.equal(node.id, 'voiceState');
});

test('announce() writes text into the live region', () => {
  const { page, api } = loadVoice();
  const node = api.announce(page.document, 'Listening');
  assert.ok(node);
  assert.equal(node.textContent, 'Listening');
  assert.equal(node.getAttribute('aria-live'), 'polite');
});

test('setState announces through the live region', () => {
  const { page, api } = loadVoice();
  api.setState('executing');
  const node = page.document.getElementById('voiceState');
  assert.ok(node, 'status element exists');
  assert.equal(node.getAttribute('aria-live'), 'polite');
  assert.equal(node.textContent, 'Executing');
});

test('voice module honours its no-fake-mic contract', () => {
  const { api } = loadVoice();
  // In the harness there is no SpeechRecognition, so it must say so.
  assert.equal(api.supported, false);
  assert.equal(api.startListening(), false);
  assert.equal(api.state, 'error');
});

// ---------------------------------------------------------------------------
// P2-3: buttons have accessible labels (page markup + onboarding buttons are
// covered in onboarding.test.js; here we assert the voice page controls)
// ---------------------------------------------------------------------------

test('primary voice buttons have text or aria-label (index.html)', () => {
  const { page } = loadVoice();
  const doc = page.document;
  // The harness tokeniser does not create text nodes for element content, so
  // check the served markup directly for the same buttons.
  const html = fs.readFileSync(path.join(__dirname, '..', 'ui', 'index.html'), 'utf8');
  for (const id of ['voiceSend', 'voiceStopListening', 'voiceStopSpeaking', 'voiceClear', 'voiceCancel']) {
    const btn = doc.getElementById(id);
    assert.ok(btn, `#${id} exists`);
    const match = html.match(new RegExp(`id="${id}"[^>]*>([^<]*)`));
    const text = match ? match[1].trim() : '';
    const label = (btn.getAttribute('aria-label') || text).trim();
    assert.ok(label.length > 0, `#${id} has text or aria-label`);
  }
  const mic = doc.getElementById('voiceMic');
  assert.ok(mic);
  assert.ok(((mic.getAttribute('aria-label') || '').trim().length > 0),
    'mic button has an accessible label');
});

// ---------------------------------------------------------------------------
// P2-3: no inline hard-coded colours in owned files (grep-level test)
// ---------------------------------------------------------------------------

test('owned UI files use no inline styles with hard-coded colours', () => {
  for (const file of ['voice.js', 'onboarding.js', 'history.js', 'about.js']) {
    const src = fs.readFileSync(path.join(__dirname, '..', 'ui', file), 'utf8');
    new Function(src); // parses
    // No element.style.* colour/background assignments, no style="...#hex..."
    assert.equal(/\.style\.(color|background|backgroundColor|borderColor)\s*=/.test(src), false,
      `${file} sets no inline colour`);
    assert.equal(/style\s*=\s*["'][^"']*(#[0-9a-fA-F]{3,8}|rgb)/.test(src), false,
      `${file} has no inline style attribute with a hard-coded colour`);
    assert.equal(/\.innerHTML\s*=/.test(src.replace(/^\s*\/\/.*$/gm, '')), false,
      `${file} assigns no innerHTML`);
  }
});

// ---------------------------------------------------------------------------
// Voice never pretends: no fake execution indicators in source
// ---------------------------------------------------------------------------

test('voice.js contains no simulated-success vocabulary', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'ui', 'voice.js'), 'utf8');
  // Strip comments: the header legitimately says "never pretends the
  // microphone works" — that is an honesty statement, not a fake indicator.
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const banned of ['simulated success', 'fake success', 'pretend']) {
    assert.equal(code.toLowerCase().includes(banned), false, `voice.js code does not contain "${banned}"`);
  }
});

test('live region reuses existing #voiceState node', () => {
  const { page, api } = loadVoice();
  const node = api.ensureLiveRegion(page.document);
  assert.equal(node.id, 'voiceState');
  assert.equal(node.getAttribute('aria-live'), 'polite');
  assert.equal(node.getAttribute('role'), 'status');
});
