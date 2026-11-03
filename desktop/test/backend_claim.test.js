'use strict';
/**
 * The data-directory claim.
 *
 * Two backends on one data directory interleave their audit sequence numbers and the
 * chain stops accepting appends (`UNIQUE constraint failed: audit_records.seq`) --
 * after which every execution fails. This was observed, not theorised. The claim is
 * the guard, so it is tested against a real second process: the interesting case is
 * two pids, and only a child gives one.
 */
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { clearClaim, isAlive, readClaim, takeClaim } = require('../app/backend.js');

const BACKEND = path.resolve(__dirname, '..', 'app', 'backend.js');

function tempDirs() {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'actionmesh-claim-'));
  const db = path.join(data, 'db');
  fs.mkdirSync(db, { recursive: true });
  return { data, db, logs: path.join(data, 'logs'), workspace: path.join(data, 'workspace') };
}

/** A short-lived process that takes the claim and reports its pid, then waits. */
function holdingChild(dirs) {
  const script = `
    const { takeClaim } = require(${JSON.stringify(BACKEND)});
    takeClaim({ db: ${JSON.stringify(dirs.db)} });
    process.stdout.write('claimed\\n');
    setInterval(() => {}, 1000);
  `;
  const child = spawn(process.execPath, ['-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('child never claimed')), 5000);
    child.stdout.once('data', () => {
      clearTimeout(timer);
      resolve(child);
    });
  });
}

function exited(child) {
  return new Promise((resolve) => child.once('exit', resolve));
}

test('a second live process cannot take the same data directory', async () => {
  const dirs = tempDirs();
  const child = await holdingChild(dirs);
  try {
    assert.equal(readClaim(dirs), child.pid);
    assert.throws(
      () => takeClaim(dirs),
      /already using/,
      'the guard must name the holder rather than start a second backend',
    );
  } finally {
    child.kill('SIGKILL');
    await exited(child);
  }
});

test('a claim left by a dead process is taken over, not obeyed', async () => {
  const dirs = tempDirs();
  const child = await holdingChild(dirs);
  const deadPid = child.pid;
  child.kill('SIGKILL');
  await exited(child);

  // The file survives the crash; the process does not. A crash must not brick the app.
  assert.equal(readClaim(dirs), deadPid);
  assert.equal(isAlive(deadPid), false);
  assert.doesNotThrow(() => takeClaim(dirs));
  assert.equal(readClaim(dirs), process.pid);
});

test('releasing the claim lets the next start take it', () => {
  const dirs = tempDirs();
  takeClaim(dirs);
  assert.equal(readClaim(dirs), process.pid);
  clearClaim(dirs, process.pid);
  assert.equal(readClaim(dirs), 0);
});

test('a process may not release a claim that is not its own', () => {
  const dirs = tempDirs();
  fs.writeFileSync(path.join(dirs.db, 'backend.lock'), `${process.pid + 100000}\n`);
  clearClaim(dirs, process.pid);
  // The claim that changed hands while this process was stopping belongs to the new
  // owner; releasing it would let a third backend in behind them.
  assert.equal(readClaim(dirs), process.pid + 100000);
});