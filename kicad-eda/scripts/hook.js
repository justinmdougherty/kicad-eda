#!/usr/bin/env node
/**
 * Cross-platform hook dispatcher for kicad-eda. The old bash hooks broke on
 * Windows (a failing PreToolUse command DENIES the tool call), so every hook
 * routes through Node, which Claude Code guarantees.
 *
 *   node hook.js session-start        environment report (KiCad, Java, venv)
 *   node hook.js check-ipc            PreToolUse gate for IPC-API PCB tools
 *   node hook.js validate-schematic   PostToolUse .kicad_sch structure check
 *
 * Exit 0 = allow / OK. Exit 2 = block (stderr shown to the model).
 * Unknown state NEVER blocks — a broken probe must not brick the workflow.
 */
'use strict';
const { spawnSync, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { ROOT, VENV_PY, WIN } = require('./venv');

const mode = process.argv[2] || '';
const IPC_PROBE_TIMEOUT_MS = 8000;
// The probe imports the VENDORED kipy (server/vendor, built against the KiCad
// 11 protos) — the same copy the MCP server puts first on sys.path — never a
// pip-installed kicad-python that may be missing the schematic API.
const VENDOR_DIR = path.join(ROOT, 'server', 'vendor');
const IPC_PROBE = `import sys; sys.path.insert(0, ${JSON.stringify(VENDOR_DIR)}); `
  + 'from kipy import KiCad; k=KiCad(); print(k.get_version())';
const ENABLE_API_HINT = 'Preferences > Plugins and Content Manager > Enable KiCad API '
  + '(KiCad 9: Preferences > API)';

function tryRun(cmd, args) {
  const r = spawnSync(cmd, args, { encoding: 'utf8' });
  return r.status === 0 ? (r.stdout || '').trim() : null;
}

function findKicadCli() {
  const exe = WIN ? 'kicad-cli.exe' : 'kicad-cli';
  if (tryRun(exe, ['version']) !== null) return exe;
  const roots = [];
  if (process.env.KICAD_PATH) {
    roots.push(process.env.KICAD_PATH, path.join(process.env.KICAD_PATH, 'bin'));
  }
  if (WIN) {
    for (const base of [
      path.join(process.env.LOCALAPPDATA || '', 'Programs', 'KiCad'),
      path.join(process.env.ProgramFiles || 'C:\\Program Files', 'KiCad')]) {
      try {
        for (const v of fs.readdirSync(base).sort().reverse()) {
          roots.push(path.join(base, v, 'bin'));
        }
      } catch { /* not installed there */ }
    }
  } else {
    roots.push('/usr/bin', '/usr/local/bin',
               '/Applications/KiCad/KiCad.app/Contents/MacOS');
  }
  for (const r of roots) {
    const cand = path.join(r, exe);
    if (fs.existsSync(cand)) return cand;
  }
  return null;
}

function findJava() {
  if (spawnSync('java', ['-version']).status === 0) return true;
  const cands = [];
  if (process.env.JAVA_HOME) {
    cands.push(path.join(process.env.JAVA_HOME, 'bin', WIN ? 'java.exe' : 'java'));
  }
  // bundled JRE under server/tools/jre*/
  const tools = path.join(ROOT, 'server', 'tools');
  try {
    for (const d of fs.readdirSync(tools)) {
      if (!d.startsWith('jre')) continue;
      const stack = [path.join(tools, d)];
      while (stack.length) {
        const dir = stack.pop();
        for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
          if (e.isDirectory()) stack.push(path.join(dir, e.name));
          else if (e.name === (WIN ? 'java.exe' : 'java')) cands.push(path.join(dir, e.name));
        }
      }
    }
  } catch { /* no bundled jre */ }
  return cands.some((c) => fs.existsSync(c));
}

/**
 * Is a KiCad GUI process alive? true / false, or null when the probe itself
 * failed (unknown).
 */
function kicadProcessRunning() {
  try {
    if (WIN) {
      const out = execSync('tasklist /FI "IMAGENAME eq kicad.exe" /FO CSV /NH', { encoding: 'utf8' })
        + execSync('tasklist /FI "IMAGENAME eq pcbnew.exe" /FO CSV /NH', { encoding: 'utf8' });
      return /"(kicad|pcbnew)\.exe"/i.test(out);
    }
    // Process NAMES only (`comm`: bare name on Linux, full path on macOS) —
    // matching whole `ps ax` lines would hit this very hook, whose command
    // line contains the plugin dir ".../kicad-eda/".
    const out = execSync('ps -eo comm=', { encoding: 'utf8' });
    return /(^|\/)(kicad|pcbnew|eeschema)\s*$/im.test(out);
  } catch {
    return null;
  }
}

