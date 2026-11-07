'use strict';
/**
 * The managed backend.
 *
 * The desktop app does not reimplement the control plane. It starts the *same*
 * FastAPI application the dashboard and /voice serve, on a loopback port it chose,
 * with a key only this process knows. That keeps one policy engine, one approval
 * store and one audit chain — a second implementation inside the shell would be a
 * second authority, which is exactly what this project's invariants forbid.
 *
 * The startup sequence is fixed and each step is a fact the UI can show:
 *   dirs -> config -> credentials -> backend -> laptop agent -> /ready -> connect
 *
 * "Laptop agent" is part of the backend process (ACTIONMESH_AGENT=1), not a second
 * process: the agent's boundary configuration is validated at startup, and a
 * misconfigured workspace is a startup failure rather than a quietly reduced
 * capability list.
 */
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');

const READY_TIMEOUT_MS = 45_000;
const READY_POLL_MS = 150;
/** Keep the backend log from growing without bound across restarts. */
const LOG_ROTATE_BYTES = 8 * 1024 * 1024;

/** Directories the app owns. Created before anything else touches them. */
function ensureDirs(dataDir) {
  const dirs = {
    data: dataDir,
    db: path.join(dataDir, 'db'),
    logs: path.join(dataDir, 'logs'),
    workspace: path.join(dataDir, 'workspace'),
  };
  for (const dir of Object.values(dirs)) fs.mkdirSync(dir, { recursive: true });
  return dirs;
}

/** Keep process logs outside any operator-selected file workspace. */
function chooseAgentLogDir(workspace, desired) {
  const root = fs.realpathSync(workspace);
  const outside = candidate => {
    const relative = path.relative(root, candidate);
    return relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative);
  };
  const candidate = path.join(fs.realpathSync(path.dirname(desired)), path.basename(desired));
  if (outside(candidate)) return candidate;
  const temporaryRoot = fs.realpathSync(os.tmpdir());
  if (!outside(temporaryRoot)) throw new Error('Choose a workspace that leaves room for private agent logs.');
  // mkdtemp creates a private 0700 directory; never reuse an untrusted existing path.
  return fs.mkdtempSync(path.join(temporaryRoot, 'actionmesh-agent-logs-'));
}

/** Ask the kernel for a free loopback port, then release it for the child. */
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/**
 * Where Python and the application live.
 *
 * Packaged: a standalone CPython under resources/runtime plus the source tree under
 * resources/app. Development: the repository's own virtualenv, so what the shell
 * runs is the same interpreter the tests ran on.
 */
function resolveRuntime({ appRoot, isPackaged, resourcesPath }) {
  if (isPackaged) {
    const runtime = path.join(resourcesPath, 'runtime');
    // The sidecar is a launcher for the *staged* application, so the bundled
    // interpreter, its site-packages and the app source are all beside each other.
    const python = path.join(runtime, 'python', 'bin', 'python3');
    const stagedApp = path.join(runtime, 'app');
    return {
      python,
      appRoot: stagedApp,
      entry: ['-m', 'uvicorn', 'actionmesh.api:app'],
      mode: 'bundled',
    };
  }
  const venv = path.join(appRoot, '.venv', 'bin', 'python');
  const python = fs.existsSync(venv) ? venv : 'python3';
  return {
    python,
    appRoot,
    entry: ['-m', 'uvicorn', 'actionmesh.api:app'],
    mode: fs.existsSync(venv) ? 'development-venv' : 'system-python',
    // Debian ships PyGObject as a compiled extension in the *system* dist-packages,
    // not on PyPI in a form a venv can use: it is bound to the machine's GLib and
    // AT-SPI. A venv with `include-system-site-packages = false` therefore cannot
    // `import gi` — and without `gi` the agent cannot reach the screenshot portal or
    // the accessibility tree, so `desktop_reachable` is honestly false and the screen
    // capabilities are not advertised at all.
    //
    // Appended, never prepended: this path only exists to be a fallback for packages
    // the venv does not have. Everything the venv *does* install still wins.
    // Development only — a packaged runtime must remain exactly what was staged.
    systemPackages: fs.existsSync('/usr/lib/python3/dist-packages')
      ? '/usr/lib/python3/dist-packages'
      : '',
  };
}

