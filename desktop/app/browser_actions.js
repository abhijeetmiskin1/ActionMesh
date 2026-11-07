'use strict';

/** Translate manual browser controls into ordinary, policy-checked actions. */
function createBrowserActionRunner({ request, getDeviceId }) {
  const operations = {
    open: ['open', ['url']], navigate: ['navigate', ['url']],
    search: ['search', ['query', 'engine']], snapshot: ['snapshot', []],
    state: ['inspect', []], click: ['click', ['ref', 'double']],
    type: ['type', ['ref', 'text']], select: ['select', ['ref', 'value']],
    scroll: ['scroll', ['ref', 'direction', 'amount']], back: ['back', []],
    forward: ['forward', []], refresh: ['refresh', []],
    wait: ['wait', ['ms', 'until']], stop: ['stop', []],
  };
  return async (frame) => {
    const operation = operations[frame.op];
    if (!operation) return { status: 'unsupported', note: 'No registered browser action serves this operation.' };
    const deviceId = await getDeviceId();
    if (!deviceId) return { status: 'unavailable', note: 'Configure a laptop device before driving the browser.' };
    const type = `browser.${operation[0]}`;
    const parameters = {};
    for (const key of operation[1]) {
      if (frame[key] !== undefined) parameters[key] = key === 'ref' ? String(frame[key]) : frame[key];
    }
    const answer = await request({ method: 'POST', path: '/execute', long: true, body: {
      actor: { id: 'voiceos-local' },
      policy: { rules: [{ action: type, decision: 'allow' }] },
      graph: { intent: `Browser panel: ${type}`, target_device_id: deviceId, nodes: [
        { node_id: 'browser', node_type: 'action', action: { type, provider: 'laptop', parameters } },
        { node_id: 'done', node_type: 'end', depends_on: ['browser'] },
      ] },
    } });
    if (!answer || !answer.ok || !answer.body) {
      return { status: 'error', note: 'The backend refused the browser request.' };
    }
    const result = answer.body;
    const node = (result.nodes || {}).browser || {};
    const verified = node.verified === true;
    return { status: result.state === 'success' ? (verified ? 'ok' : 'succeeded_unverified') : result.state,
      verified, execution_id: result.execution_id,
      note: node.error || result.error || (verified ? 'Independently verified.' : 'Not independently verified; check the execution or Approvals view.') };
  };
}

module.exports = { createBrowserActionRunner };
