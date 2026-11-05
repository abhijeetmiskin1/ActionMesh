'use strict';
/**
 * Saved tasks (Phase 6 Agent 5) — renderer module.
 *
 * A saved task IS a stored automation (a Workflow). There is no second store,
 * second journal, or second execution path here: every operation below is one
 * call against the existing `/automations` API family in actionmesh/api.py:
 *
 *   GET    /automations                      listSavedTasks
 *   GET    /automations/{workflow_id}        getSavedTask
 *   POST   /automations                      saveTask (name/steps/trigger only)
 *   PUT    /automations/{workflow_id}        renameTask / editTask
 *   POST   /automations/{workflow_id}/enabled  setTaskEnabled
 *   POST   /automations/{workflow_id}/run      runTask
 *   DELETE /automations/{workflow_id}         deleteTask
 *   POST   /automations/draft                 draftTask (steps come ONLY from
 *                                             an operator template — the route
 *                                             says so rather than inventing)
 *
 * Invariants (mirroring the backend, never weakening it):
 *  - Saving grants nothing. No body built here ever carries `grants`,
 *    `permissions`, an actor, or a risk ceiling — `POST`/`PUT /automations`
 *    refuse such a body with 400, and this module never sends one.
 *  - Every run is re-authorized. `runTask` posts `{}` and surfaces whatever
 *    state the server returns — including `awaiting_approval` and `denied` —
 *    verbatim. An unverified or waiting run is NEVER reported as success.
 *  - Steps only come from the real planner. `draftTask`/`buildExamples` show
 *    the draft's `reason` when `source` is "none"; a built-in example is only
 *    offered when the draft actually produced steps (source `template:*`).
 *  - No innerHTML anywhere (CSP); everything is createElement + textContent.
 *    The renderer is untrusted-text-safe: workflow names/steps are rendered
 *    as text, never parsed.
 *  - Light theme only: reuses `.row`, `.chip`, `.notice`, `.state` tokens.
 *
 * Transport is the single `window.actionmesh` bridge (preload.js), exactly
 * as app.js uses it: `bridge.request({method, path, body, long})`.
 *
 * Lead wiring (do NOT edit forbidden files to wire this in — see report):
 *   1. index.html: add `<script src="savedtasks.js"></script>` before app.js.
 *      The module paints into the EXISTING automations hosts — no new markup:
 *        #automationList, #automationCount, #automationNotes,
 *        #automationDraftForm, #automationText, #automationDraftGo,
 *        #automationDraft, #automationSelected, #automationRefresh
 *      and (for the first-run flow) #onboardingMount is NOT used here.
 *   2. app.js: call `ActionMeshSavedTasks.initSavedTasks(bridge)` on boot and
 *      delegate the automation list/detail clicks to `renderSavedTaskList` /
 *      `renderSavedTaskDetail` (or keep app.js renderers and call the pure
 *      API helpers below from them). Bridge: pass the same bridge object
 *      app.js uses (`window.actionmesh`).
 *
 * Exports for Node tests (desktop/test conventions: node:test, no jsdom),
 * attached as window.ActionMeshSavedTasks in the browser. Pure helpers take
 * an explicit bridge so tests inject a fake without touching globals.
 */
