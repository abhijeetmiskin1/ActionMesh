# C42 per-Purpose Providers - Design Document

## Overview

Extend the desktop app's single provider bar to support **separate providers for different purposes**: chat, reasoning, image generation, audio generation (TTS), and 9router "Both" (desktop + dashboard). Each capability gets its own configuration bar in the UI.

---

## Security Requirements (STRICT)

- **No plaintext secrets anywhere** — keys live only in OS keyring via Electron `safeStorage`
- **Never log, echo, or return API keys** — they must never appear in errors, responses, or logs
- **Separate secrets per purpose** — a chat key cannot be used for image/audio without explicit storage under that purpose
- **Main process isolation** — renderer sees ONLY `{present, status}` objects, never material keys
- **Closed endpoints** — VoiceOS transport still has closed `OFFICIAL_ENDPOINTS`; adding one requires security review
- **Encryption unavailable = refuse** — if `safeStorage.isEncryptionAvailable()` returns false, storing is refused, never written as plaintext

---

## Architecture

### Current State (One Provider)

```
Renderer → preload → main
   │            │         │
cfg fields  bridge      IPC handlers
           providerTest   providers.test()
                          settings.update()
                          safeStorage encrypt/decrypt
```

### Target State (Per-Purpose Providers)

```
Renderer → preload → main
   │            │         │
purpose bars  bridge     IPC handlers
   │            │         ├─ chat:* / reasoning:* / image:* / audio:*
   │            │         └─ settings.update({{chatApiKey}, {reasoningApiKey}, ...})
   │            │                   └─ safeStorage encrypt each secret independently
   │            │
┌─┴────────────┴──────────┐
│ CONFIGURATIONS          │
├─────────────────────────┤
│ Chat:    [provider] [...] │
│ Reasoning: [provider][...] │
│ Image:   [provider][...] │
│ Audio:   [provider][...] │
└─────────────────────────┘
```

---

## Implementation Plan

### Step 1: Settings Store Extension (`app/settings.js`)

**SECRET_KEYS** (extend):
```js
const SECRET_KEYS = [
  'externalApiKey', 
  'providerApiKey',  // existing
  'chatApiKey',       // NEW
  'reasoningApiKey',  // NEW
  'imageApiKey',      // NEW
  'audioApiKey'       // NEW
];
```

**DEFAULTS** (extend with purpose-specific prefs):
```js
// Purpose bars
{
  chatProvider: '',
  chatModel: '',
  chatEndpoint: '',
  chatTimeout: 12,
  chatDefault: false,
  
  reasoningProvider: '',
  reasoningModel: '',
  reasoningEndpoint: '',
  reasoningTimeout: 12,
  reasoningDefault: false,
  
  imageProvider: '',
  imageModel: '',
  imageEndpoint: '',
  imageTimeout: 12,
  
  audioProvider: '',
  audioModel: '',
  audioEndpoint: '',
  audioTimeout: 12,
}
```

**Behavior**: Follow EXACT pattern from current code:
- `update()`: secrets stored via `safeStorage.encryptString()`; if encryption unavailable → `'unavailable'` state, REFUSE write
- `public()`: returns `{...values, secrets:{k:{present, status}}, encryptionAvailable}`
- `secret(key)`: main-process only, never sent to renderer

---

### Step 2: Providers Adapter Extension (`app/providers.js`)

**Add purpose-aware test/generate functions**:

Current: `test(config)` and `generate(config, {purpose, system, user})` only support `plan|diagnose`.

New: Add generic adapters:
- `chat(config, {prompt})` → text completion
- `reasoning(config, {context})` → structured plan output
- `image(config, {prompt})` → image URL (if adapter exists)
- `audio(config, {text})` → TTS audio URL (if adapter exists)

**For now**: Most vendors will return `implemented:false` until adapters are built. Start with what exists (anthropic/openai/gemini) mapped to chat/reasoning.

---

### Step 3: Main Process IPC Handlers (`app/main.js`)

**Remove single `lastProviderTest`**; replace with purpose-scoped tracking:

```js
let lastChatTest = null;
let lastReasoningTest = null;
let lastImageTest = null;
let lastAudioTest = null;
```

**IPC handlers**:
- `purpose:test({purpose, config})` → run `providers.{purpose}Test(config)` → store test result → return status
- `purpose:configure({purpose, config})` → save settings + secrets (encrypted) → return updated status
- `purpose:remove({purpose})` → clear secret for that purpose → return updated status
- `purposeStatus({purpose})` → return status object

