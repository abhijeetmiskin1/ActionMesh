'use strict';
/**
 * First-run onboarding — renderer only.
 *
 * Two rules from the work order shape everything below:
 *
 *  1. **Nothing is granted silently.** This module never flips a consent,
 *     permission or provider setting on its own. Every change goes through the
 *     existing `updateSettings` / `purposeConfigure` bridge calls and only
 *     from an explicit user click. Rendering this screen changes nothing.
 *  2. **No credentials touch this file.** The module reads only
 *     `hasKey` / `encryptionAvailable` booleans and provider *states* (never a
 *     key), builds DOM with `createElement` + `textContent` only (never
 *     `innerHTML` — the lock `ui_render.test.js` asserts on `app.js`), and
 *     writes nothing to web storage. A typed key lives in a local variable
 *     for one IPC call and is cleared immediately.
 *
 * It uses only `window.actionmesh` (the single `contextBridge` in
 * `preload.js`, which is lead-owned and untouched). It cannot fetch — the
 * page's CSP is `connect-src 'none'` — and it cannot reach the OS. What it
 * *shows* is verified state from `app:state` / `purpose:status`, mapped to
 * READY / UNAVAILABLE / NEEDS_CONFIG per feature lane.
 *
 * The module is mount-independent: `renderOnboarding(container, bridge)`
 * builds its tree under `container`, so the lead can mount it in
 * `index.html` without this file touching lead-owned markup. Until the lead
 * wires the mount + `onboardingCompleted` DEFAULTS key (see
 * docs/ONBOARDING.md), calling it is inert — exactly the safe failure.
 */
