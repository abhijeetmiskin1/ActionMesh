'use strict';
/**
 * Guided first-run flow (Phase 6 Agent 5) — six steps over the REAL UI.
 *
 *   1. welcome      — what ActionMesh is, what this walk will not do
 *   2. first-task   — type a first safe instruction; a real draft is read
 *   3. see-plan     — review the plan the planner actually proposed
 *   4. approve-run  — save it and run it through the pipeline; an approval
 *                     wait or a denial is shown as exactly that
 *   5. history      — open the real history/run surface for the result
 *   6. done         — finish (onboardingCompleted, the existing key)
 *
 * This is a companion to `onboarding.js` (the readiness wizard), not a
 * replacement: it reuses that module's persistence contract — the only
 * write ever made is `updateSettings({ onboardingCompleted: true })`,
 * and only from an explicit Finish/complete() click. It never grants
 * anything, never touches a consent/API key, never writes web storage,
 * and never uses innerHTML. Skippable from every step; resumable because
 * it starts from the welcome step every time the mount is opened and
 * complete() is idempotent.
 *
 * Every step drives a real surface — no dead buttons:
 *   - Drafting uses the bridge `request` POST /automations/draft (the
 *     same route app.js uses), so step 3 shows the planner's proposal,
 *     not a mock.
 *   - Saving uses POST /automations with `draft.proposal`; running uses
 *     POST /automations/{id}/run. A run that returns `awaiting_approval`
 *     or `denied` is displayed as such and links to the approvals view.
 *   - History navigates via the lead-provided `navigate(view)` callback
 *     (app.js `showView`); without one it degrades to a text pointer.
 *
 * Lead wiring (do NOT edit forbidden files to wire this in — see report):
 *   1. index.html: add `<script src="savedtasks.js"></script>` then
 *      `<script src="onboarding_flow.js"></script>` before app.js
 *      (savedtasks first: this module calls ActionMeshSavedTasks if present).
 *   2. app.js: on boot, if `#onboardingMount` is not hidden, call
 *      `ActionMeshFirstRun.renderFirstRun(mount, bridge, {
 *         navigate: (view) => showView(view),
 *         onOpenApprovals: () => showView('approvals'),
 *         initiallyCompleted: state.settings.onboardingCompleted,
 *         resumeStep: ??? // leave unset: always resumes at welcome
 *       })`.
 *      Optional: pass the list of saved tasks after step 4 so step 5 can
 *      highlight the run. Without wiring, calling renderFirstRun is inert:
 *      it needs `#onboardingMount` + a bridge, and does no I/O until the
 *      user clicks "Draft my task".
 *
 * Pure helpers are exported for Node tests (desktop/test conventions:
 * node:test, no jsdom), attached as window.ActionMeshFirstRun.
 */
