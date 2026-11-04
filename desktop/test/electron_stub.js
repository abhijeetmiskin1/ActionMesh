'use strict';
/**
 * A stand-in for the `electron` module, for testing the main process under plain node.
 *
 * `app/main.js` builds its IPC surface inside `whenReady`, so there is no exported
 * function to call: the only way to exercise the purpose handlers without a display
 * server is to load the real module with a fake `electron` and a fake `./backend`,
 * then invoke the handlers it registered. That is what this file enables — it is a
 * test fixture, never packaged (package.json's `files` excludes `test/`).
 *
 * It reproduces shapes, not behaviour: windows do not render, the tray does not
 * appear, and `Notification` never fires. The IPC handlers, the settings store, the
 * provider bridge and the purpose logic are all the real ones.
 */
const os = require('node:os');
const path = require('node:path');

const store = {
  handlers: new Map(),
  invoked: [],
  windows: [],
  notifications: [],
  opened: [],
  menus: [],
  safeStorageAvailable: true,
  lock: true,
};

const userData = process.env.ACTIONMESH_TEST_USERDATA || path.join(os.tmpdir(), 'actionmesh-electron-stub');

class FakeWebContents {
  constructor() {
    this.sent = [];
    this.handlers = {};
    // Real BrowserWindows expose the session on webContents (main.js sets its
    // permission-request handler through here); the stub does the same so the boot
    // path runs unchanged.
    this.session = sharedSession;
  }

  on(event, handler) { this.handlers[event] = handler; return this; }

  send(channel, payload) { this.sent.push({ channel, payload }); }

  setWindowOpenHandler(handler) { this.openHandler = handler; }
}

class FakeSession {
  constructor() {
    this.webRequest = { onBeforeSendHeaders() {} };
  }

  setPermissionRequestHandler(handler) { this.permissionHandler = handler; }

  async clearStorageData() {}
}

const sharedSession = new FakeSession();

class BrowserWindow {
  constructor(options = {}) {
    this.options = options;
    this.webContents = new FakeWebContents();
    this.session = sharedSession;
    this.handlers = {};
    this.shown = false;
    this.destroyed = false;
    this.visible = false;
    this.focused = false;
    this.maximized = false;
    this.bounds = { x: 0, y: 0, width: 1280, height: 860 };
    this.loaded = null;
    store.windows.push(this);
  }

  once(event, handler) { this.handlers[`once:${event}`] = handler; return this; }

  on(event, handler) { this.handlers[event] = handler; return this; }

  show() { this.shown = true; this.visible = true; }

  hide() { this.visible = false; }

  focus() { this.focused = true; }

  close() { this.destroyed = true; this.visible = false; const h = this.handlers.closed; if (h) h(); }

  maximize() { this.maximized = true; }

  minimize() { this.minimized = true; }

  restore() { this.minimized = false; }

  isMinimized() { return Boolean(this.minimized); }

  isMaximized() { return this.maximized; }

  isVisible() { return this.visible; }

  isFocused() { return this.focused; }

  isDestroyed() { return this.destroyed; }

  getNormalBounds() { return { ...this.bounds }; }

  loadFile(file) { this.loaded = { kind: 'file', file }; return Promise.resolve(); }

  loadURL(url) { this.loaded = { kind: 'url', url }; return Promise.resolve(); }

  static getAllWindows() { return store.windows.filter((win) => !win.destroyed); }
}

class Tray {
  constructor(image) {
    this.image = image;
    this.handlers = {};
    store.tray = this;
  }

  setToolTip() {}

  setContextMenu(menu) { this.menu = menu; }

  on(event, handler) { this.handlers[event] = handler; }
}

class Notification {
  constructor(options) {
    this.options = options;
    store.notifications.push(options);
  }

  static isSupported() { return true; }

  on() {}

  show() {}
}

const appState = {
  ready: null,
  displayedNotifications: [],
};

const app = {
  isPackaged: false,
  getPath(name) {
    if (name === 'userData') return userData;
    return path.join(userData, name);
  },
  setName() {},
  setAppUserModelId() {},
  requestSingleInstanceLock() { return store.lock; },
  whenReady() {
    if (!appState.ready) appState.ready = Promise.resolve();
    return appState.ready;
  },
  quit() { store.quitRequested = true; },
  on() {},
};

const ipcMain = {
  handle(channel, handler) {
    store.handlers.set(channel, handler);
  },
};

const safeStorage = {
  isEncryptionAvailable() { return store.safeStorageAvailable; },
  // Reversible and obviously fake: the test never depends on the cipher, only on the
  // fact that the plaintext is not what lands on disk.
  encryptString(text) { return Buffer.from(`sealed:${text}`, 'utf8'); },
  decryptString(buffer) { return buffer.toString('utf8').replace(/^sealed:/, ''); },
};

const Menu = {
  buildFromTemplate(template) { const menu = { template }; store.menus.push(menu); return menu; },
  setApplicationMenu(menu) { store.applicationMenu = menu; },
};

const globalShortcut = {
  register() { return true; },
  isRegistered() { return true; },
  unregisterAll() {},
};

const screen = {
  getAllDisplays() { return [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }]; },
};

const nativeImage = {
  createFromPath(file) {
    return { file, isEmpty: () => false, resize: () => ({ file, isEmpty: () => false }) };
  },
};

const shell = {
  openExternal(url) { store.opened.push({ kind: 'external', url }); return Promise.resolve(); },
  openPath(target) { store.opened.push({ kind: 'path', target }); return Promise.resolve(''); },
};

const sessionApi = {
  fromPartition() { return sharedSession; },
};

/** Invoke a registered IPC handler the way preload would. */
function invoke(channel, ...args) {
  const handler = store.handlers.get(channel);
  if (!handler) throw new Error(`no handler for ${channel}`);
  store.invoked.push(channel);
  return handler({}, ...args);
}

module.exports = {
  app,
  BrowserWindow,
  Menu,
  Tray,
  Notification,
  globalShortcut,
  screen,
  nativeImage,
  shell,
  session: sessionApi,
  safeStorage,
  ipcMain,
  __store: store,
  __invoke: invoke,
  __appState: appState,
};