(function registerSavedTasks(global) {
  'use strict';

  /** The one transport this module may use (preload.js contextBridge). */
  const IDENTITY = { id: 'voiceos-local' };

  /**
   * Built-in example phrases. Each entry pairs the sentence a user would
   * type with the name of the operator template it needs. An example is
   * REAL only when the planner drafts steps for it; `exampleStatus`
   * (below) is what keeps a phrase honest — a phrase with no matching
   * operator template (source "none") is dropped, never shown as working.
   *
   * The phrases use actions in the shipped mock registry (seeds.py) so a
   * stock install with a tasks file can draft them; on any install where
   * the operator has not authored the template, the example simply reads
   * as "available when the template exists" and the draft button says so.
   */
  const EXAMPLES = [
    { id: 'daily-search', label: 'Daily web search (9am)', text: 'every day at 9am daily check' },
    { id: 'disk-check', label: 'Check the disk (by hand)', text: 'check the disk' },
    { id: 'interval-check', label: 'Disk check every 30 minutes', text: 'every 30 minutes check the disk' },
    { id: 'on-failure', label: 'Disk check when a run fails', text: 'when a run fails check the disk' },
  ];

  /** Run-state words, mirroring app.js RUN_STATE_WORDS. Never optimistic. */
  const RUN_STATE_WORDS = {
    succeeded: 'every step succeeded',
    failed: 'a step failed',
    denied: 'refused — see the step below',
    cancelled: 'cancelled',
    interrupted: 'interrupted by a restart',
    partial: 'some steps did not run',
    awaiting_approval: 'waiting for your approval',
    running: 'running now',
    claimed: 'claimed, not yet started',
    missed: 'missed — the occurrence passed while the app was not running',
    skipped: 'not run',
  };

  function runStateWord(state) {
    return RUN_STATE_WORDS[state] || state || 'not run yet';
  }

  /** True only for a genuinely verified success shape. */
  function isSuccess(run) {
    return Boolean(run) && run.state === 'succeeded';
  }

  // -------------------------------------------------------------------------
  // Thin typed wrappers over the bridge. Each takes `bridge` so tests inject
  // a fake; in the browser `defaultBridge()` is window.actionmesh.
  // -------------------------------------------------------------------------
  function defaultBridge() {
    return typeof window !== 'undefined' ? window.actionmesh : null;
  }

  async function request(bridge, method, path, body) {
    const b = bridge || defaultBridge();
    if (!b || typeof b.request !== 'function') {
      throw new Error('backend bridge unavailable');
    }
    const result = await b.request({
      method,
      path,
      body: body === undefined ? null : body,
      long: false,
    });
    if (!result || !result.ok) {
      const detail = (result && result.body && result.body.detail)
        || (result && result.error) || (result && result.text) || 'request failed';
      const error = new Error(String(detail));
      error.status = result && result.status;
      throw error;
    }
    return result.body;
  }

  /** GET /automations — the whole library (server is actor-scoped). */
  async function listSavedTasks(bridge) {
    const answer = await request(bridge, 'GET', '/automations');
    return (answer && answer.workflows) || [];
  }

  /** GET /automations/{workflow_id} — one saved task, or throws 404. */
  async function getSavedTask(bridge, workflowId) {
    return request(bridge, 'GET', `/automations/${encodeURIComponent(workflowId)}`);
  }

  /**
   * The body a save sends — name, description, steps, the one trigger, and
   * device/enabled. Deliberately has no grants/permissions/actor/max_risk
   * field: a saved plan grants nothing, and the server refuses a body that
   * tries to carry one. Steps keep their stored parameters verbatim.
   */
  function taskBody(task, overrides) {
    const t = task || {};
    const o = overrides || {};
    const steps = (o.steps !== undefined ? o.steps : t.steps || []).map((step, index) => ({
      label: step.label,
      action: step.action_type || step.action,
      parameters: step.parameters || {},
      when: step.when || {},
      device_id: step.device_id || '',
      index,
    }));
    return {
      name: o.name !== undefined ? o.name : t.name,
      description: o.description !== undefined ? o.description : (t.description || ''),
      steps,
      schedule: o.schedule !== undefined ? o.schedule : (t.schedule || {}),
      event: o.event !== undefined ? o.event : (t.event || {}),
      device_id: o.device_id !== undefined ? o.device_id : (t.device_id || ''),
      enabled: o.enabled !== undefined ? o.enabled : Boolean(t.enabled),
    };
  }

  /**
   * POST /automations — save a new task. The response's `runs_nothing`
   * sentence ("saved. Nothing has run and no permission was granted...")
   * is the honest part; callers should show it.
   */
  async function saveTask(bridge, task, overrides) {
    return request(bridge, 'POST', '/automations', taskBody(task, overrides));
  }

  /** PUT /automations/{workflow_id} — rename/edit a saved task. */
  async function editTask(bridge, workflowId, task, overrides) {
    return request(
      bridge, 'PUT', `/automations/${encodeURIComponent(workflowId)}`,
      taskBody(task, overrides),
    );
  }

  /** Rename is an edit with only the name changed; everything else is kept. */
  async function renameTask(bridge, workflowId, task, name) {
    const clean = String(name || '').trim();
    if (!clean) throw new Error('a task needs a name');
    return editTask(bridge, workflowId, task, { name: clean });
  }

  /** POST /automations/{workflow_id}/enabled — arm or disarm a trigger. */
  async function setTaskEnabled(bridge, workflowId, enabled) {
    if (typeof enabled !== 'boolean') throw new Error('enabled must be true or false');
    return request(bridge, 'POST', `/automations/${encodeURIComponent(workflowId)}/enabled`, { enabled });
  }

  /**
   * POST /automations/{workflow_id}/run — run through the real pipeline.
   *
   * The body is always `{}`: the server stamps the real owner and a fresh
   * PolicyEngine session re-authorizes every step. The returned run is
   * surfaced exactly as the server reports it — `awaiting_approval`,
   * `denied`, `partial`, etc. are returned, never reworded into success.
   */
  async function runTask(bridge, workflowId) {
    return request(bridge, 'POST', `/automations/${encodeURIComponent(workflowId)}/run`, {});
  }

  /** DELETE /automations/{workflow_id} — deletes the plan, keeps run history. */
  async function deleteTask(bridge, workflowId) {
    return request(bridge, 'DELETE', `/automations/${encodeURIComponent(workflowId)}`);
  }

  /**
   * POST /automations/draft — read a sentence into a draft. Nothing is
   * stored and nothing runs. `source` says where the steps came from:
   * `template:<name>` or `none` (no steps were invented).
   */
  async function draftTask(bridge, text) {
    const clean = String(text || '').trim();
    if (!clean) throw new Error('say what the task should do first');
    return request(bridge, 'POST', '/automations/draft', { text: clean, actor: IDENTITY });
  }

  /**
   * Classify a draft for display. "real" means the planner produced steps
   * from an operator template; anything else is shown with its reason and
   * is never offered as a working example.
   */
  function exampleStatus(draft) {
    const source = draft && draft.source;
    if (source && source.startsWith('template:') && (draft.steps || []).length > 0) {
      return { real: true, template: source.slice('template:'.length) };
    }
    return { real: false, reason: (draft && draft.reason) || 'no steps were drafted' };
  }

  /** A draft is saveable only when it has steps the server would accept. */
  function isSaveable(draft) {
    return Boolean(draft) && (draft.steps || []).length > 0
      && Boolean(draft.proposal) && Boolean(draft.proposal.name)
      && (draft.proposal.steps || []).length > 0;
  }

  // -------------------------------------------------------------------------
  // DOM rendering (createElement + textContent only, CSP-safe).
  // -------------------------------------------------------------------------
  let currentDoc = null;
  function doc(container) {
    if (container && container.ownerDocument) currentDoc = container.ownerDocument;
    return currentDoc || (typeof globalThis !== 'undefined' && globalThis.document
      ? globalThis.document : null);
  }

  function el(tag, attrs, children) {
    const d = doc();
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
      node.append(d.createTextNode ? (typeof child === 'object' && child.nodeType ? child : d.createTextNode(String(child))) : child);
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

  function scheduleWords(schedule) {
    const s = schedule || {};
    if (!Object.keys(s).length) return '';
    if (s.kind === 'interval') {
      const seconds = Number(s.seconds) || 0;
      if (seconds % 3600 === 0 && seconds >= 3600) {
        const hours = seconds / 3600;
        return `every ${hours === 1 ? 'hour' : `${hours} hours`}`;
      }
      const minutes = Math.round(seconds / 60) || 1;
      return `every ${minutes === 1 ? 'minute' : `${minutes} minutes`}`;
    }
    if (s.kind === 'daily') return `every day at ${s.at || 'an unset time'}`;
    if (s.kind === 'weekly') {
      const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      const named = (s.days || []).map((d) => days[d] || d).join(', ');
      return named ? `${named} at ${s.at || 'an unset time'}` : 'weekly, with no day set';
    }
    return `a ${s.kind || 'unrecognised'} schedule`;
  }

  function triggerWords(task) {
    if (!task) return '';
    if (task.trigger_kind === 'event') {
      const event = task.event || {};
      const narrowed = event.state ? ` (state ${event.state})` : '';
      return `when ${event.event_type || 'an event'}${narrowed}`;
    }
    if (task.trigger_kind === 'schedule') {
      return scheduleWords(task.schedule) || 'a schedule with no cadence';
    }
    return 'run by hand only';
  }

  function stateChip(state) {
    const chip = state === 'succeeded' ? 'succeeded'
      : state === 'denied' || state === 'failed' ? state
        : state === 'awaiting_approval' ? 'awaiting_approval' : 'planned';
    return el('span', { class: 'state', dataset: { s: chip }, text: runStateWord(state) });
  }

  /** One saved-task row. Clicking selects; buttons act, never dead. */
  function savedTaskRow(task, { onSelect, onRun } = {}) {
    const row = el('li', { class: 'row selectable', dataset: { id: task.workflow_id } }, [
      el('div', { class: 'row-main' }, [
        el('div', { class: 'row-title', text: task.name || '(unnamed task)' }),
        el('div', {
          class: 'row-sub',
          text: `${triggerWords(task)} · ${(task.steps || []).length} step(s) · ${task.enabled ? 'armed' : 'disarmed'}`,
        }),
      ]),
      el('div', { class: 'row-tail' }, [
        task.last_state ? stateChip(task.last_state) : null,
        el('span', { class: 'chip', text: task.trigger_kind || 'manual' }),
      ]),
    ]);
    if (onSelect) {
      row.setAttribute('tabindex', '0');
      row.setAttribute('role', 'button');
      row.setAttribute('aria-label', `Saved task ${task.name || task.workflow_id}`);
      row.addEventListener('click', () => onSelect(task));
      row.addEventListener('keydown', (event) => {
        if (event && (event.key === 'Enter' || event.key === ' ')) {
          if (typeof event.preventDefault === 'function') event.preventDefault();
          onSelect(task);
        }
      });
    }
    if (onRun) {
      const runButton = el('button', {
        class: 'btn ghost small', type: 'button', text: 'Run',
        'aria-label': `Run saved task ${task.name || task.workflow_id}`,
      });
      runButton.addEventListener('click', (event) => {
        if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
        onRun(task);
      });
      row.append(el('div', { class: 'row-actions' }, [runButton]));
    }
    return row;
  }

  /** Render the list into #automationList; returns the tasks shown. */
  async function renderSavedTaskList(bridge, callbacks) {
    // In the browser the hosts are global by id; in tests the caller may
    // pass them explicitly via callbacks.hosts (mirrors onboarding.js,
    // which resolves doc from the container it is mounted into).
    let d = (callbacks && callbacks.hosts && (callbacks.hosts.automationList || callbacks.hosts.ownerDocument))
      ? doc(callbacks.hosts.automationList || callbacks.hosts) : doc();
    if (!d && callbacks && callbacks.host) d = doc(callbacks.host);
    const list = (callbacks && callbacks.hosts && callbacks.hosts.automationList)
      || (d ? d.getElementById('automationList') : null);
    if (!list) return [];
    const count = (callbacks && callbacks.hosts && callbacks.hosts.automationCount)
      || (d ? d.getElementById('automationCount') : null);
    const notes = (callbacks && callbacks.hosts && callbacks.hosts.automationNotes)
      || (d ? d.getElementById('automationNotes') : null);
    replace(list, el('li', { class: 'empty', text: 'Loading…' }));
    try {
      const tasks = await listSavedTasks(bridge);
      if (count) count.textContent = String(tasks.length);
      if (notes) {
        notes.textContent = 'A saved task grants nothing. Every step is authorised again when it runs.';
      }
      if (!tasks.length) {
        replace(list, el('li', {
          class: 'empty',
          text: 'No saved tasks yet. Describe one on the right to see a draft, or try an example.',
        }));
        return tasks;
      }
      replace(list, tasks.map((task) => savedTaskRow(task, callbacks)));
      return tasks;
    } catch (error) {
      replace(list, el('li', { class: 'empty', text: `Could not read saved tasks: ${error.message}` }));
      return [];
    }
  }

  /**
   * Render one task's detail (view / rename / edit / delete / run / reopen)
   * into a host. All buttons call the real API; a run that comes back
   * `awaiting_approval` or `denied` is shown as exactly that.
   */
  function renderSavedTaskDetail(host, task, bridge, { onChanged, onRun } = {}) {
    if (!host) return;
    doc(host);
    if (!task) {
      replace(host, el('div', { class: 'empty-state' }, [
        el('p', { text: 'Select a saved task to see its plan. Saving a task grants nothing.' }),
      ]));
      return;
    }
    const nameInput = el('input', {
      class: 'input', type: 'text', value: task.name || '',
      'aria-label': 'Task name',
    });
    const saveButton = el('button', { class: 'btn primary', type: 'button', text: 'Save changes' });
    const runButton = el('button', { class: 'btn ghost', type: 'button', text: 'Run now' });
    const toggleButton = el('button', {
      class: 'btn ghost', type: 'button',
      text: task.enabled ? 'Disarm' : 'Arm',
    });
    const deleteButton = el('button', { class: 'btn ghost', type: 'button', text: 'Delete task' });
    const status = el('div', { class: 'notice', 'aria-live': 'polite', text: 'Not run yet.' });
    if (task.last_state) {
      status.textContent = `Last run: ${runStateWord(task.last_state)}`;
    }

    saveButton.addEventListener('click', async () => {
      try {
        const updated = await renameTask(bridge, task.workflow_id, task, nameInput.value);
        status.textContent = 'Saved. Nothing has run and no permission was granted.';
        if (onChanged) onChanged(updated);
      } catch (error) {
        status.textContent = `Could not save: ${error.message}`;
      }
    });
    runButton.addEventListener('click', async () => {
      status.textContent = 'Running through the pipeline…';
      try {
        const run = await runTask(bridge, task.workflow_id);
        // Surface the server state honestly — never tidy it into success.
        status.textContent = isSuccess(run) || run.state === 'succeeded'
          ? 'Every step succeeded.'
          : `Run state: ${runStateWord(run.state)}`;
        if (onRun) onRun(run);
      } catch (error) {
        status.textContent = `Could not run: ${error.message}`;
      }
    });
    toggleButton.addEventListener('click', async () => {
      try {
        await setTaskEnabled(bridge, task.workflow_id, !task.enabled);
        if (onChanged) onChanged({ ...task, enabled: !task.enabled });
      } catch (error) {
        status.textContent = `Could not change: ${error.message}`;
      }
    });
    deleteButton.addEventListener('click', async () => {
      try {
        await deleteTask(bridge, task.workflow_id);
        status.textContent = 'Deleted. Its run history is kept.';
        if (onChanged) onChanged(null);
      } catch (error) {
        status.textContent = `Could not delete: ${error.message}`;
      }
    });

    const steps = (task.steps || []).map((step, index) => el('li', {
      class: 'row-sub',
      text: `${index + 1}. ${step.label || step.action_type || step.action} (${step.action_type || step.action || '?'})`,
    }));
    replace(host, [
      el('div', { class: 'detail-head' }, [
        el('div', {}, [
          el('div', { class: 'detail-title', text: task.name || '(unnamed task)' }),
          el('div', {
            class: 'detail-meta',
            text: `${triggerWords(task)} · id ${task.workflow_id}`,
          }),
        ]),
        task.last_state ? stateChip(task.last_state) : null,
      ]),
      el('div', { class: 'field' }, [
        el('label', { text: 'Name' }),
        nameInput,
      ]),
      el('ul', { class: 'rows' }, steps.length ? steps : el('li', { class: 'empty', text: 'No steps in this plan.' })),
      el('div', { class: 'detail-actions' }, [saveButton, runButton, toggleButton, deleteButton]),
      status,
      el('p', { class: 'row-sub', text: 'Saving grants nothing. A run re-authorises every step, and may wait for your approval.' }),
    ]);
  }

  /**
   * Render a draft review into #automationDraft. A draft with no steps
   * (source "none") is shown with the planner's reason and offers no save
   * button — the honest refusal, not a dead control.
   */
  function renderDraft(host, draft, bridge, { onSaved } = {}) {
    if (host) doc(host);
    if (!host) return;
    if (!draft) {
      replace(host, el('div', { class: 'empty', text: 'No draft yet.' }));
      return;
    }
    const status = exampleStatus(draft);
    const parts = [
      el('div', { class: 'detail-title', text: draft.name || 'Draft' }),
      el('div', {
        class: 'row-sub',
        text: status.real
          ? `Steps from operator template "${status.template}". Review, then save.`
          : 'No steps were drafted — a step only comes from an operator-authored phrase.',
      }),
    ];
    if (draft.schedule && Object.keys(draft.schedule).length) {
      parts.push(el('div', { class: 'row-sub', text: `Trigger: ${scheduleWords(draft.schedule)}` }));
    } else if (draft.event && Object.keys(draft.event).length) {
      parts.push(el('div', { class: 'row-sub', text: `Trigger: when ${draft.event.event_type || 'an event'}` }));
    }
    const steps = (draft.steps || []).map((step, index) => el('li', {
      class: 'row-sub', text: `${index + 1}. ${step.label} (${step.action})`,
    }));
    parts.push(el('ul', { class: 'rows' }, steps.length ? steps : el('li', { class: 'empty', text: 'No steps — nothing to save.' })));
    if (!status.real && draft.reason) {
      parts.push(el('div', { class: 'notice', text: draft.reason }));
    }
    if (isSaveable(draft)) {
      const saveButton = el('button', { class: 'btn primary', type: 'button', text: 'Save this task' });
      const note = el('div', { class: 'notice', 'aria-live': 'polite', text: '' });
      saveButton.addEventListener('click', async () => {
        try {
          // Save the exact proposal body the planner produced — never a
          // re-typed plan this page invented.
          const saved = await request(bridge, 'POST', '/automations', draft.proposal);
          note.textContent = (saved && saved.runs_nothing) || 'Saved. Nothing has run and no permission was granted.';
          if (onSaved) onSaved(saved);
        } catch (error) {
          note.textContent = `Could not save: ${error.message}`;
        }
      });
      parts.push(el('div', { class: 'detail-actions' }, [saveButton]), note);
      parts.push(el('p', {
        class: 'row-sub',
        text: 'Saving stores the task disarmed: no schedule or event starts it until '
          + 'you arm it, and every step is authorised again each time it runs.',
      }));
    }
    replace(host, parts);
  }

  /** Draft + save in one step (used by the first-run flow and examples). */
  async function draftAndSave(bridge, text) {
    const draft = await draftTask(bridge, text);
    if (!isSaveable(draft)) {
      const error = new Error(draft.reason || 'no steps were drafted');
      error.draft = draft;
      throw error;
    }
    const saved = await request(bridge, 'POST', '/automations', draft.proposal);
    return { draft, saved };
  }

  /**
   * Wire the existing automations hosts (index.html, lead-owned) to this
   * module. Safe to call when hosts are absent (lead has not mounted the
   * script yet): it simply does nothing rather than throwing at boot.
   */
  function initSavedTasks(bridge) {
    const d0 = (typeof globalThis !== 'undefined' && globalThis.document) || null;
    if (d0) doc(d0.body || d0);
    const b = bridge || defaultBridge();
    const d = doc();
    const form = d ? d.getElementById('automationDraftForm') : null;
    const text = d ? d.getElementById('automationText') : null;
    const go = d ? d.getElementById('automationDraftGo') : null;
    const draftHost = d ? d.getElementById('automationDraft') : null;
    const refresh = d ? d.getElementById('automationRefresh') : null;
    const selected = d ? d.getElementById('automationSelected') : null;
    if (!form || !text || !go || !draftHost) return false;

    const submit = async (event) => {
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      try {
        const draft = await draftTask(b, text.value);
        renderDraft(draftHost, draft, b, {
          onSaved: () => { renderSavedTaskList(b, {}); },
        });
      } catch (error) {
        renderDraft(draftHost, { name: 'Draft', steps: [], source: 'none', reason: error.message }, b);
      }
    };
    form.addEventListener('submit', submit);
    go.addEventListener('click', submit);
    if (refresh) refresh.addEventListener('click', () => { renderSavedTaskList(b, {}); });
    if (selected) {
      renderSavedTaskDetail(selected, null, b);
    }
    renderSavedTaskList(b, {
      onSelect: (task) => { renderSavedTaskDetail(selected, task, b, { onChanged: () => renderSavedTaskList(b, {}) }); },
      onRun: async (task) => {
        const run = await runTask(b, task.workflow_id);
        const host = doc() ? doc().getElementById('automationSelected') : null;
        if (host) {
          renderSavedTaskDetail(host, task, b, { onChanged: () => renderSavedTaskList(b, {}) });
        }
        return run;
      },
    });
    return true;
  }

  const api = {
    IDENTITY, EXAMPLES, RUN_STATE_WORDS,
    runStateWord, isSuccess, exampleStatus, isSaveable,
    taskBody, scheduleWords, triggerWords,
    listSavedTasks, getSavedTask, saveTask, editTask, renameTask,
    setTaskEnabled, runTask, deleteTask, draftTask, draftAndSave,
    savedTaskRow, renderSavedTaskList, renderSavedTaskDetail, renderDraft,
    initSavedTasks,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof global !== 'undefined') {
    global.ActionMeshSavedTasks = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
