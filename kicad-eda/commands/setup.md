---
description: Check and install the external dependencies (KiCad, Java, Python) the kicad-eda pipeline needs.
---

Verify the kicad-eda environment and install anything missing.

1. Run the dependency checker for this platform:
   - Windows: `powershell -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/install-deps.ps1" -CheckOnly`
   - macOS/Linux: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/install-deps.sh" --check`
2. Report what is present and what is missing.
3. For anything missing, ask the user before installing (KiCad is a multi-GB
   download), then re-run the script WITHOUT the check flag to install.
4. Python packages for the MCP server need no action — the server launcher
   builds its own venv on first start. If the `kicad-generator` MCP server
   shows as failed, restart Claude Code after Python is installed.
5. Finish by confirming the full chain: `kicad-cli version`, `java -version`,
   and that `${CLAUDE_PLUGIN_ROOT}/server/tools/freerouting-1.9.0.jar` exists.
