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

const ROOT = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, '..');
const WIN = process.platform === 'win32';
const mode = process.argv[2] || '';

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
  process.stdout.write(lines.join('\n') + '\n');
  process.exit(0);
}

if (mode === 'check-ipc') {
  // The IPC-API tool family needs a running KiCad GUI. The headless pipeline
  // (build_board_from_netlist / autoroute_board / export_fab_package) does
  // NOT go through here. Best-effort process probe; unknown = allow.
  let running = false;
  try {
    const out = WIN
      ? execSync('tasklist /FI "IMAGENAME eq kicad.exe" /FO CSV /NH', { encoding: 'utf8' })
        + execSync('tasklist /FI "IMAGENAME eq pcbnew.exe" /FO CSV /NH', { encoding: 'utf8' })
      : execSync('ps ax', { encoding: 'utf8' });
    running = /kicad|pcbnew/i.test(out);
  } catch { process.exit(0); }
  if (running) process.exit(0);
  process.stderr.write(
    'BLOCKED: this tool talks to KiCad\'s IPC API, but KiCad does not appear '
    + 'to be running. Open the board in KiCad with Preferences > API enabled '
    + 'and retry — or use the headless tools (build_board_from_netlist, '
    + 'autoroute_board, export_fab_package), which do not need the GUI.\n');
  process.exit(2);
}

if (mode === 'validate-schematic') {
  // Read the hook event from stdin, hand it to the python checker if a
  // python is available; otherwise pass (never block on a missing dep).
  let input = '';
  try { input = fs.readFileSync(0, 'utf8'); } catch { process.exit(0); }
  const cands = WIN
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]];
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
