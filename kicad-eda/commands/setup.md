---
description: Check and install the external dependencies (KiCad, Java, Python) the kicad-eda pipeline needs, and report whether the optional KiCad 11 nightly (schematic API backend) is present.
---

Verify the kicad-eda environment and install anything missing.

1. Run the dependency checker for this platform:
   - Windows: `powershell -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/scripts/install-deps.ps1" -CheckOnly`
   - macOS/Linux: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/install-deps.sh" --check`
2. Report what is present and what is missing.
3. For anything missing, ask the user before installing (KiCad is a multi-GB
   download), then re-run the script WITHOUT the check flag to install.
4. The **KiCad 11 nightly is optional** and the script only reports it
   (`[ok]` / `[--]` line) — never install it on the user's behalf. Tell the
   user what it adds (KiCad builds the schematic through its own API, headless
   or live in the open KiCad 11 editor; without it the plugin writes the
   schematic file itself) and where it comes from:
   <https://downloads.kicad.org/kicad/windows/explore/nightlies> (Windows; it
   installs beside KiCad 10 as `Program Files\KiCad\10.99`). If the script
   warns that its `sym-lib-table` is missing, the nightly has never been
   opened: open KiCad 11 once, or copy `sym-lib-table` and `fp-lib-table` from
   `<install>\share\kicad\template` into `%APPDATA%\kicad\10.99`.
5. Python packages for the MCP server need no action — the server launcher
   builds its own venv on first start. If the `kicad-generator` MCP server
   shows as failed, restart Claude Code after Python is installed.
6. Finish by confirming the full chain: `kicad-cli version`, `java -version`,
   and that `${CLAUDE_PLUGIN_ROOT}/server/tools/freerouting-1.9.0.jar` exists —
   or call the `server_status` MCP tool, which reports every installed KiCad
   (`installs`), the kicad-cli / bundled Python in use, the schematic backend
   (`schematic_backend` "api" with `api_kicad_version`, or "file" with the
   reason), Java/Freerouting, IPC status, open documents and capabilities in
   one shot.
