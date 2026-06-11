#!/usr/bin/env node
/**
 * Cross-platform launcher for the kicad-generator MCP server.
 *
 * Claude Code is a Node app, so `node` is always available — but `python`
 * vs `python3` vs the py launcher is a per-OS lottery, and the server's
 * Python deps must live in a venv that survives plugin updates. This shim:
 *
 *   1. finds a system Python (python / python3 / py -3)
 *   2. creates a venv in the persistent data dir on first run
 *      (CLAUDE_PLUGIN_DATA if provided, else <plugin>/.venv)
 *   3. installs server/requirements.txt when it changes
 *   4. execs the venv Python on server/kicad_server.py with stdio inherited
 *      (MCP speaks over stdin/stdout — nothing else may print to stdout)
 */
'use strict';
const { spawnSync, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, '..');
const DATA = process.env.CLAUDE_PLUGIN_DATA || ROOT;
const VENV = path.join(DATA, '.venv');
const WIN = process.platform === 'win32';
const VENV_PY = path.join(VENV, WIN ? 'Scripts' : 'bin', WIN ? 'python.exe' : 'python');
const REQ = path.join(ROOT, 'server', 'requirements.txt');
const REQ_STAMP = path.join(VENV, 'requirements.installed.txt');

function findPython() {
  const candidates = WIN
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]];
  for (const [cmd, args] of candidates) {
    const r = spawnSync(cmd, [...args, '--version'], { stdio: 'ignore' });
    if (r.status === 0) return [cmd, args];
  }
  return null;
}

function ensureVenv() {
  if (fs.existsSync(VENV_PY)) return true;
  const py = findPython();
  if (!py) {
    process.stderr.write('kicad-eda: no Python 3 found (need python/python3/py). Install Python 3.10+.\n');
    return false;
  }
  const [cmd, args] = py;
  const r = spawnSync(cmd, [...args, '-m', 'venv', VENV], { stdio: ['ignore', 'ignore', 'inherit'] });
  return r.status === 0 && fs.existsSync(VENV_PY);
}

function ensureDeps() {
  let want = '';
  try { want = fs.readFileSync(REQ, 'utf8'); } catch { return true; }
  let have = '';
  try { have = fs.readFileSync(REQ_STAMP, 'utf8'); } catch { /* first run */ }
  if (want === have) return true;
  const r = spawnSync(VENV_PY, ['-m', 'pip', 'install', '--quiet', '-r', REQ],
                      { stdio: ['ignore', 'ignore', 'inherit'] });
  if (r.status === 0) { fs.writeFileSync(REQ_STAMP, want); return true; }
  process.stderr.write('kicad-eda: pip install failed; server may be missing deps.\n');
  return false;
}

if (!ensureVenv()) process.exit(1);
ensureDeps();

const server = spawn(VENV_PY, [path.join(ROOT, 'server', 'kicad_server.py')],
                     { stdio: 'inherit', env: process.env });
server.on('exit', (code) => process.exit(code === null ? 1 : code));
server.on('error', (err) => { process.stderr.write(String(err) + '\n'); process.exit(1); });
