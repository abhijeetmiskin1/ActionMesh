'use strict';
/**
 * A browser-global shim, enough to *boot* `ui/app.js` under plain node.
 *
 * The renderer is where the four lanes actually appear, so the two things worth
 * testing about it cannot be tested by reading it: that it starts without throwing,
 * and that every control it wires points at an element that exists. Neither needs a
 * layout engine — both need `document`, `window` and an event loop, which is all this
 * file provides.
 *
 * It is not a DOM. There is no layout, no styles, no rendering; `append`, `textContent`
 * and `dataset` behave, and `querySelector` understands only the simple selectors this
 * page's own code uses (class, attribute, and a descendant of those).
 */
const fs = require('node:fs');
const path = require('node:path');

const UI_DIR = path.resolve(__dirname, '..', 'ui');

class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }

  add(...names) { for (const n of names) this.set.add(n); this.sync(); }

  remove(...names) { for (const n of names) this.set.delete(n); this.sync(); }

  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    this.sync();
    return on;
  }

  contains(name) { return this.set.has(name); }

  sync() { this.node._class = [...this.set].join(' '); }
}

/** The page tests `child instanceof Node`, so there has to be a `Node` to test against. */
class Node {}

/** `data-purpose` ←→ `dataset.purpose`, the mapping the real element does in both directions. */
function kebab(name) { return String(name).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`); }

class FakeNode extends Node {
  constructor(doc, tag) {
    super();
    this.ownerDocument = doc;
    this.tagName = String(tag || '').toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    // `dataset` writes through to `data-*` attributes. In a browser they are one store, and
    // the page reads them both ways: it builds a lane button with `dataset` and the page's
    // own selectors find it with `[data-purpose="..."]`. Reads fall back to `attributes`,
    // so a `data-*` written by the HTML tokeniser is visible as `dataset` too.
    this._dataset = new Proxy({}, {
      get: (target, key) => {
        if (typeof key !== 'string') return target[key];
        return Object.prototype.hasOwnProperty.call(target, key)
          ? target[key]
          : this.attributes[`data-${kebab(key)}`];
      },
      set: (target, key, value) => {
        target[key] = String(value);
        this.attributes[`data-${kebab(key)}`] = String(value);
        return true;
      },
      deleteProperty: (target, key) => {
        delete target[key];
        delete this.attributes[`data-${kebab(key)}`];
        return true;
      },
      has: (target, key) => typeof key === 'string'
        && (Object.prototype.hasOwnProperty.call(target, key)
          || Object.prototype.hasOwnProperty.call(this.attributes, `data-${kebab(key)}`)),
      ownKeys: (target) => [...new Set([
        ...Object.keys(target),
        ...Object.keys(this.attributes)
          .filter((name) => name.startsWith('data-'))
          .map((name) => name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())),
      ])],
      getOwnPropertyDescriptor: (target, key) => {
        if (typeof key !== 'string') return Object.getOwnPropertyDescriptor(target, key);
        const value = Object.prototype.hasOwnProperty.call(target, key)
          ? target[key]
          : this.attributes[`data-${kebab(key)}`];
        return value === undefined ? undefined : { value, enumerable: true, configurable: true, writable: true };
      },
    });
    this.listeners = {};
    this.style = {};
    this.classList = new ClassList(this);
    this._class = '';
    this._text = '';
    this._value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.type = '';
    this.placeholder = '';
    this.id = '';
  }

  get nodeType() { return this.tagName === '#TEXT' ? 3 : 1; }

  /** The write-through view set up in the constructor; assigning replaces it wholesale. */
  get dataset() { return this._dataset; }
  set dataset(value) {
    for (const key of Object.keys(value || {})) this._dataset[key] = value[key];
  }

  get firstChild() { return this.childNodes[0] || null; }

  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }

  get className() { return this._class; }

  set className(value) {
    this._class = String(value || '');
    this.classList.set = new Set(this._class.split(/\s+/).filter(Boolean));
  }

  get textContent() {
    if (this.tagName === '#TEXT') return this._text;
    return this.childNodes.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    if (this.tagName === '#TEXT') { this._text = String(value); return; }
    // `textContent = ''` empties; anything else replaces the children with one text node.
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    if (String(value) !== '') this.append(this.ownerDocument.createTextNode(String(value)));
  }

  get innerHTML() { return ''; }

  set innerHTML(value) {
    // The renderer promises never to use this. Recording it is what lets a test fail if
    // that promise is broken later.
    this.ownerDocument.__innerHTMLWrites.push(value);
  }

  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node;
      if (!child) continue;
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }

  appendChild(node) { this.append(node); return node; }

  prepend(...nodes) {
    const added = nodes.map((node) => (typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node));
    for (const node of added) { node.parentNode = this; }
    this.childNodes.unshift(...added);
  }

  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  contains(node) {
    let current = node;
    while (current) {
      if (current === this) return true;
      current = current.parentNode;
    }
    return false;
  }

  /** `parent.after(node)` — the renderer uses it to place an inline reply box. */
  after(...nodes) {
    if (!this.parentNode) return;
    const at = this.parentNode.childNodes.indexOf(this);
    for (const node of nodes) {
      const child = typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node;
      child.parentNode = this.parentNode;
      this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this) + 1, 0, child);
    }
    void at;
  }

  before(...nodes) {
    if (!this.parentNode) return;
    for (const node of nodes) {
      const child = typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node;
      child.parentNode = this.parentNode;
      this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 0, child);
    }
  }

  insertBefore(node, reference) {
    const at = reference ? this.childNodes.indexOf(reference) : -1;
    node.parentNode = this;
    if (at < 0) this.childNodes.push(node); else this.childNodes.splice(at, 0, node);
    return node;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'id') { this.id = String(value); this.ownerDocument.__register(this); }
    if (name === 'class') this.className = value;
    if (name === 'hidden') this.hidden = true;
    if (name === 'disabled') this.disabled = true;
  }

  /** `<option value="x">` — the page sets `.value` on options, which here is the attribute. */
  get value() {
    return Object.prototype.hasOwnProperty.call(this.attributes, 'value')
      ? this.attributes.value
      : (this.tagName === 'OPTION' ? this.textContent : this._value);
  }

  set value(next) {
    if (this.tagName === 'OPTION') this.attributes.value = String(next);
    else this._value = String(next);
  }

  /** `<select>` children, the way the renderer reads them when it rebuilds a list. */
  get options() { return this.children.filter((child) => child.tagName === 'OPTION'); }

  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }

  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }

  removeAttribute(name) { delete this.attributes[name]; }

  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }

  removeEventListener(type, handler) {
    const list = this.listeners[type] || [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  /** Fire a listener the way a click would. */
  click() { this.dispatch('click', {}); }

  dispatch(type, event) {
    // Real events bubble, and this page leans on that: the lane cards are re-rendered on
    // every status change, so their buttons are delegated to the container's listener.
    // A non-bubbling dispatch would make every delegated control look dead.
    let stopped = false;
    const payload = {
      type,
      target: this,
      currentTarget: this,
      preventDefault() {},
      stopPropagation() { stopped = true; },
      ...(event || {}),
    };
    for (let node = this; node && !stopped; node = node.parentNode) {
      payload.currentTarget = node;
      for (const handler of [...(node.listeners[type] || [])]) handler(payload);
    }
    return payload;
  }

  focus() { this.ownerDocument.activeElement = this; }

  blur() {}

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  /**
   * Match one compound simple selector — a tag, `#id`, `.class` and `[attr]`/`[attr=v]`
   * in any order, as in `.provider-bar[data-purpose="chat"]`. Anything else (a
   * combinator, a pseudo-class) is rejected rather than silently mis-matched: a test
   * built on a selector this shim does not understand would be checking nothing.
   */
  matches(selector) {
    if (!selector) return false;
    const trimmed = String(selector).trim();
    if (!trimmed) return false;
    const part = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([~^$*|]?=)["']?([^"'\]]*)["']?)?\]/g;
    let consumed = 0;
    let match;
    while ((match = part.exec(trimmed)) !== null) {
      if (match.index !== consumed) return false;
      consumed = part.lastIndex;
      const [, tagName, id, cls, attr, operator, attrValue] = match;
      if (tagName && this.tagName !== tagName.toUpperCase()) return false;
      if (id && this.id !== id) return false;
      if (cls && !this.classList.contains(cls)) return false;
      if (attr) {
        if (!this.hasAttribute(attr)) return false;
        if (operator === '=' && this.getAttribute(attr) !== attrValue) return false;
        if (operator === '~=' && !String(this.getAttribute(attr) || '').split(/\s+/).includes(attrValue)) return false;
      }
    }
    return consumed > 0 && consumed === trimmed.length;
  }

  /** Depth-first walk, so tests can find a node by its text. */
  *walk() {
    yield this;
    for (const child of this.childNodes) {
      if (child.nodeType === 1) yield* child.walk();
    }
  }

  querySelectorAll(selector) {
    // Split on the descendant combinator, but not on a space inside an attribute value.
    const parts = [];
    let depth = 0;
    let current = '';
    for (const ch of String(selector).trim()) {
      if (ch === '[') depth += 1;
      if (ch === ']') depth -= 1;
      if (depth === 0 && /\s/.test(ch)) {
        if (current) parts.push(current);
        current = '';
      } else {
        current += ch;
      }
    }
    if (current) parts.push(current);
    const last = parts[parts.length - 1];
    return [...this.walk()].filter((node) => node !== this && node.matches(last)
      // A descendant selector is only the case this page uses: ".modal input".
      && (parts.length === 1 || (() => {
        let parent = node.parentNode;
        const ancestors = parts.slice(0, -1);
        let index = ancestors.length - 1;
        while (parent && index >= 0) {
          if (parent.matches(ancestors[index])) index -= 1;
          parent = parent.parentNode;
        }
        return index < 0;
      })()));
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 }; }

  scrollIntoView() {}

  getElementsByTagName(tag) { return this.querySelectorAll(tag); }
}

