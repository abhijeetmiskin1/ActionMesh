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
  // The two browser pages the work order requires stay reachable, opened from here
  // so their session (and its header injection) is configured by the main process.
  openWebPage: (which) => ipcRenderer.invoke('window:open-web', which),
  clearWebSession: () => ipcRenderer.invoke('window:clear-web-session'),
  openExternal: (url) => ipcRenderer.invoke('external:open', url),
});