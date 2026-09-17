#!/usr/bin/env node
/**
 * Shared venv locations for the kicad-eda plugin scripts.
 *
 * The MCP server's Python deps live in a venv that survives plugin updates:
 * CLAUDE_PLUGIN_DATA/.venv when Claude Code provides a data dir, else
 * <plugin>/.venv. launch-server.js creates it; hook.js probes KiCad's IPC API
 * through it (kipy is installed there, not in the system Python).
 */
'use strict';
const { spawnSync } = require('child_process');
const path = require('path');

const ROOT = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, '..');
const DATA = process.env.CLAUDE_PLUGIN_DATA || ROOT;
const VENV = path.join(DATA, '.venv');
const WIN = process.platform === 'win32';
const VENV_PY = path.join(VENV, WIN ? 'Scripts' : 'bin', WIN ? 'python.exe' : 'python');

/** First working system Python 3 as [cmd, argsPrefix], or null. */
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

module.exports = { ROOT, DATA, VENV, VENV_PY, WIN, findPython };