class FakeDocument extends FakeNode {
  constructor() {
    super(null, '#document');
    this.ownerDocument = this;
    this.nodes = new Map();
    this.__innerHTMLWrites = [];
    this.__listenerCount = 0;
    this.hidden = false;
    this.readyState = 'complete';
    this.activeElement = null;
  }

  __register(node) {
    if (node.id) this.nodes.set(node.id, node);
  }

  createElement(tag) {
    const node = new FakeNode(this, tag);
    return node;
  }

  createTextNode(text) {
    const node = new FakeNode(this, '#text');
    node._text = String(text);
    return node;
  }

  getElementById(id) { return this.nodes.get(String(id)) || null; }

  querySelectorAll(selector) { return FakeNode.prototype.querySelectorAll.call(this, selector); }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
    this.__listenerCount += 1;
  }

  dispatch(type, event) { return FakeNode.prototype.dispatch.call(this, type, event); }

  get body() { return this.__body || (this.__body = this.createElement('body')); }

  get documentElement() { return this.__root || (this.__root = this.createElement('html')); }

  /**
   * Build a tree from index.html.
   *
   * Not a parser — a tokeniser. The page queries things by where they sit (the header's
   * selects live *inside* `.provider-bar`, a lane button's purpose is read off the modal
   * ancestor), so a flat list of ids cannot express what the renderer asks. This keeps
   * the element stack and the attributes that matter: id, class, data-*, hidden,
   * disabled, value, and the few form properties the page assigns. `<script>` and
   * `<style>` bodies are skipped, and void elements never open a level.
   */
  loadIndexHtml() {
    const html = fs.readFileSync(path.join(UI_DIR, 'index.html'), 'utf8')
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, '');
    this.__body = this.createElement('body');
    this.__body.parentNode = this;
    this.childNodes.push(this.__body);

    const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
      'link', 'meta', 'param', 'source', 'track', 'wbr']);
    const stack = [this.__body];
    const token = /<(\/?)([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>/g;
    let match;
    while ((match = token.exec(html)) !== null) {
      const [, closing, rawTag, rawAttrs, selfClosing] = match;
      const tag = rawTag.toLowerCase();
      if (closing) {
        // Pop to the matching open tag; an unmatched close is ignored, as a browser does.
        for (let depth = stack.length - 1; depth > 0; depth -= 1) {
          if (stack[depth].tagName === tag.toUpperCase()) { stack.length = depth; break; }
        }
        continue;
      }
      const node = this.createElement(tag);
      for (const attr of rawAttrs.matchAll(/([a-zA-Z_:][\w:.-]*)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?/g)) {
        const name = attr[1].toLowerCase();
        const value = attr[2] === undefined ? '' : attr[2].replace(/^["']|["']$/g, '');
        // `dataset` reads back from `attributes`, so a data-* attribute is enough here.
        node.setAttribute(name, value);
      }
      stack[stack.length - 1].append(node);
      if (!VOID.has(tag) && selfClosing !== '/') stack.push(node);
    }
    return this;
  }
}

/**
 * Install the globals `ui/app.js` expects and return the document plus a record of
 * every bridge call the page makes.
 */
function install({ bridge } = {}) {
  const document = new FakeDocument().loadIndexHtml();
  const calls = [];
  const actionmesh = {};

  // A bridge that records instead of answering. Each method returns `undefined` unless
  // the caller supplied one, which is the shape a page must tolerate anyway.
  const record = (name) => (...args) => {
    calls.push({ name, args });
    const handler = bridge && bridge[name];
    return handler ? handler(...args) : Promise.resolve(undefined);
  };
  for (const name of [
    'state', 'updateSettings', 'providerSpecs', 'providerStatus', 'providerConfigure',
    'providerTest', 'providerRemove', 'providerSetModel', 'purposeSpecs', 'purposeStatus',
    'purposeConfigure', 'purposeTest', 'purposeRemove', 'purposeSetModel', 'purposeSetDefault',
    'startBackend', 'stopBackend', 'restartBackend', 'backendLogs', 'openDataDir', 'request',
    'startStream', 'stopStream', 'onStreamEvent', 'onStreamStatus', 'onBackendChanged',
    'onNavigate', 'notify', 'showAbout', 'minimize', 'toggleMaximize', 'close',
    'openWebPage', 'clearWebSession', 'openExternal',
    'browserOpen', 'browserNavigate', 'browserSearch', 'browserInspect', 'browserSnapshot',
    'browserClick', 'browserType', 'browserSelect', 'browserScroll', 'browserBack',
    'browserForward', 'browserRefresh', 'browserWait', 'browserStop', 'browserState',
    'browserPageText', 'onBrowserEvent',
  ]) {
    actionmesh[name] = record(name);
  }
  actionmesh.onBrowserEvent = () => () => {};
  actionmesh.onStreamEvent = () => () => {};
  actionmesh.onStreamStatus = () => () => {};
  actionmesh.onBackendChanged = () => () => {};
  actionmesh.onNavigate = () => () => {};

  const timers = [];
  const win = {
    actionmesh,
    document,
    navigator: { platform: 'Linux x86_64', userAgent: 'node-harness', clipboard: { writeText: async () => {} } },
    location: { href: 'file:///index.html' },
    URL: { createObjectURL: () => 'blob:fake', revokeObjectURL() {} },
    setTimeout: (fn, ms, ...rest) => {
      // Long polls are recorded, not scheduled: a boot test must not leave a 5-second
      // interval behind that keeps the process alive.
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout() {},
    setInterval: (fn, ms) => { timers.push({ fn, ms, repeat: true }); return timers.length; },
    clearInterval() {},
    confirm: () => false,
    alert: () => {},
    addEventListener(type, handler) { document.addEventListener(type, handler); },
    removeEventListener() {},
    devicePixelRatio: 1,
    innerWidth: 1440,
    innerHeight: 900,
    fetch: async () => { throw new Error('the renderer must not fetch'); },
  };
  // The renderer's first statement is `const bridge = window.actionmesh`, and inside the
  // evaluator below `window` is resolved from this object — so it has to be its own
  // global, exactly as it is in a page.
  win.window = win;
  win.self = win;
  // `CSS.escape` appears once, in a selector built from a task id. Node has no CSS global,
  // so the shim supplies the same escaping the browser would: every character that is not
  // a letter, digit, hyphen or underscore is backslash-escaped, and a leading digit is
  // escaped as a hex code point, which is what the CSSOM says to do.
  win.CSS = {
    escape(value) {
      const text = String(value);
      let out = '';
      for (let index = 0; index < text.length; index += 1) {
        const ch = text[index];
        const code = ch.codePointAt(0);
        if (/[a-zA-Z0-9_-]/.test(ch)) {
          out += (index === 0 && /[0-9]/.test(ch)) ? `\\${code.toString(16)} ` : ch;
        } else if (code < 0x20 || code === 0x7f) {
          out += `\\${code.toString(16)} `;
        } else {
          out += `\\${ch}`;
        }
      }
      return out;
    },
  };
  win.Node = Node;

  return {
    document,
    window: win,
    calls,
    timers,
    /** The furthest timer, so a test can run a poll without a real clock. */
    runTimers() {
      for (const timer of [...timers]) timer.fn();
    },
    /**
     * Evaluate ui/app.js with these globals in scope.
     *
     * The module reads its globals at load (`const bridge = window.actionmesh`), so this
     * has to be loaded after the harness exists, not required at the top of the file.
     * `new Function` is containment, not a security boundary: the source is this
     * repository's own renderer, read from disk by the test, and never user input.
     */
    load() {
      const source = fs.readFileSync(path.join(UI_DIR, 'app.js'), 'utf8');
      const names = ['window', 'document', 'navigator', 'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'confirm', 'alert', 'URL', 'fetch', 'Node', 'CSS'];
      const values = names.map((name) => (name in win ? win[name] : globalThis[name]));
      // eslint-disable-next-line no-new-func
      const run = new Function(...names, `${source}\nreturn { boot };`);
      return run(...values);
    },
  };
}

module.exports = { install, FakeDocument, FakeNode };