/**
 * One backend per data directory.
 *
 * The store this app starts one process against is a set of SQLite files: an
 * execution database, an audit chain, and a VoiceOS database. SQLite tolerates
 * several readers, but the *audit chain* is not a database so much as an append-only
 * log whose sequence numbers are computed in memory; two processes appending to it
 * interleave and collide (`UNIQUE constraint failed: audit_records.seq`), and from
 * then on every execution fails. That is not a hypothetical: it happened, from a
 * second Electron copy that spawned a second backend before the single-instance lock
 * had taken effect.
 *
 * So the directory itself carries a claim. A backend that finds a live claim refuses
 * to start and says which pid holds it. A claim held by a dead process is stale and
 * is taken over, because a crash must not brick the app.
 */
function claimPath(dirs) {
  return path.join(dirs.db, 'backend.lock');
}

/** The pid recorded in the claim file, or 0 when there is none. */
function readClaim(dirs) {
  try {
    const pid = Number.parseInt(fs.readFileSync(claimPath(dirs), 'utf8').trim(), 10);
    return Number.isInteger(pid) && pid > 0 ? pid : 0;
  } catch {
    return 0;
  }
}

/** Signal 0 asks the kernel whether a pid exists without touching it. */
function isAlive(pid) {
  if (!pid || pid === process.pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM means it exists and belongs to someone else -- still alive.
    return error.code === 'EPERM';
  }
}

/** Refuse to start when another live process owns this data directory. */
function assertUnclaimed(dirs) {
  const holder = readClaim(dirs);
  if (holder && isAlive(holder)) {
    throw new Error(
      `another ActionMesh backend (pid ${holder}) is already using ${dirs.db}. `
      + 'Two backends on one audit chain corrupt it. Close the other copy first.',
    );
  }
  // Stale: the process that wrote it is gone. Replacing it is the recovery path.
  if (holder) fs.rmSync(claimPath(dirs), { force: true });
}

/**
 * Take the claim, or throw naming the process that holds it.
 *
 * `wx` makes creation itself the test: two processes starting at the same instant
 * cannot both succeed, so the check above cannot be raced past between reading and
 * writing. A claim that appears under us but whose owner is dead is stale and is
 * removed once, then taken again.
 */
function takeClaim(dirs) {
  assertUnclaimed(dirs);
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      fs.writeFileSync(claimPath(dirs), `${process.pid}\n`, { flag: 'wx', mode: 0o600 });
      return;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
    const holder = readClaim(dirs);
    if (holder && isAlive(holder)) {
      throw new Error(
        `another ActionMesh backend (pid ${holder}) is already using ${dirs.db}. `
        + 'Two backends on one audit chain corrupt it. Close the other copy first.',
      );
    }
    fs.rmSync(claimPath(dirs), { force: true });
  }
  throw new Error(`could not claim ${claimPath(dirs)}`);
}

function clearClaim(dirs, pid) {
  // Only the owner may release it: a stale claim is replaced on the next start, and
  // a claim that changed hands while this process was stopping belongs to the new
  // owner now.
  if (readClaim(dirs) === pid) {
    try {
      fs.rmSync(claimPath(dirs), { force: true });
    } catch {
      /* already gone */
    }
  }
}

class Backend {
  constructor({ dataDir, appRoot, isPackaged, resourcesPath, settings, key, bridge, browserBridge }) {
    this.dataDir = dataDir;
    this.appRoot = appRoot;
    this.isPackaged = isPackaged;
    this.resourcesPath = resourcesPath;
    this.settings = settings;
    this.key = key; // loopback API key — memory only, never written to disk
    // C41: where the planner's model calls go. The *credential* is deliberately absent
    // here — `bridge` carries a socket path and a per-launch token that authorises
    // "talk to that socket", nothing more. The key stays in this process, which is the
    // one place the OS keyring can be opened.
    this.bridge = bridge || null;
    // C42: the browser drive socket. Same shape as `bridge`, same honesty: a socket
    // path and a per-launch token, never a credential — none crosses this channel.
    this.browserBridge = browserBridge || null;
    this.child = null;
    this.port = 0;
    this.baseUrl = '';
    this.status = 'stopped';
    this.lastError = '';
    this.startedAt = null;
    this.dirs = null;
    this.runtime = null;
    // True only while this Electron process holds the data-directory claim.
    this.claimed = false;
  }