**Example handler**:
```js
ipcMain.handle('chat:configure', (_event, patch) => {
  const spec = providers.specFor(String(patch.provider || ''));
  if (!spec.implemented) return {ok: false, error: 'not implemented'};
  
  const update = {
    chatProvider: spec.id,
    chatModel: String(patch.model || ''),
    chatEndpoint: spec.requiresBaseUrl ? String(patch.baseUrl || '') : '',
  };
  const timeout = Number(patch.timeoutSeconds);
  if (Number.isFinite(timeout)) {
    update.chatTimeout = Math.min(Math.max(Math.round(timeout), 1), 60);
  }
  
  const key = typeof patch.apiKey === 'string' ? patch.apiKey.trim() : '';
  if (key) {
    if (!settings.encryptionAvailable()) {
      return {ok: false, error: 'The OS keyring is unavailable...'};
    }
    update.chatApiKey = key;
  }
  
  const answer = settings.update(update);
  return {ok: true, status: chatStatus()};
});
```

---

### Step 4: Renderer UI (`ui/index.html`, `ui/app.js`, `ui/styles.css`)

**HTML structure** (add four purpose bars):

```html
<!-- In header, replace single provider-bar with container -->
<div class="header-right">
  <!-- Existing chat provider bar -->
  <div class="provider-bar" id="chatProviderBar" data-purpose="chat">
    <label class="provider-field">
      <span class="provider-caption">CHAT</span>
      <select class="provider-select" id="chatProviderSelect"></select>
    </label>
    <label class="provider-field">
      <span class="provider-caption">MODEL</span>
      <select class="provider-select" id="chatModelSelect"></select>
    </label>
    <span class="provider-status" id="chatProviderStatus" data-state="not_configured">Not configured</span>
    <button class="btn ghost small" id="chatProviderConfigure">CONFIGURE</button>
  </div>
  
  <!-- Same pattern for reasoning, image, audio -->
</div>
```

**Modal dialogs**: One modal per purpose (or multi-tab modal):
```html
<div class="modal-backdrop" id="chatProviderModal" hidden>...</div>
<div class="modal-backdrop" id="reasoningProviderModal" hidden>...</div>
<div class="modal-backdrop" id="imageProviderModal" hidden>...</div>
<div class="modal-backdrop" id="audioProviderModal" hidden>...</div>
```

**JavaScript**: Generalize current wire functions:
```js
function renderPurposeBar(purpose, status) {
  const select = byId(`${purpose}ProviderSelect`);
  const modelSelect = byId(`${purpose}ModelSelect`);
  const chip = byId(`${purpose}ProviderStatus`);
  // Populate selects, show status
}

async function openPurposeModal(purpose) {
  const backdrop = byId(`${purpose}ProviderModal`);
  // Pre-fill with saved values
}

async function savePurposeConfig(purpose) {
  const payload = {
    provider: byId(`cfg${purpose.capitalize()}Provider`).value,
    model: byId(`cfg${purpose.capitalize()}Model`).value,
    apiKey: byId(`cfg${purpose.capitalize()}ApiKey`).value,
  };
  await bridge[purpose + ':configure'](payload);
}
```

---

### Step 5: Bridge Layer (`app/preload.js`)

Expose named calls per purpose:
```js
exposeInMainWorld('actionmesh', {
  // Existing
  providerSpecs: () => ipcRenderer.invoke('provider:specs'),
  providerStatus: () => ipcRenderer.invoke('provider:status'),
  
  // Per-purpose
  chatConfigure: (patch) => ipcRenderer.invoke('chat:configure'),
  chatTest: (config) => ipcRenderer.invoke('chat:test'),
  chatStatus: () => ipcRenderer.invoke('chat:status'),
  
  reasoningConfigure: (patch) => ipcRenderer.invoke('reasoning:configure'),
  reasoningTest: (config) => ipcRenderer.invoke('reasoning:test'),
  reasoningStatus: () => ipcRenderer.invoke('reasoning:status'),
  
  imageConfigure: (patch) => ipcRenderer.invoke('image:configure'),
  imageTest: (config) => ipcRenderer.invoke('image:test'),
  imageStatus: () => ipcRenderer.invoke('image:status'),
  
  audioConfigure: (patch) => ipcRenderer.invoke('audio:configure'),
  audioTest: (config) => ipcRenderer.invoke('audio:test'),
  audioStatus: () => ipcRenderer.invoke('audio:status'),
});
```

