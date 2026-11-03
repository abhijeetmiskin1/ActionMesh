'use strict';
/**
 * Settings and native credential storage.
 *
 * Two kinds of value live here and they are kept apart on purpose:
 *
 *  * preferences (port, device name, shortcut, allowed commands) — plain JSON;
 *  * secrets (an external server's API key, a model transport key) — encrypted with
 *    Electron's safeStorage, which on Linux is the Secret Service (libsecret) via
 *    the OS keyring. If safeStorage is unavailable the setting is refused, never
 *    written in clear text, because "it did not save" is a recoverable annoyance and
 *    "your key is in a plaintext config file" is not.
 *
 * The managed backend's own loopback key never reaches this file at all: it is
 * minted into memory at launch (see backend.js) so a copy of the profile directory
 * does not hand out control of a server that is no longer running.
 */
const fs = require('fs');
const path = require('path');

const SECRET_KEYS = ['externalApiKey', 'providerApiKey'];

const DEFAULTS = {
  backendMode: 'managed', // 'managed' | 'external'
  externalBaseUrl: '',
  agentEnabled: true,
  workspaceRoot: '',
  allowShell: false,
  // C38: a consent boundary of its own. Screen capture and accessibility inspection
  // are what a person means by "can see my screen", which is not what they mean by
  // "can run tests" — so one never turns on the other.
  allowDesktop: false,
  allowedCommands: ['python3', 'bash', 'sh'],
  deviceId: 'laptop-1',
  deviceName: 'This laptop',
  voiceTasksFile: '',
  shortcut: 'CommandOrControl+Shift+Space',
  shortcutEnabled: true,
  notificationsEnabled: true,
  providerName: '',
  providerEndpoint: '',
  providerModel: '',
  providerTimeout: 12,
  providerDefault: false,
  // Cached only so a choice survives a restart; never the key itself.
  lastActor: 'desktop-user',
};

class SettingsStore {
  constructor({ userDataDir, safeStorage }) {
    this.file = path.join(userDataDir, 'settings.json');
    this.safeStorage = safeStorage;
    this.values = { ...DEFAULTS };
    this.secrets = {}; // in-memory copy, decrypted
    this.secretStatus = {}; // key -> 'stored' | 'unavailable' | 'unreadable'
    this.load();
  }

  encryptionAvailable() {
    try {
      return Boolean(this.safeStorage && this.safeStorage.isEncryptionAvailable());
    } catch {
      return false;
    }
  }

  load() {
    let raw = {};
    try {
      raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch {
      raw = {};
    }
    for (const key of Object.keys(DEFAULTS)) {
      if (key in raw) this.values[key] = raw[key];
    }
    const sealed = raw.secrets && typeof raw.secrets === 'object' ? raw.secrets : {};
    for (const key of SECRET_KEYS) {
      if (!(key in sealed)) continue;
      try {
        const plain = this.safeStorage.decryptString(Buffer.from(sealed[key], 'base64'));
        this.secrets[key] = plain;
        this.secretStatus[key] = 'stored';
      } catch {
        this.secretStatus[key] = 'unreadable';
      }
    }
  }

  save() {
    const sealed = {};
    for (const key of SECRET_KEYS) {
      if (!this.secrets[key]) continue;
      sealed[key] = this.safeStorage.encryptString(this.secrets[key]).toString('base64');
    }
    const payload = { ...this.values, secrets: sealed };
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    // 0600: even the encrypted blob is nobody else's business.
    fs.writeFileSync(this.file, JSON.stringify(payload, null, 2), { mode: 0o600 });
  }

  get(key) {
    return key in DEFAULTS ? this.values[key] : undefined;
  }

  /** A secret, for main-process use only. Never sent to the renderer. */
  secret(key) {
    return this.secrets[key] || '';
  }

  hasSecret(key) {
    return Boolean(this.secrets[key]);
  }

  /**
   * Save a preference or a secret. Returns the settings the renderer may see —
   * which is every preference plus, per secret, whether one is held.
   */
  update(patch) {
    if (!patch || typeof patch !== 'object') return this.public();
    for (const [key, value] of Object.entries(patch)) {
      if (key in DEFAULTS) {
        this.values[key] = value;
      } else if (SECRET_KEYS.includes(key)) {
        if (value === '' || value === null) {
          delete this.secrets[key];
          this.secretStatus[key] = 'cleared';
        } else if (this.encryptionAvailable()) {
          this.secrets[key] = String(value);
          this.secretStatus[key] = 'stored';
        } else {
          // Refuse rather than persist. The renderer is told why.
          this.secretStatus[key] = 'unavailable';
        }
      }
    }
    this.save();
    return this.public();
  }

  /** What the UI is allowed to know: no key material, only presence. */
  public() {
    const secrets = {};
    for (const key of SECRET_KEYS) {
      secrets[key] = {
        present: Boolean(this.secrets[key]),
        status: this.secretStatus[key] || 'absent',
      };
    }
    return {
      ...this.values,
      secrets,
      encryptionAvailable: this.encryptionAvailable(),
      settingsFile: this.file,
      backendSnapshot: undefined,
    };
  }
}

module.exports = { SettingsStore, DEFAULTS };