  /** Everything the app is allowed to say about itself; no key material. */
  snapshot() {
    return {
      status: this.status,
      port: this.port,
      baseUrl: this.baseUrl,
      startedAt: this.startedAt,
      lastError: this.lastError,
      runtimeMode: this.runtime ? this.runtime.mode : 'unknown',
      dataDir: this.dataDir,
    };
  }

  /** 1-3: dirs, config overlay, credentials. */
  prepare() {
    this.dirs = ensureDirs(this.dataDir);
    const workspace = this.settings.workspaceRoot || this.dirs.workspace;
    fs.mkdirSync(workspace, { recursive: true });
    this.agentLogDir = chooseAgentLogDir(workspace, path.join(this.dirs.logs, 'agent'));
    this.runtime = resolveRuntime({
      appRoot: this.appRoot,
      isPackaged: this.isPackaged,
      resourcesPath: this.resourcesPath,
    });
    // The interpreter's PYTHONPATH and cwd must point at the *staged* source in a
    // packaged build (resources/runtime/app), not at the shell's own resources
    // directory. Let the runtime resolver own that answer; keeping the constructor
    // argument would start the bundled Python against a path that does not exist.
    this.appRoot = this.runtime.appRoot;
    // Fail loudly here rather than as a wall of ModuleNotFoundError later. A
    // packaged build whose staged source is missing is broken, not degraded --
    // and a shell that starts "ready" against nothing is a lie the UI cannot see.
    const python = this.runtime.python;
    if (python.includes(path.sep) && !fs.existsSync(python)) {
      throw new Error(`no Python runtime at ${python}`);
    }
    if (!fs.existsSync(this.appRoot)) {
      throw new Error(`no application source at ${this.appRoot}`);
    }
    // Compiled PyGObject belongs to the host CPython ABI. Keep the bundled
    // backend isolated and call only the shipped, typed OS adapter with host Python.
    this.nativeDesktop = false;
    if (this.isPackaged && process.platform === 'linux' && this.settings.agentEnabled && this.settings.allowDesktop) {
      try {
        const probe = spawnSync('/usr/bin/python3', ['-I',
          path.join(this.appRoot, 'actionmesh', 'agent', 'device_screen.py'), '--native-probe'], {
          timeout: 3000, maxBuffer: 1024, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], env: {},
        });
        this.nativeDesktop = probe.status === 0 && JSON.parse(probe.stdout).available === true;
      } catch { /* unavailable: never advertise a fake desktop */ }
    }
    this.nativeMedia = false;
    if (this.isPackaged && process.platform === 'linux' && this.settings.agentEnabled && this.settings.allowMedia) {
      try {
        const probe = spawnSync('/usr/bin/python3', ['-I',
          path.join(this.appRoot, 'actionmesh', 'agent', 'mpris.py'), '--native-probe'], {
          timeout: 3000, maxBuffer: 1024, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], env: {},
        });
        this.nativeMedia = probe.status === 0 && JSON.parse(probe.stdout).available === true;
      } catch { /* unavailable; never enable another OS boundary */ }
    }
    return { workspace, ...this.runtime };
  }

  env(port, workspace) {
    const s = this.settings;
    const env = {
      ...process.env,
      ACTIONMESH_ENV: 'development',
      ACTIONMESH_HOST: '127.0.0.1',
      ACTIONMESH_PORT: String(port),
      ACTIONMESH_API_KEY: this.key,
      ACTIONMESH_DATABASE_URL: `sqlite:///${path.join(this.dirs.db, 'executions.db')}`,
      ACTIONMESH_AUDIT_DB: path.join(this.dirs.db, 'audit.db'),
      ACTIONMESH_VOICEOS_DB: path.join(this.dirs.db, 'voiceos.db'),
      // Automations get their own file, like VoiceOS: saved plans and their run
      // history have to outlive a restart, or the scheduler's "did this already
      // run?" question is answered against an empty table every launch.
      ACTIONMESH_AUTOMATIONS_DB: path.join(this.dirs.db, 'automations.db'),
      // A file-backed signing key means paired devices survive a restart, which is
      // the difference between "your phone stays paired" and "re-pair every launch".
      ACTIONMESH_SECRET_FILE: path.join(this.dirs.db, 'signing.key'),
      PYTHONUNBUFFERED: '1',
      // Never let a stray PYTHONPATH from the user's shell shadow the app's modules.
      PYTHONPATH: this.appRoot,
      // The system PyGObject directory, if the development runtime found one. It is
      // *not* put on PYTHONPATH: PYTHONPATH entries precede site-packages, so a system
      // `click` or `typing_extensions` would shadow the venv's copy and break uvicorn.
      // `device_screen.ensure_gi_importable()` appends it instead, at the end of
      // sys.path, where it can only fill gaps. Absent in a packaged runtime.
      ...(this.runtime.systemPackages
        ? { ACTIONMESH_SYSTEM_PACKAGES: this.runtime.systemPackages }
        : {}),
      // A packaged runtime must not go looking at the user's site-packages: what
      // ships is exactly what runs.
      PYTHONNOUSERSITE: '1',
      ACTIONMESH_NATIVE_DESKTOP: this.nativeDesktop ? '1' : '0',
      ACTIONMESH_NATIVE_MEDIA: this.nativeMedia ? '1' : '0',
    };
    // 5: the laptop agent, configured from the app's own settings.
    if (s.agentEnabled) {
      env.ACTIONMESH_AGENT = '1';
      env.ACTIONMESH_WORKSPACE_ROOT = workspace;
      env.ACTIONMESH_DEVICE_ID = s.deviceId || 'laptop-1';
      env.ACTIONMESH_DEVICE_NAME = s.deviceName || 'This laptop';
      env.ACTIONMESH_ALLOW_SHELL = s.allowShell ? '1' : '0';
      // Not derived from allowShell: seeing the screen is its own boundary.
      env.ACTIONMESH_ALLOW_DESKTOP = s.allowDesktop ? '1' : '0';
      // Browser and media are distinct boundaries too: a user may allow shell
      // and not the controlled browser. Default off, so a legacy settings file
      // without these keys reads as "no".
      env.ACTIONMESH_ALLOW_BROWSER = s.allowBrowser ? '1' : '0';
      env.ACTIONMESH_ALLOW_MEDIA = s.allowMedia ? '1' : '0';
      env.ACTIONMESH_ALLOWED_COMMANDS = (s.allowedCommands || []).join(',');
      env.ACTIONMESH_AGENT_LOG_DIR = this.agentLogDir || path.join(this.dirs.logs, 'agent');
    }
    if (s.voiceTasksFile) env.ACTIONMESH_VOICEOS_TASKS = s.voiceTasksFile;
    // 6: the model bridge. The credential is *not* here — only the address of the
    // socket the trusted main process serves, and the token that authorises one
    // conversation with it. The environment is the only sane channel: argv is visible
    // in /proc/<pid>/cmdline, and a file would outlive the process that can honour it.
    //
    // The token is per launch, so a token copied out of a running process is useless
    // against the next one. The socket lives in the app's own profile directory at
    // mode 0600, which is the same protection the settings file has.
    if (this.bridge && this.bridge.socketPath && this.bridge.token) {
      env.ACTIONMESH_PROVIDER_BRIDGE_SOCKET = this.bridge.socketPath;
      env.ACTIONMESH_PROVIDER_BRIDGE_TOKEN = this.bridge.token;
      // The provider name goes across unconditionally. The *choice* is read live by the
      // bridge, per request, from the settings store — so switching providers in the UI
      // takes effect on the next message without restarting the backend, and revoking a
      // key takes effect just as immediately. Whether the planner is actually a model is
      // then a fact reported by /voiceos/status, not an assumption baked into a spawn.
      env.ACTIONMESH_VOICEOS_LLM_PROVIDER = 'desktop';
    }
    // C42: the browser drive bridge. Env only, same channel as the model
    // bridge: argv would leak in /proc/<pid>/cmdline, and a file would outlive
    // the process that can honour it. No API key crosses: the token is per
    // launch and the socket lives in the profile directory at mode 0600.
    if (this.browserBridge && this.browserBridge.socketPath && this.browserBridge.token) {
      env.ACTIONMESH_BROWSER_BRIDGE_SOCKET = this.browserBridge.socketPath;
      env.ACTIONMESH_BROWSER_BRIDGE_TOKEN = this.browserBridge.token;
    }
    return env;
  }

  async start() {
    if (this.child) return this.snapshot();
    const workspace = this.settings.workspaceRoot || this.dirs.workspace;
    // Before a port, before a process: may this copy own the store at all?
    try {
      takeClaim(this.dirs);
      this.claimed = true;
    } catch (error) {
      // A refused claim is a startup failure with a reason the UI can show, not a
      // quiet degradation: starting anyway is exactly what corrupts the chain.
      this.status = 'failed';
      this.lastError = error.message;
      throw error;
    }
    const port = await freePort();
    this.port = port;
    this.baseUrl = `http://127.0.0.1:${port}`;
    this.status = 'starting';
    this.lastError = '';

    const logPath = path.join(this.dirs.logs, 'backend.log');
    // A development session restarts the backend a lot, and a pipe is never closed
    // when the child dies of a signal we did not send. Rotate rather than let the log
    // grow to hundreds of megabytes; the tail of the previous run is what matters.
    try {
      if (fs.statSync(logPath).size > LOG_ROTATE_BYTES) {
        fs.renameSync(logPath, `${logPath}.1`);
      }
    } catch {
      /* no previous log */
    }
    const log = fs.createWriteStream(logPath, { flags: 'a' });
    log.write(`\n=== ${new Date().toISOString()} starting on ${this.baseUrl} ===\n`);

    const child = spawn(
      this.runtime.python,
      [...this.runtime.entry, '--host', '127.0.0.1', '--port', String(port)],
      {
        cwd: this.appRoot,
        env: this.env(port, workspace),
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    );
    this.child = child;
    child.stdout.pipe(log, { end: false });
    child.stderr.pipe(log, { end: false });
    child.on('exit', (code, signal) => {
      log.write(`=== exited code=${code} signal=${signal} ===\n`);
      // The streams are shared with a now-dead child; end them so the descriptor is
      // released. (Nothing else writes to this stream after the child exits.)
      log.end();
      // Only the child this instance is *currently* running may report on it. A
      // SIGKILL'd child exits after `stop()` has already resolved and `restart()`
      // has spawned its replacement, so an unguarded handler nulls the live child
      // and stamps "failed / SIGKILL" over a backend that is answering /ready.
      if (this.child !== child) return;
      this.child = null;
      if (this.status !== 'stopping') {
        this.status = 'failed';
        this.lastError = `backend exited (code ${code ?? 'none'}${signal ? `, ${signal}` : ''})`;
      } else {
        this.status = 'stopped';
      }
    });

    try {
      // 6: /ready is the gate. It reports the durable store and the audit chain, so
      // "ready" here means the state the app depends on actually answers.
      await this.waitReady(READY_TIMEOUT_MS);
      this.status = 'ready';
      this.startedAt = new Date().toISOString();
    } catch (error) {
      this.lastError = error.message;
      this.status = 'failed';
      await this.stop();
      throw error;
    }
    return this.snapshot();
  }

  async waitReady(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (!this.child) throw new Error(this.lastError || 'backend exited before ready');
      try {
        const response = await fetch(`${this.baseUrl}/ready`, {
          headers: { 'x-api-key': this.key },
          signal: AbortSignal.timeout(1500),
        });
        if (response.ok) {
          const body = await response.json();
          if (body && body.status === 'ready') return body;
        }
      } catch {
        /* not up yet */
      }
      await new Promise((r) => setTimeout(r, READY_POLL_MS));
    }
    throw new Error(`backend did not become ready within ${Math.round(timeoutMs / 1000)}s`);
  }

  async stop() {
    const child = this.child;
    if (!child) {
      this.status = 'stopped';
      this.releaseClaim();
      return;
    }
    this.status = 'stopping';
    await new Promise((resolve) => {
      const killTimer = setTimeout(() => {
        try {
          child.kill('SIGKILL');
        } catch {
          /* already gone */
        }
        resolve();
      }, 6000);
      child.once('exit', () => {
        clearTimeout(killTimer);
        resolve();
      });
      try {
        child.kill('SIGTERM');
      } catch {
        clearTimeout(killTimer);
        resolve();
      }
    });
    this.child = null;
    this.status = 'stopped';
    this.releaseClaim();
  }

  releaseClaim() {
    if (!this.claimed) return;
    this.claimed = false;
    clearClaim(this.dirs, process.pid);
  }

  async restart() {
    await this.stop();
    return this.start();
  }
}

function newLoopbackKey() {
  return crypto.randomBytes(32).toString('base64url');
}

module.exports = {
  Backend,
  newLoopbackKey,
  ensureDirs,
  chooseAgentLogDir,
  resolveRuntime,
  freePort,
  // Exported for the data-directory claim's own tests: the guard that stops a second
  // backend from corrupting one audit chain is worth testing without spawning one.
  takeClaim,
  clearClaim,
  readClaim,
  isAlive,
  os,
};