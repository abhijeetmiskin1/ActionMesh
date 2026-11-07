'use strict';
/**
 * The only bridge between the shell and the page.
 *
 * The renderer gets a fixed set of named functions — no `ipcRenderer`, no `require`,
 * no Node globals — so a string that lands in the DOM has nothing to call except the
 * operations listed here. In particular there is no way to reach an arbitrary URL
 * through the API proxy: every call names a path and the main process checks it
 * against a fixed allowlist.
 */
const { contextBridge, ipcRenderer } = require('electron');

const subscribe = (channel) => (handler) => {
  if (typeof handler !== 'function') return () => {};
  const listener = (_event, payload) => handler(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
};

contextBridge.exposeInMainWorld('actionmesh', {
  // identity / environment
  state: () => ipcRenderer.invoke('app:state'),

  // settings and native credential storage
  updateSettings: (patch) => ipcRenderer.invoke('settings:update', patch),

  // AI provider — narrowly scoped. There is no `getKey`: a key can be set, tested,
  // and removed from here, and never read back into the page.
  providerSpecs: () => ipcRenderer.invoke('provider:specs'),
  providerStatus: () => ipcRenderer.invoke('provider:status'),
  providerConfigure: (patch) => ipcRenderer.invoke('provider:configure', patch),
  providerTest: (overrides) => ipcRenderer.invoke('provider:test', overrides),
  providerRemove: () => ipcRenderer.invoke('provider:remove'),
  providerSetModel: (model) => ipcRenderer.invoke('provider:model', model),

  // Per-purpose providers (C42): chat | reasoning | image | audio. Each purpose has
  // its own closed surface, so the renderer string that names purposes is validated
  // by the main process before it touches settings or secrets. Keys are still typed
  // in via IPC arguments, never returned, and stored only when encryption is available.
  generateImage: (payload) => ipcRenderer.invoke('image:generate', payload),
  analyzeObservation: (payload) => ipcRenderer.invoke('screen:analyze', payload),
  speechStatus: () => ipcRenderer.invoke('speech:status'),
  speechTranscribe: (payload) => ipcRenderer.invoke('speech:transcribe', payload),
  searchPublic: (query) => ipcRenderer.invoke('search:public', query),
  todoistConfigure: (token) => ipcRenderer.invoke('todoist:configure', token),
  todoistList: () => ipcRenderer.invoke('todoist:list'),
  todoistDisconnect: () => ipcRenderer.invoke('todoist:disconnect'),
  githubMcpConfigure: (token) => ipcRenderer.invoke('github-mcp:configure', token),
  githubMcpSearch: (query) => ipcRenderer.invoke('github-mcp:search', query),
  githubMcpDisconnect: () => ipcRenderer.invoke('github-mcp:disconnect'),
  purposeSpecs: (purpose) => ipcRenderer.invoke('purpose:specs', purpose),
  purposeStatus: (purpose) => ipcRenderer.invoke('purpose:status', purpose),
  purposeConfigure: (payload) => ipcRenderer.invoke('purpose:configure', payload),
  purposeTest: (payload) => ipcRenderer.invoke('purpose:test', payload),
  purposeRemove: (payload) => ipcRenderer.invoke('purpose:remove', payload),
  purposeSetModel: (payload) => ipcRenderer.invoke('purpose:model', payload),
  purposeSetDefault: (payload) => ipcRenderer.invoke('purpose:default', payload),

  // managed backend
  startBackend: () => ipcRenderer.invoke('backend:start'),
  stopBackend: () => ipcRenderer.invoke('backend:stop'),
  restartBackend: () => ipcRenderer.invoke('backend:restart'),
  backendLogs: () => ipcRenderer.invoke('backend:logs'),
  openDataDir: () => ipcRenderer.invoke('backend:open-data-dir'),

  // the control plane API, proxied through the main process
  request: (request) => ipcRenderer.invoke('api:request', request),

  // live events
  startStream: (scope) => ipcRenderer.invoke('stream:start', scope),
  stopStream: () => ipcRenderer.invoke('stream:stop'),
  onStreamEvent: subscribe('stream:event'),
  onStreamStatus: subscribe('stream:status'),
  onBackendChanged: subscribe('backend:changed'),
  onNavigate: subscribe('ui:navigate'),

  // native surfaces
  notify: (payload) => ipcRenderer.invoke('notify', payload),
  showAbout: () => ipcRenderer.invoke('window:about'),
  minimize: () => ipcRenderer.invoke('window:minimize'),
  toggleMaximize: () => ipcRenderer.invoke('window:toggle-maximize'),
  close: () => ipcRenderer.invoke('window:close'),
  // Controlled browser drive (C42) — the renderer's named-op surface onto the
  // hardened window. No `getKey`, no raw IPC, no way to reach a URL the main
  // process did not validate: every call names one of these fixed channels and
  // the main process checks its payload before it reaches the window.
  browserOpen: (payload) => ipcRenderer.invoke('browser:open', payload),
  browserNavigate: (payload) => ipcRenderer.invoke('browser:navigate', payload),
  browserSearch: (payload) => ipcRenderer.invoke('browser:search', payload),
  browserInspect: () => ipcRenderer.invoke('browser:inspect'),
  browserSnapshot: () => ipcRenderer.invoke('browser:snapshot'),
  browserClick: (payload) => ipcRenderer.invoke('browser:click', payload),
  browserType: (payload) => ipcRenderer.invoke('browser:type', payload),
  browserSelect: (payload) => ipcRenderer.invoke('browser:select', payload),
  browserScroll: (payload) => ipcRenderer.invoke('browser:scroll', payload),
  browserBack: () => ipcRenderer.invoke('browser:back'),
  browserForward: () => ipcRenderer.invoke('browser:forward'),
  browserRefresh: () => ipcRenderer.invoke('browser:refresh'),
  browserWait: (payload) => ipcRenderer.invoke('browser:wait', payload),
  browserStop: () => ipcRenderer.invoke('browser:stop'),
  browserState: () => ipcRenderer.invoke('browser:state'),
  browserPageText: (payload) => ipcRenderer.invoke('browser:page_text', payload),
  // Page observations flow shell -> renderer as bounded, display-only events.
  onBrowserEvent: subscribe('browser:event'),
  // The two browser pages the work order requires stay reachable, opened from here
  // so their session (and its header injection) is configured by the main process.
  openWebPage: (which) => ipcRenderer.invoke('window:open-web', which),
  clearWebSession: () => ipcRenderer.invoke('window:clear-web-session'),
  openExternal: (url) => ipcRenderer.invoke('external:open', url),
});