---

## 9router Dashboard Considerations

### What Exists Today

9router has `/dashboard/providers/new` and `/dashboard/media-providers/web` forms for adding providers. The backend already stores connections encrypted in `~/.9router/db/data.sqlite` (column `providerConnections.name` and `providerConnections.value`).

### What Doesn't Exist

- No image provider type (only TTS voices for media providers)
- No per-purpose routing UI (user picks a single provider per request)
- "Both" mode would need new routing logic in the desktop app AND optional guidance in 9router docs

### Action Items

1. **Desktop side**: Implement per-purpose bars with configurable providers
2. **9router side**: Add documentation pointing users to `/dashboard/providers/new` pages; do NOT try to modify compiled dashboard
3. **Route combos**: For each purpose, define which provider combinations work best (e.g., anthropic → chat/reasoning, openai-compat → image/TTS if available)

---

## Open Questions (Answer These Before Implementation)

1. **Which vendors for image/audio?** User said "i give api key so connect and add them" but no vendors listed yet. Options:
   - Image: OpenAI DALL-E, Stability AI, Replicate (Stable Diffusion)
   - Audio: ElevenLabs, Deepgram, Amazon Polly
   - **Decision needed**: Which to implement first?

2. **UI layout preference**: 
   - A) Four separate bars in header (horizontal space issue?)
   - B) Single dropdown selector showing all capabilities, one active at a time
   - C) Sidebar panel with four collapsible sections
   
3. **Migration from existing single provider**: When user upgrades to this version, their current `providerName/providerApiKey` should map to **which purpose**? Default to "chat" makes sense.

4. **"Both" meaning clarified**: User wants desktop app AND 9router dashboard to support the same providers. Does this mean:
   - Desktop handles everything (with optional sync to 9router)?
   - Or 9router is primary configuration source, desktop reads from it?
   - **Recommendation**: Desktop keeps local settings; 9router is separate. Document how to mirror configs manually.

---

## Testing Strategy

1. **Settings encryption tests** (unit):
   - Mock `safeStorage.isEncryptionAvailable()` → false → verify refusal
   - Mock `safeStorage.encryptString()` → success → verify secret stored in `this.secrets`
   - Mock decrypt → verify `public()` returns `{present: true, status: 'stored'}`

2. **End-to-end tests** (integration):
   - Configure chat provider with mock Anthropic key → verify IPC flow
   - Test connection succeeds/fails correctly
   - Verify removed key clears state → renderer shows `missing_key`
   - Restart app → verify loaded state matches saved state

3. **Security audit checklist**:
   - No plaintext secrets in JSON files (check `settings.json` permissions = 0600)
   - No keys returned in any RPC response → grep for `apiKey` in return values
   - CSP `connect-src 'none'` enforced → verify network blocked
   - Renderer never sees `settings.secrets['chatApiKey'].material`

---

## Next Steps

1. **Confirm vendor choices** for image/audio
2. **Confirm UI layout** (A/B/C options above)
3. **Write settings.js extension** (first step, lowest risk)
4. **Implement one purpose fully** (chat recommended, since adapters exist)
5. **Generalize to remaining purposes** with stub adapters where missing
6. **Run tests** before touching voiceos agent or other layers

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Encryption unavailable on some systems | Medium | High | Refuse clearly in UI; log why; do NOT fallback to plaintext |
| Keys leaked in errors/logs | Low | Critical | Code review all return paths; add static analysis rule |
| Security hole in `public()` returning material | Low | Critical | Only allow `{present, status}` format; code review |
| Breaking existing single-provider setup | Medium | Medium | Map old `providerName` → `chatProvider` on upgrade |
| Header too crowded with 4 bars | High | Low | Use dropdown or collapsible design |

---

## Timeline Estimate

- **Day 1**: Settings extension + one purpose (chat) end-to-end
- **Day 2**: Reasoning + tests + generalization scaffolding
- **Day 3**: Image/audio stub adapters + UI polish
- **Day 4**: End-to-end testing + security audit
- **Day 5**: Documentation + vendor integration (if user provides keys)

---

## Approval Required Before Proceeding

1. ✅ Vendor selection for image/audio
2. ✅ UI layout preference
3. ✅ Migration strategy for existing provider config
4. ✅ Confirmation of "Both" deployment interpretation

---

*This document supersedes earlier ad-hoc designs dated 2026-11-03.*