(function registerFirstRun(global) {
  'use strict';

  /** The six steps, in order. Each is skippable; skipping never blocks. */
  const STEPS = [
    { id: 'welcome', label: 'Welcome', blurb: 'ActionMesh proposes, policy decides, you control. This walk drafts, saves, and runs one real task — and grants nothing.' },
    { id: 'first-task', label: 'Your first task', blurb: 'Describe a small, safe thing to do. The planner reads it into a draft; nothing runs yet.' },
    { id: 'see-plan', label: 'See the plan', blurb: 'This is what the planner actually proposed. If it came up empty, it says why — no steps are ever invented.' },
    { id: 'approve-run', label: 'Save & run', blurb: 'Saving stores the plan and grants nothing. Running re-authorises every step, and may wait for your approval.' },
    { id: 'history', label: 'History', blurb: 'Where the run landed. An approval wait or a refusal stays visible here; it is never tidied into success.' },
    { id: 'done', label: 'Done', blurb: 'You have done a full loop: ask, plan, save, approve, run, remember. Finishing marks first-run complete.' },
  ];

  /** A safe first instruction for users who do not know what to type. */
  const SUGGESTION = 'check the disk';

  function stepIndex(id) {
    return STEPS.findIndex((s) => s.id === id);
  }

  function nextStepId(id) {
    const i = stepIndex(id);
    return i >= 0 && i < STEPS.length - 1 ? STEPS[i + 1].id : null;
  }

  /** A draft is saveable only with steps and a savable proposal body. */
  function isSaveable(draft) {
    return Boolean(draft) && (draft.steps || []).length > 0
      && Boolean(draft.proposal) && Boolean(draft.proposal.name)
      && (draft.proposal.steps || []).length > 0;
  }

  function saveableReason(draft) {
    if (!draft) return 'no draft yet';
    if ((draft.steps || []).length === 0) {
      return draft.reason || 'no steps were drafted, so there is nothing to save';
    }
    return 'this draft has no savable plan';
  }

  /** Run-state word, mirroring app.js RUN_STATE_WORDS. Never optimistic. */
  const RUN_STATE_WORDS = {
    succeeded: 'every step succeeded',
    failed: 'a step failed',
    denied: 'refused — no consent was granted',
    cancelled: 'cancelled',
    interrupted: 'interrupted by a restart',
    partial: 'some steps did not run',
    awaiting_approval: 'waiting for your approval',
    running: 'running now',
    claimed: 'claimed, not yet started',
    missed: 'missed — the occurrence passed',
    skipped: 'not run',
  };

  function runStateWord(state) {
    return RUN_STATE_WORDS[state] || state || 'unknown';
  }

  // -------------------------------------------------------------------------
  // DOM (createElement + textContent only; CSP-safe).
  // -------------------------------------------------------------------------
  let currentDoc = null;
  function doc(container) {
    if (container && container.ownerDocument) currentDoc = container.ownerDocument;
    return currentDoc || (typeof globalThis !== 'undefined' && globalThis.document
      ? globalThis.document : null);
  }

  function el(tag, attrs, children) {
    const d = doc();
    if (!d) return null;
    const node = d.createElement(tag);
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
      node.append(typeof child === 'object' && child.nodeType ? child : d.createTextNode(String(child)));
    }
    return node;
  }

  function replace(node, children) {
    if (!node) return;
    const d = doc();
    while (node.firstChild) node.removeChild(node.firstChild);
    for (const child of [].concat(children || [])) {
      if (child === null || child === undefined || child === false) continue;
      node.append(typeof child === 'object' && child.nodeType ? child : d.createTextNode(String(child)));
    }
  }

  function defaultBridge() {
    return typeof window !== 'undefined' ? window.actionmesh : null;
  }

  async function request(bridge, method, path, body) {
    const b = bridge || defaultBridge();
    if (!b || typeof b.request !== 'function') {
      throw new Error('backend bridge unavailable');
    }
    const result = await b.request({
      method, path, body: body === undefined ? null : body, long: false,
    });
    if (!result || !result.ok) {
      const detail = (result && result.body && result.body.detail)
        || (result && result.error) || (result && result.text) || 'request failed';
      throw new Error(String(detail));
    }
    return result.body;
  }

  /**
   * Mount the six-step flow under `container`.
   *
   * options: {
   *   navigate(view)        — lead's showView; drives history/approvals views
   *   onOpenApprovals()     — alternative to navigate('approvals')
   *   initiallyCompleted    — if true, the mount starts hidden (lead shows it
   *                           only when onboardingCompleted is unset)
   *   resumeStep            — step id to resume at (default 'welcome')
   * }
   *
   * Returns a controller: { element, currentStep(), goTo(id), skip(),
   * draftState, savedTask, lastRun, complete() }. Mounting performs NO
   * bridge I/O and writes nothing; only complete() writes
   * onboardingCompleted, and only once per call.
   */
  function renderFirstRun(container, bridge, options) {
    const opts = options || {};
    const api = bridge || defaultBridge();
    const d = doc(container);
    if (!container || !d) return null;

    let current = stepIndex(opts.resumeStep) >= 0 ? stepIndex(opts.resumeStep) : 0;
    let draft = null;
    let savedTask = null;
    let lastRun = null;
    let status = '';

    const root = el('section', {
      id: 'firstRunRoot', class: 'first-run', role: 'region',
      'aria-label': 'First-run guide',
    });
    if (opts.initiallyCompleted) root.setAttribute('hidden', '');

    const nav = el('ol', { class: 'first-run-steps' }, STEPS.map((s, i) => el('li', {
      class: i === current ? 'first-run-step current' : 'first-run-step',
      dataset: { step: s.id },
      text: s.label,
    })));
    const panel = el('div', { class: 'first-run-panel', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'false' });
    const controls = el('div', { class: 'first-run-controls' });
    root.append(nav, panel, controls);
    container.append(root);

    function setStatus(text) {
      status = text;
    }

    async function doDraft(text) {
      const clean = String(text || '').trim();
      if (!clean) { setStatus('Say what the task should do first.'); paint(); return null; }
      setStatus('Reading that into a plan…');
      paint();
      try {
        draft = await request(api, 'POST', '/automations/draft', {
          text: clean, actor: { id: 'voiceos-local' },
        });
        setStatus(isSaveable(draft)
          ? 'Draft ready. Review it, then save and run.'
          : (draft.reason || 'No steps were drafted.'));
        return draft;
      } catch (error) {
        setStatus(`Could not draft: ${error.message}`);
        paint();
        return null;
      }
    }

    async function doSaveAndRun() {
      if (!isSaveable(draft)) { setStatus(saveableReason(draft)); paint(); return; }
      setStatus('Saving. Nothing has run and no permission was granted…');
      paint();
      try {
        savedTask = await request(api, 'POST', '/automations', draft.proposal);
        const id = savedTask && savedTask.workflow_id;
        if (!id) { setStatus('Saved, but the server returned no task id — check the list.'); paint(); return; }
        setStatus('Saved. Running through the pipeline; every step is authorised again…');
        paint();
        lastRun = await request(api, 'POST', `/automations/${encodeURIComponent(id)}/run`, {});
        const state = lastRun && lastRun.state;
        setStatus(state === 'succeeded'
          ? 'Every step succeeded.'
          : `Run state: ${runStateWord(state)}. It has not been reported as a success.`);
        paint();
      } catch (error) {
        setStatus(`Could not save or run: ${error.message}`);
        paint();
      }
    }

    function paint() {
      while (panel.firstChild) panel.removeChild(panel.firstChild);
      while (controls.firstChild) controls.removeChild(controls.firstChild);
      const step = STEPS[current];
      nav.childNodes.forEach((li, i) => {
        li.className = i === current ? 'first-run-step current' : 'first-run-step';
      });
      panel.append(el('h3', { class: 'first-run-title', text: step.label }));
      if (step.blurb) panel.append(el('p', { class: 'first-run-blurb', text: step.blurb }));

      if (step.id === 'first-task') {
        const input = el('input', {
          class: 'input', type: 'text', value: SUGGESTION,
          'aria-label': 'Describe your first task',
        });
        const go = el('button', { class: 'btn primary', type: 'button', text: 'Draft my task' });
        go.addEventListener('click', async () => {
          await doDraft(input.value);
          if (draft) { current = stepIndex('see-plan'); paint(); }
        });
        panel.append(el('div', { class: 'field' }, [
          el('label', { text: 'Try' }), input,
        ]), go);
        panel.append(el('p', {
          class: 'row-sub',
          text: 'A safe first try: "check the disk". If no operator template matches, the draft says so and invents nothing.',
        }));
      } else if (step.id === 'see-plan') {
        if (!draft) {
          panel.append(el('p', { class: 'notice', text: 'No draft yet. Go back one step and describe a task.' }));
        } else {
          const real = draft.source && draft.source.startsWith('template:');
          panel.append(el('p', {
            class: 'row-sub',
            text: real
              ? `Steps from operator template "${draft.source.slice('template:'.length)}".`
              : 'The planner drafted no steps; it says why below.',
          }));
          const steps = (draft.steps || []).map((s, i) => el('li', {
            class: 'row-sub', text: `${i + 1}. ${s.label} (${s.action})`,
          }));
          panel.append(el('ul', { class: 'rows' }, steps.length ? steps : el('li', { class: 'empty', text: 'No steps.' })));
          if (draft.reason) panel.append(el('div', { class: 'notice', text: draft.reason }));
          if (draft.schedule && Object.keys(draft.schedule).length) {
            panel.append(el('p', { class: 'row-sub', text: `Trigger: ${JSON.stringify(draft.schedule)}` }));
          }
        }
      } else if (step.id === 'approve-run') {
        const saveRun = el('button', { class: 'btn primary', type: 'button', text: 'Save & run' });
        saveRun.disabled = !isSaveable(draft);
        saveRun.addEventListener('click', doSaveAndRun);
        panel.append(saveRun);
        if (!isSaveable(draft)) {
          panel.append(el('p', { class: 'notice', text: saveableReason(draft) }));
        }
        if (savedTask) {
          panel.append(el('p', { class: 'row-sub', text: `Saved task: ${savedTask.name || savedTask.workflow_id}` }));
        }
        if (lastRun) {
          panel.append(el('p', { class: 'row-sub', text: `Last run: ${runStateWord(lastRun.state)}` }));
          if (lastRun.state === 'awaiting_approval' || lastRun.state === 'denied') {
            const open = el('button', { class: 'btn ghost', type: 'button', text: 'Open approvals' });
            open.addEventListener('click', () => {
              if (typeof opts.onOpenApprovals === 'function') opts.onOpenApprovals();
              else if (typeof opts.navigate === 'function') opts.navigate('approvals');
            });
            panel.append(open);
          }
        }
        panel.append(el('p', {
          class: 'row-sub',
          text: 'Saving ≠ permission. The run re-authorises each step; a critical step will wait for your approval instead of running.',
        }));
      } else if (step.id === 'history') {
        const open = el('button', { class: 'btn primary', type: 'button', text: 'Open history' });
        open.addEventListener('click', () => {
          if (typeof opts.navigate === 'function') opts.navigate('history');
          else setStatus('History lives in the History view. Open it from the sidebar.');
        });
        panel.append(open);
        if (lastRun) {
          panel.append(el('p', { class: 'row-sub', text: `Your run: ${runStateWord(lastRun.state)}` }));
        } else {
          panel.append(el('p', { class: 'row-sub', text: 'Nothing has run yet — the previous step is where a run happens.' }));
        }
      } else if (step.id === 'done') {
        panel.append(el('p', {
          class: 'row-sub',
          text: 'You asked, saw the plan, saved it (granting nothing), ran it through policy, and saw where it landed. That is the whole loop.',
        }));
      }

      if (status) panel.append(el('div', { class: 'notice', 'aria-live': 'polite', text: status }));

      const back = el('button', {
        class: 'btn ghost small', type: 'button', text: 'Back',
        disabled: current === 0, 'aria-label': 'Back',
      });
      back.addEventListener('click', () => { if (current > 0) { current -= 1; setStatus(''); paint(); } });
      const skip = el('button', {
        class: 'btn ghost small', type: 'button', text: 'Skip this step',
        'aria-label': `Skip ${step.label}`,
      });
      skip.addEventListener('click', () => {
        if (current < STEPS.length - 1) { current += 1; setStatus(''); paint(); }
      });
      const next = el('button', {
        class: 'btn small', type: 'button',
        text: current === STEPS.length - 1 ? 'Finish' : 'Next',
        'aria-label': current === STEPS.length - 1 ? 'Finish first-run guide' : 'Next step',
      });
      next.addEventListener('click', async () => {
        if (current < STEPS.length - 1) { current += 1; setStatus(''); paint(); return; }
        await controller.complete();
      });
      controls.append(back, skip, next);
    }

    const controller = {
      element: root,
      currentStep() { return STEPS[current].id; },
      goTo(id) {
        const i = stepIndex(id);
        if (i < 0) return false;
        current = i; paint(); return true;
      },
      skip() {
        if (current >= STEPS.length - 1) return false;
        current += 1; paint(); return true;
      },
      get draftState() { return draft; },
      get savedTask() { return savedTask; },
      get lastRun() { return lastRun; },
      get status() { return status; },
      async complete() {
        // The ONLY write this module ever makes — the existing key, and
        // only from an explicit click. Idempotent: safe to call again
        // (e.g. a resumed session finishing late).
        if (api && typeof api.updateSettings === 'function') {
          await api.updateSettings({ onboardingCompleted: true });
        }
        root.setAttribute('hidden', '');
        if (container && typeof container.setAttribute === 'function') {
          container.setAttribute('hidden', '');
        }
      },
    };

    paint();
    return controller;
  }

  const api = {
    STEPS, SUGGESTION, RUN_STATE_WORDS,
    stepIndex, nextStepId, isSaveable, saveableReason, runStateWord,
    renderFirstRun,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof global !== 'undefined') {
    global.ActionMeshFirstRun = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