/**
 * Ask KiCad's IPC API for its version through the server venv (where kipy
 * lives). Returns 'ok', 'no-kipy' (module missing -> caller falls back to the
 * process check), 'unreachable' (KiCad answered nothing), or 'unknown' (the
 * probe itself could not run / timed out — never block on that).
 */
function probeIpc() {
  if (!fs.existsSync(VENV_PY)) return 'no-kipy';
  const r = spawnSync(VENV_PY, ['-c', IPC_PROBE],
                      { encoding: 'utf8', timeout: IPC_PROBE_TIMEOUT_MS });
  if (r.error) return 'unknown';
  if (r.status === 0) return 'ok';
  if (/ModuleNotFoundError/.test(r.stderr || '')) return 'no-kipy';
  return 'unreachable';
}

function blockMessage(running) {
  const why = running === false
    ? 'KiCad is not running.'
    : running === true
      ? `KiCad is running but its API server is not enabled (${ENABLE_API_HINT}).`
      : `KiCad's API server did not answer (is KiCad running with ${ENABLE_API_HINT}?).`;
  return 'BLOCKED: this tool needs KiCad\'s IPC API. ' + why
    + ' Open the board in KiCad with the API enabled and retry — or use the '
    + 'headless tools (create_pcb, build_board_from_netlist, autoroute_board, '
    + 'drc_pcb, export_fab_package), which do not need the GUI.\n';
}

if (mode === 'session-start') {
  const cli = findKicadCli();
  const kicad = cli ? (tryRun(cli, ['version']) || 'found') : null;
  const java = findJava();
  const lines = [];
  lines.push(kicad
    ? `kicad-eda: KiCad CLI ${kicad.split('\n')[0]}.`
    : 'kicad-eda: KiCad not found — run /kicad-eda:setup (schematic tools work, board build/DRC/Gerbers will not).');
  lines.push(java
    ? 'kicad-eda: Java present (Freerouting autorouter available).'
    : 'kicad-eda: Java not found — run /kicad-eda:setup to enable autorouting.');
  lines.push(fs.existsSync(VENV_PY)
    ? 'kicad-eda: server venv present.'
    : 'kicad-eda: server venv not created yet (it is built on first MCP server start).');
  process.stdout.write(lines.join('\n') + '\n');
  process.exit(0);
}

if (mode === 'check-ipc') {
  // The IPC-API tool family (live board edits) needs a running KiCad GUI with
  // the API server enabled. The headless pipeline (create_pcb /
  // build_board_from_netlist / autoroute_board / drc_pcb / export_fab_package)
  // and EVERY schematic tool (they run their own headless KiCad 11 api-server
  // or the file model; no GUI needed) do NOT go through here — keep the
  // hooks.json matcher limited to the live board tools.
  // Probe the API socket through the venv first; fall back to a process scan
  // when kipy is absent. Unknown = allow.
  const ipc = probeIpc();
  if (ipc === 'ok' || ipc === 'unknown') process.exit(0);
  const running = kicadProcessRunning();
  if (ipc === 'no-kipy') {
    // cannot ask the API; the process check is the best we have
    if (running === true || running === null) process.exit(0);
  }
  process.stderr.write(blockMessage(running));
  process.exit(2);
}

if (mode === 'validate-schematic') {
  // Read the hook event from stdin, hand it to the python checker if a
  // python is available; otherwise pass (never block on a missing dep).
  let input = '';
  try { input = fs.readFileSync(0, 'utf8'); } catch { process.exit(0); }
  const cands = WIN
    ? [[VENV_PY, []], ['py', ['-3']], ['python', []], ['python3', []]]
    : [[VENV_PY, []], ['python3', []], ['python', []]];
  for (const [cmd, args] of cands) {
    const probe = spawnSync(cmd, [...args, '--version'], { stdio: 'ignore' });
    if (probe.status !== 0) continue;
    const r = spawnSync(cmd, [...args, path.join(ROOT, 'scripts', 'validate-schematic.py')],
                        { input, encoding: 'utf8' });
    if (r.stderr) process.stderr.write(r.stderr);
    process.exit(r.status === 2 ? 2 : 0);
  }
  process.exit(0);
}

process.exit(0);
