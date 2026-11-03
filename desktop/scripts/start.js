#!/usr/bin/env node
/**
 * Launch the development build with the strongest sandbox this machine can actually give it.
 *
 * Chromium on Linux has exactly two sandbox backends, and `npm start` from a checkout can
 * land in a place where neither is usable:
 *
 *   1. The setuid helper (`chrome-sandbox`), which must be root-owned with mode 4755.
 *      npm installs it owned by the developer, so Chromium aborts with "The SUID sandbox
 *      helper binary was found, but is not configured correctly".
 *   2. Unprivileged user namespaces, which need no root — but several distributions
 *      (Ubuntu 23.10+ with AppArmor, and hardened kernels) refuse them to unprivileged
 *      processes. `unshare --user --map-root-user` then fails with EPERM.
 *
 * Refusing to start is not a useful answer for a development command: neither backend is
 * something the developer can fix without root, and the packaged .deb does its own setup
 * from a root post-install script (it installs an AppArmor profile for the app, which is
 * what lets the userns backend work on those same distributions).
 *
 * So this script probes in order and takes the first backend that works — setuid, then
 * userns, then no sandbox — and *says which one it took* rather than degrading silently.
 * The no-sandbox case prints a warning naming the deb as the way to get a sandboxed build.
 */
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

// `require('electron')` from Node resolves to the path of the binary.
const electron = require('electron');
const helper = path.join(path.dirname(electron), 'chrome-sandbox');
const appDir = path.resolve(__dirname, '..');

/** True when the setuid helper is root-owned and setuid, which is what Chromium checks. */
function setuidSandboxWorks() {
  try {
    const stat = fs.statSync(helper);
    return stat.uid === 0 && (stat.mode & 0o4000) !== 0;
  } catch {
    return false;
  }
}

/** True when the kernel will map a root uid inside a new user namespace. */
function userNamespaceSandboxWorks() {
  const probe = spawnSync('unshare', ['--user', '--map-root-user', 'true'], {
    stdio: 'ignore',
    timeout: 5_000,
  });
  // A missing `unshare` is not evidence that namespaces are unavailable; Chromium does
  // not use that binary. Treat "cannot probe" as "may work" and let Chromium decide.
  if (probe.error && probe.error.code === 'ENOENT') return true;
  return probe.status === 0;
}

const args = [appDir, ...process.argv.slice(2)];
const explicit = args.some((a) => a === '--no-sandbox' || a === '--disable-setuid-sandbox');

if (!explicit) {
  if (setuidSandboxWorks()) {
    // Nothing to add: this is the strongest backend and it is available.
  } else if (userNamespaceSandboxWorks()) {
    args.push('--disable-setuid-sandbox');
    console.log('Using the user-namespace sandbox (chrome-sandbox is not root-owned here).');
  } else {
    args.push('--no-sandbox');
    console.warn(
      'WARNING: no Chromium sandbox is available on this machine.\n'
        + '  chrome-sandbox is not root-owned/setuid, and this kernel denies unprivileged\n'
        + '  user namespaces (see unshare --user --map-root-user).\n'
        + '  Starting with --no-sandbox, which disables renderer isolation.\n'
        + '  For a sandboxed build, install the package: sudo dpkg -i release/*.deb\n'
        + '  Its post-install step sets up the sandbox properly from root.',
    );
  }
}

const child = spawn(electron, args, { stdio: 'inherit' });
child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});