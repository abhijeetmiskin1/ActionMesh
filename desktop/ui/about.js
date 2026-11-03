'use strict';
/**
 * The About window's only script.
 *
 * It renders facts the main process reports about itself — version, bundle id, the
 * runtime it is embedding — and nothing else. No network, no configuration, no
 * buttons: everything here is a read of `app:state`.
 */
const bridge = window.actionmesh;

function row(label, value) {
  const dt = document.createElement('dt');
  const dd = document.createElement('dd');
  dt.textContent = label;
  dd.textContent = value === undefined || value === null || value === '' ? '—' : String(value);
  return [dt, dd];
}

async function boot() {
  let details = null;
  try {
    details = await bridge.state();
  } catch {
    details = null;
  }

  const kv = document.getElementById('aboutKv');
  if (!kv) return;
  const rows = details
    ? [
      ...row('Version', details.version),
      ...row('Bundle identifier', details.appId),
      ...row('Platform', `${details.platform} ${details.arch}`),
      ...row('Electron', details.electron),
      ...row('Chromium', details.chrome),
      ...row('Node', details.node),
      ...row('Build', details.packaged ? 'packaged release' : 'development'),
    ]
    : [...row('Version', 'unavailable')];
  for (const node of rows) kv.append(node);

  // Both strings are constants in the bundle, not values from the backend: a title
  // in an About window should not be something a running process can rewrite.
  const license = document.getElementById('aboutLicense');
  if (license) license.textContent = 'Licensed under Apache-2.0. Built with Electron.';
}

document.addEventListener('DOMContentLoaded', boot);