(function registerOnboarding(global) {
  'use strict';

  /** The four provider lanes, in display order. Mirrors PURPOSES in main.js. */
  const PURPOSES = ['chat', 'reasoning', 'image', 'audio'];

  const PURPOSE_LABELS = { chat: 'Chat', reasoning: 'Reasoning', image: 'Image', audio: 'Audio' };

  /**
   * Ordered steps. `optional: true` means "Skip" is always available and the
   * step's state never blocks the final readiness summary.
   */
  const STEPS = [
    { id: 'welcome', label: 'Welcome', optional: false,
      blurb: 'ActionMesh proposes, policy decides, you control. Onboarding checks what is already true; it grants nothing.' },
    { id: 'service', label: 'Service', optional: false,
      blurb: 'The local control plane (backend) must be running.' },
    { id: 'device', label: 'Device', optional: false,
      blurb: 'This laptop is the device that executes approved actions.' },
    { id: 'permissions', label: 'Permissions', optional: false,
      blurb: 'OS boundaries stay off until you turn each one on. Nothing below is required to finish.' },
    { id: 'providers', label: 'AI providers', optional: true,
      blurb: 'Optional. Configure chat, reasoning, image generation and speech-to-text providers in AI Providers.' },
    { id: 'browser', label: 'Browser', optional: true,
      blurb: 'Optional. A hardened, controlled browser window you can already open from the shell.' },
    { id: 'media', label: 'Media', optional: true,
      blurb: 'Optional. Media control is its own consent boundary.' },
    { id: 'security', label: 'Security & consent', optional: false,
      blurb: 'How secrets and approvals are handled. Read-only; nothing changes here.' },
    { id: 'final', label: 'Readiness', optional: false,
      blurb: 'What is ready, what needs config, and what is unavailable — in words.' },
  ];

  /** The exact vocabulary the work order mandates. */
  const READY = 'READY';
  const UNAVAILABLE = 'UNAVAILABLE';
  const NEEDS_CONFIG = 'NEEDS_CONFIG';

  /**
   * Map a provider lane's verified state (providers.js STATE_TEXT vocabulary
   * as produced by main.js purposeStatus) to the onboarding vocabulary.
   * `connected` is the only READY. `provider_unavailable` from an
   * unimplemented adapter is UNAVAILABLE; everything else that merely lacks
   * configuration is NEEDS_CONFIG. A network-level `provider_unavailable`
   * on a text lane is likewise UNAVAILABLE (the vendor answered, or did not).
   */
  function readinessForProviderState(state, implemented) {
    // Unimplemented first: a lane with no real adapter+probe can never earn
    // READY, not even if a stale state somehow said `connected`.
    if (implemented === false) return UNAVAILABLE;
    const s = String(state || 'not_configured');
    if (s === 'connected') return READY;
    if (s === 'not_configured' || s === 'missing_key' || s === 'connecting') return NEEDS_CONFIG;
    if (
      s === 'invalid_credentials' || s === 'rate_limited'
      || s === 'provider_unavailable' || s === 'timeout'
    ) return UNAVAILABLE;
    return NEEDS_CONFIG;
  }

  /** Whether a lane spec table marks this purpose unimplemented-only. */
  function purposesImplemented(specsByPurpose) {
    const out = {};
    for (const purpose of PURPOSES) {
      const specs = (specsByPurpose && specsByPurpose[purpose]) || [];
      out[purpose] = specs.length > 0 && specs.every((spec) => !(spec && spec.implemented));
    }
    return out;
  }

  /**
   * Each check returns { status, detail } where status is one of
   * READY / UNAVAILABLE / NEEDS_CONFIG. `optional` steps report their real
   * status but are excluded from the blocking aggregate.
   */
  function checkService(appState) {
    const backend = (appState && appState.backend) || {};
    if (backend.status === 'ready') {
      return { status: READY, detail: 'Backend is ready.' };
    }
    if (backend.status === 'failed') {
      return { status: UNAVAILABLE, detail: String(backend.lastError || 'Backend failed to start.') };
    }
    return { status: NEEDS_CONFIG, detail: 'Backend is not running yet.' };
  }

  function checkDevice(appState) {
    const settings = (appState && appState.settings) || {};
    if (settings.deviceName) {
      return { status: READY, detail: `This laptop: ${String(settings.deviceName)}.` };
    }
    return { status: NEEDS_CONFIG, detail: 'No device name is set.' };
  }

  function checkPermissions(appState) {
    const settings = (appState && appState.settings) || {};
    const enabled = ['allowShell', 'allowDesktop', 'allowBrowser', 'allowMedia']
      .filter((key) => settings[key] === true);
    // Off is the correct default — permissions can never make onboarding
    // "not ready". READY here means "boundaries are safely defaulted".
    return {
      status: READY,
      detail: enabled.length
        ? 'Boundaries default off; you explicitly enabled: ' + enabled.join(', ') + '.'
        : 'All OS boundaries are off, as they should be by default.',
    };
  }

  function checkProviders(purposeStatusByPurpose, specsByPurpose) {
    const unimplemented = purposesImplemented(specsByPurpose);
    const lanes = {};
    for (const purpose of PURPOSES) {
      const status = (purposeStatusByPurpose && purposeStatusByPurpose[purpose]) || {};
      const implemented = !unimplemented[purpose];
      lanes[purpose] = {
        label: PURPOSE_LABELS[purpose],
        status: readinessForProviderState(status.state, implemented),
        detail: String(status.detail || status.text || 'Not configured'),
        providerLabel: String(status.providerLabel || ''),
        model: String(status.model || ''),
      };
    }
    // Overall providers step: READY only if at least one text lane is
    // READY; image/audio alone can never make it READY (unimplemented).
    const textReady = lanes.chat.status === READY || lanes.reasoning.status === READY;
    return {
      status: textReady ? READY : NEEDS_CONFIG,
      detail: textReady
        ? 'At least one text lane is connected.'
        : 'No text lane is connected; providers are optional and can be configured later.',
      lanes,
    };
  }

  function checkBrowser(appState, browserState) {
    const settings = (appState && appState.settings) || {};
    if (settings.allowBrowser !== true) {
      return { status: NEEDS_CONFIG, detail: 'Browser control is off. The shell browser window itself stays available.' };
    }
    if (browserState && browserState.status) {
      return { status: READY, detail: 'Browser control is on.' };
    }
    return { status: READY, detail: 'Browser control is on.' };
  }

  function checkMedia(appState) {
    const settings = (appState && appState.settings) || {};
    if (settings.allowMedia === true) {
      return { status: READY, detail: 'Media control is explicitly on.' };
    }
    return { status: NEEDS_CONFIG, detail: 'Media control is off, as it should be by default.' };
  }

  function checkSecurity(appState) {
    const settings = (appState && appState.settings) || {};
    if (settings.encryptionAvailable === false) {
      return {
        status: UNAVAILABLE,
        detail: 'The OS keyring is unavailable, so API keys cannot be stored. They will not be written in plain text.',
      };
    }
    return {
      status: READY,
      detail: 'Keys are stored only in the OS keyring; approvals stay per-action; content never crosses the result boundary.',
    };
  }

  /**
   * Final readiness: READY only when every non-optional step is READY.
   * Optional steps are reported, never blocking. UNAVAILABLE on any
   * required step makes the whole thing UNAVAILABLE; otherwise
   * NEEDS_CONFIG.
   */
  function aggregateReadiness(stepResults) {
    const results = stepResults || {};
    let hasUnavailable = false;
    let hasNeedsConfig = false;
    for (const step of STEPS) {
      if (step.optional || step.id === 'final') continue;
      const entry = results[step.id];
      if (!entry) continue;
      if (entry.status === UNAVAILABLE) hasUnavailable = true;
      else if (entry.status === NEEDS_CONFIG) hasNeedsConfig = true;
    }
    if (hasUnavailable) return UNAVAILABLE;
    if (hasNeedsConfig) return NEEDS_CONFIG;
    return READY;
  }

  function el(doc, tag, attrs, children) {
    const node = doc.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === undefined || value === null || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.append(
        child && child.nodeType === 1 ? child : doc.createTextNode(String(child)),
      );
    }
    return node;
  }

  function statusChip(doc, status) {
    const word = status === READY ? 'READY' : status === UNAVAILABLE ? 'UNAVAILABLE' : 'NEEDS CONFIG';
    return el(doc, 'span', {
      class: 'onboarding-state', dataset: { s: String(status || NEEDS_CONFIG) }, text: word,
    });
  }

  function collectChecks(appState, browserState) {
    const purposes = (appState && appState.purposes) || {};
    const specs = (appState && appState.specsByPurpose) || {};
    const providers = checkProviders(purposes, specs);
    const results = {
      welcome: { status: READY, detail: 'This wizard only reads verified state; it grants nothing.' },
      service: checkService(appState),
      device: checkDevice(appState),
      permissions: checkPermissions(appState),
      providers,
      browser: checkBrowser(appState, browserState),
      media: checkMedia(appState),
      security: checkSecurity(appState),
    };
    results.final = {
      status: aggregateReadiness(results),
      detail: 'Final readiness. Optional integrations never block this.',
    };
    return results;
  }

  /**
   * Render the wizard into `container` and return its controller. All DOM is
   * built via textContent; no innerHTML. No bridge call is made until the
   * user clicks an explicit control (Refresh / Test / consent toggle /
   * Finish / Skip).
   */
  function renderOnboarding(container, bridge, options) {
    const doc = (container && container.ownerDocument)
      || (global.document) || null;
    if (!doc || !container || typeof container.append !== 'function') {
      return null;
    }
    const api = bridge || (global.window && global.window.actionmesh) || {};
    const skipped = new Set();
    let current = 0;
    let appState = (options && options.initialState) || null;
    let browserState = null;

    const root = el(doc, 'section', { class: 'onboarding', id: 'onboardingRoot' });
    root.setAttribute('aria-label', 'First-run onboarding');
    root.setAttribute('role', 'region');

    const heading = el(doc, 'h2', { class: 'onboarding-title', text: 'Welcome to ActionMesh' });
    const intro = el(doc, 'p', {
      class: 'onboarding-intro',
      text: 'This checks what is already true on this machine. It grants nothing, '
        + 'stores no keys in plain text, and asks for no unnecessary permissions. '
        + 'Optional integrations can be skipped.',
    });
    root.append(heading, intro);

    const stepsNav = el(doc, 'ol', { class: 'onboarding-steps' });
    const panel = el(doc, 'div', { class: 'onboarding-panel' });
    const controls = el(doc, 'div', { class: 'onboarding-controls' });
    root.append(stepsNav, panel, controls);

    function refreshStepsNav(results) {
      while (stepsNav.firstChild) stepsNav.removeChild(stepsNav.firstChild);
      STEPS.forEach((step, index) => {
        const entry = results && results[step.id];
        const li = el(doc, 'li', {
          class: 'onboarding-step' + (index === current ? ' is-current' : ''),
          dataset: { step: step.id, state: entry ? entry.status : NEEDS_CONFIG },
        }, [
          el(doc, 'span', { class: 'onboarding-step-label', text: step.label }),
          statusChip(doc, entry ? entry.status : NEEDS_CONFIG),
        ]);
        li.addEventListener('click', () => {
          if (index <= current) { current = index; paint(results); }
        });
        stepsNav.append(li);
      });
    }

    function renderProviders(results) {
      const entry = results.providers || {};
      const lanes = entry.lanes || {};
      const list = el(doc, 'div', { class: 'onboarding-lanes' });
      for (const purpose of PURPOSES) {
        const lane = lanes[purpose] || {};
        list.append(el(doc, 'div', {
          class: 'onboarding-lane', dataset: { purpose, state: lane.status || NEEDS_CONFIG },
        }, [
          el(doc, 'span', { class: 'onboarding-lane-label', text: `${lane.label || purpose} — ` }),
          statusChip(doc, lane.status || NEEDS_CONFIG),
          el(doc, 'span', {
            class: 'onboarding-lane-detail',
            text: lane.providerLabel && lane.model
              ? ` ${lane.providerLabel} · ${lane.model}. ${lane.detail}`
              : ` ${lane.detail || ''}`,
          }),
        ]));
      }
      const note = el(doc, 'p', {
        class: 'onboarding-note',
        text: 'Connect each lane from AI Providers. Image and Audio support compatible 9router endpoints. '
          + '“Test Connection” verifies the configured model; an image test generates an image and may incur usage.',
      });
      const testBtn = el(doc, 'button', {
        class: 'btn ghost small', type: 'button', text: 'Test chat connection',
        'aria-label': 'Test chat connection',
      });
      testBtn.addEventListener('click', async () => {
        testBtn.disabled = true;
        try {
          if (typeof api.purposeTest === 'function') await api.purposeTest({ purpose: 'chat' });
          const refreshed = await reload();
          paint(refreshed);
        } catch { /* a failed test is reported by the lane state, not a crash */ }
        testBtn.disabled = false;
      });
      return el(doc, 'div', {}, [list, note, testBtn]);
    }

    function renderPermissions(results, appStateNow) {
      const settings = (appStateNow && appStateNow.settings) || {};
      const rows = [
        ['allowShell', 'Run commands (shell)'],
        ['allowDesktop', 'See the screen (desktop)'],
        ['allowBrowser', 'Controlled browser'],
        ['allowMedia', 'Media control'],
      ];
      const list = el(doc, 'ul', { class: 'onboarding-consents' });
      for (const [key, label] of rows) {
        const checkbox = doc.createElement('input');
        checkbox.setAttribute('type', 'checkbox');
        checkbox.checked = settings[key] === true;
        checkbox.setAttribute('data-consent', key);
        checkbox.setAttribute('aria-label', label);
        checkbox.addEventListener('change', async () => {
          // Only reached by an explicit user toggle. No other code path
          // in this module calls updateSettings with a consent key.
          if (typeof api.updateSettings === 'function') {
            await api.updateSettings({ [key]: Boolean(checkbox.checked) });
          }
        });
        list.append(el(doc, 'li', {}, [
          el(doc, 'label', {}, [checkbox, ` ${label}`]),
        ]));
      }
      const note = el(doc, 'p', {
        class: 'onboarding-note',
        text: 'Each boundary is independent: allowing shell never allows desktop, browser or media. '
          + 'A grant is not an approval — critical actions still need per-action approval.',
      });
      return el(doc, 'div', {}, [list, note]);
    }

    async function reload() {
      try {
        if (typeof api.state === 'function') {
          appState = await api.state();
        }
      } catch { /* offline: keep last known state, show NEEDS_CONFIG */ }
      try {
        if (typeof api.browserState === 'function') {
          const reply = await api.browserState();
          browserState = (reply && reply.state) || reply || null;
        }
      } catch { browserState = null; }
      return collectChecks(appState, browserState);
    }

    function paint(results) {
      while (panel.firstChild) panel.removeChild(panel.firstChild);
      while (controls.firstChild) controls.removeChild(controls.firstChild);
      const step = STEPS[current];
      const entry = (results && results[step.id]) || { status: NEEDS_CONFIG, detail: '' };
      refreshStepsNav(results);

      panel.append(el(doc, 'h3', { class: 'onboarding-panel-title', text: step.label }));
      // P2-3: announce step changes to assistive technology.
      panel.setAttribute('role', 'status');
      panel.setAttribute('aria-live', 'polite');
      panel.setAttribute('aria-atomic', 'false');
      if (step.blurb) panel.append(el(doc, 'p', { class: 'onboarding-blurb', text: step.blurb }));
      if (step.id === 'providers') {
        panel.append(renderProviders(results));
      } else if (step.id === 'permissions') {
        panel.append(renderPermissions(results, appState));
      } else if (entry) {
        panel.append(el(doc, 'p', { class: 'onboarding-detail' }, [
          statusChip(doc, entry.status),
          ` ${entry.detail || ''}`,
        ]));
        if (step.id === 'security') {
          panel.append(el(doc, 'p', {
            class: 'onboarding-note',
            text: 'Credentials live in the OS keyring only. The renderer never sees a key, '
              + 'and the backend log and audit chain carry no secrets.',
          }));
        }
      }

      const back = el(doc, 'button', {
        class: 'btn ghost small', type: 'button', text: 'Back',
        disabled: current === 0, 'aria-label': 'Back',
      });
      back.addEventListener('click', () => { if (current > 0) { current -= 1; paint(results); } });
      const next = el(doc, 'button', {
        class: 'btn small', type: 'button',
        text: current === STEPS.length - 1 ? 'Finish' : 'Next',
        'aria-label': current === STEPS.length - 1 ? 'Finish onboarding' : 'Next step',
      });
      next.addEventListener('click', async () => {
        if (current < STEPS.length - 1) { current += 1; paint(results); return; }
        if (typeof api.updateSettings === 'function') {
          await api.updateSettings({ onboardingCompleted: true });
        }
        root.setAttribute('hidden', '');
        if (container && typeof container.setAttribute === 'function') {
          container.setAttribute('hidden', '');
        }
      });
      const skip = step.optional
        ? el(doc, 'button', { class: 'btn ghost small', type: 'button', text: `Skip ${step.label}`, 'aria-label': `Skip ${step.label} step` })
        : null;
      if (skip) {
        skip.addEventListener('click', () => {
          skipped.add(step.id);
          if (current < STEPS.length - 1) { current += 1; paint(results); }
        });
      }
      const refresh = el(doc, 'button', {
        class: 'btn ghost small', type: 'button', text: 'Refresh status',
        'aria-label': 'Refresh status',
      });
      refresh.addEventListener('click', async () => { paint(await reload()); });

      controls.append(back, refresh);
      if (skip) controls.append(skip);
      controls.append(next);
      return entry;
    }

    container.append(root);

    const controller = {
      element: root,
      currentStep() { return STEPS[current].id; },
      skippedSteps() { return [...skipped]; },
      skip(stepId) {
        const step = STEPS.find((s) => s.id === stepId);
        if (!step || !step.optional) return false;
        skipped.add(stepId);
        return true;
      },
      async refresh() { return paint(await reload()); },
      async complete() {
        if (typeof api.updateSettings === 'function') {
          await api.updateSettings({ onboardingCompleted: true });
        }
        // Hide BOTH: the inner section (root) and the caller-owned mount
        // container. app.js sets container.hidden = false to show the fixed
        // overlay; hiding only root would leave an empty overlay covering
        // the screen with its translucent background.
        root.setAttribute('hidden', '');
        if (container && typeof container.setAttribute === 'function') {
          container.setAttribute('hidden', '');
        }
      },
    };

    // Initial paint is synchronous from whatever state we already have; a
    // refresh is offered explicitly (Refresh status) rather than fired on
    // mount, so mounting this module performs no bridge I/O.
    paint(collectChecks(appState, browserState));
    return controller;
  }

  global.ActionMeshOnboarding = {
    STEPS,
    PURPOSES,
    PURPOSES_LIST: PURPOSES,
    READY,
    UNAVAILABLE,
    NEEDS_CONFIG,
    readinessForProviderState,
    purposesImplemented,
    checkService,
    checkDevice,
    checkPermissions,
    checkProviders,
    checkBrowser,
    checkMedia,
    checkSecurity,
    aggregateReadiness,
    collectChecks,
    renderOnboarding,
  };
})(typeof window !== 'undefined' ? window : globalThis);

if (typeof module !== 'undefined' && module.exports) {
  module.exports = (typeof window !== 'undefined' && window.ActionMeshOnboarding)
    || globalThis.ActionMeshOnboarding;
}
