# Project libraries — custom symbols and footprints

A part that KiCad's stock libraries do not have lives in a **project
library** next to the design: a `.kicad_sym` symbol library and a `.pretty`
footprint library that the netlist declares. The plugin authors them (part
presets or footprint templates, in a dialect every supported KiCad loads),
validates them with KiCad itself, resolves them in the same order at every
stage, and writes KiCad's own library tables so the schematic, ERC, DRC and
the board build all find them. The binding decision is D1 in
[ROADMAP_DECISIONS.md](ROADMAP_DECISIONS.md); the authoring how-to (presets,
templates, netlist hints) is
[skills/netlist-pipeline/libraries.md](../skills/netlist-pipeline/libraries.md).

## 1. The `libraries` block

```jsonc
"libraries": {
  "symbols":    {"SPS_parts": "libs/SPS_parts.kicad_sym"},
  "footprints": {"SPS_parts": "libs/SPS_parts.pretty"}
}
```

- Nickname -> path. A relative path is relative to the netlist.json's
  directory; `${KIPRJMOD}` is that directory; absolute paths work, and so do
  shared libraries outside the project (`../libs/shared.pretty`).
- A nickname is non-empty, without `:` or control characters. The block takes
  only `symbols` and `footprints` (the schema rejects any other key).
- Components use the nicknames like stock ones:
  `"lib_id": "SPS_parts:LG290P"`, `"footprint": "SPS_parts:Quectel_LG290P"`.
- Default home of authored parts: `libs/<name>_parts.kicad_sym` and
  `libs/<name>_parts.pretty`, nickname `<name>_parts`, where `<name>` is the
  netlist's `name`, else its file stem (unless that is `netlist`), else its
  folder name; a run of other characters than letters, digits, `_` and `-`
  becomes `_`. The library tools register what they author here themselves.

## 2. Resolution order: block > project tables > stock

Every consumer (lint, the schematic build, the design review, the board
build, the review report) resolves a nickname the same way
(`project_libs.load(netlist, design)`, `server/tools/libtable.py`):

1. the netlist `libraries` block;
2. the project `sym-lib-table` / `fp-lib-table` beside the netlist;
3. the stock library of the install the consumer uses
   (`<symbols>/<nick>.kicad_sym`, `<footprints>/<nick>.pretty`).

- A declared library never falls through to stock: when its file is missing
  that is an error (LIB-E2), not a silent stock pick.
- A disabled project-table row hides the nickname, as in KiCad.
- The same nickname in the block and in a project table with another path is
  an error (a D1 conflict, LIB-E2), and so is a block library whose project
  row is disabled. A nickname listed twice in one table is an error too
  (KiCad refuses such a table).
- `resolve_libraries(netlist)` shows the outcome for every nickname the design
  uses or declares: `source` (netlist | project | stock), `path`, `exists`,
  `generated` (authored by this plugin) and its `errors`.

## 3. The library stage, gate by gate

| Where | What happens |
|---|---|
| G1 | The harness feature `pipeline/rp_feature_libs.py` loads the libraries (again just before lint, in case a feature declared one). A library that does not load is a G1 blocker naming the nickname and path. Lint gets the project-library context, so the LIB-* rules (section 6) check them. |
| G2 | `build_schematic_from_netlist(netlist, ...)` refuses, before it writes or spawns anything, when the libraries do not load. It merges the libraries the design uses into the schematic directory's tables (managed rows, section 4) before KiCad 11's API creates the document (the api-server reads project tables only then) and before the file build; the file backend also writes a minimal `<stem>.kicad_pro` (kicad-cli ERC reads project tables only beside one). Pin sidecars come from `<netlist dir>/pins` first (section 5). The response carries `libraries` (each part's source), `library_tables` (the files written) and `fallback` (section 8). `design_report(netlist)` reads the pin types of project parts through the same resolver. |
| G3 | `build_board_from_netlist(netlist, ...)` refuses, before it spawns anything, when the libraries do not load. When the block or a project table declares a library, it merges the libraries the design uses into the board directory's tables with a minimal `<stem>.kicad_pro` (so kicad-cli DRC resolves the footprint ids), and a declared footprint library makes it hand the SWIG builder a library map (a temporary JSON) instead of the stock directory. The builder stamps the library nickname on every footprint it loads, stock ones included. A footprint its pcbnew cannot load fails the build with a message naming the .kicad_mod, its `(version N)`, the pcbnew build and the newest format that build loads (section 7). |
| Route cache | The footprint digest (section 5a) re-routes the board when a footprint file changes. |
| Review | The pipeline report's `libraries` and `review_report(netlist)` give every part's library source: stock | project | generated (authored by this plugin) | unknown. |

## 4. Managed table rows

The block is materialised into KiCad's own tables beside the generated
outputs (the schematic's and the board's directory): one row per declared
library the design uses, tagged `(descr "kicad-eda managed")`. A relative
path is rebased onto `${KIPRJMOD}` of that directory; an absolute one stays
absolute.

- Rows without that tag are never touched (they stay byte-for-byte in place).
  An unmanaged row with the same nickname is reused when it names the same
  library in the same state; otherwise the build stops with the conflict.
- A rerun writes nothing; a library the block no longer declares loses its
  managed row; without project libraries no table file is created.
- Tables are files only: the plugin never calls KiCad's library-table RPC and
  never edits the user's KiCad configuration (ROADMAP D0/D5).
- `resolve_libraries(netlist, materialize=True)` and the library tools
  (`register=True`, the default) write the tables beside the netlist itself.
  A GUI-flow project without a netlist can use a minimal
  `{"name": "<project>"}` netlist.json in the KiCad project folder: the
  tables then land beside the schematic, where KiCad and the schematic tools
  read them.

## 5. Pin sidecars

A sidecar maps pin names and numbers to schematic offsets. It is optional:
the schematic tools derive pin positions from the symbol itself (on the API
backend KiCad reports them), and on the file backend a sidecar overrides
them.

- Project parts: `<netlist dir>/pins/<nick>_<symbol>.pins.json`, written by
  `author_symbol(netlist, ...)` (`pins_sidecar=True`, the default) and by
  `build_pin_sidecars(netlist=...)` (out_dir defaults to `<netlist dir>/pins`,
  the symbols to the netlist's project-library parts).
- Stock parts: the plugin's central `server/pins/`. A project-library part is
  refused there (`build_pin_sidecars` names it and writes nothing).
- Search order on the file backend: the project's `pins/` (beside the
  netlist for `build_schematic_from_netlist`, beside the schematic for the
  interactive tools), then `server/pins/`.

## 5a. Route-hash footprint digest

The route cache (`<netlist>.cache.json`, [PIPELINE.md](PIPELINE.md) section 4)
keys on everything that moves copper. The libraries feature adds
`footprint_digest`: a sha256 over the sorted (lib_id, .kicad_mod bytes) of
every footprint the board build loads — the components' footprints and the
mounting-hole footprint, stock included — computed by
`project_libs.footprint_digest(libs, design)`. Editing one byte of a project
footprint re-routes the board; a footprint that resolves nowhere hashes as
missing. The `libraries` block itself is listed in `ROUTE_HASH_EXCLUDED`:
pointing a nickname at an identical copy of its library keeps the cache.

## 6. LIB-* lint codes

`server/tools/lint_rules_libs.py` (G1, right after the core rules):

| Code | Level | Finding |
|---|---|---|
| LIB-E1 | error | the project libraries did not load (a malformed block or table) |
| LIB-E2 | error | a used nickname resolves nowhere or to a disabled row; a block-vs-project conflict; a declared library file that is missing or of the wrong kind; an undefined `${VAR}` in a path |
| LIB-E3 | error | a symbol or footprint name missing from its resolved library, or an unreadable library (one error per lib_id, naming its users), including the `power:PWR_FLAG` symbol and the mounting-hole footprint the builds add |
| LIB-E4 | error | a netted pad key that is not a pad of the footprint (the board build matches pad NUMBERS; a pin name gets a hint naming its number), or a `no_connect` key that is neither a pad nor a pin |
| LIB-E5 | error | the listed pins of one stacked group (one spot of one unit) sit on different nets, or on a net and `no_connect`: KiCad joins stacked pins into one net |
| LIB-W1 | warning | symbol pins without a matching pad (once per symbol + footprint) |
| LIB-W2 | warning | a project footprint without an F.CrtYd / B.CrtYd courtyard |
| LIB-W3 | warning | the design has `height_zones` but a project footprint has no height (no 3D model, no Height property, no component `height`) |

The harness passes the project libraries to lint by itself. From the command
line, give the stock directories so the libraries resolve like the pipeline's:

```powershell
<venv>\python server\tools\lint_netlist.py <netlist.json> [symbols_dir] [footprints_dir]
```

Without a resolver (`lint_netlist.lint(design, symbols_dir)` from Python),
only LIB-E1, LIB-E3 and LIB-E4 run, against the stock libraries.

## 7. KiCad 9 / 10 file dialects

The board is built by the pcbnew Python of a KiCad 9 or 10 install (the
newest one that has it; KiCad 11 dropped those bindings), so every footprint
the board build loads must be in a dialect that pcbnew reads:

| pcbnew | loads footprints up to |
|---|---|
| KiCad 9.0 | `(version 20241229)` |
| KiCad 10.0 | `(version 20260206)` |

A KiCad 11 nightly writes `(version 20260901)`, which neither loads (and the
nightly has no pcbnew Python to build boards with). pcbnew silently returns no
footprint for a file that is too new; the builder turns that into the G3
failure of section 3.

- Authored parts are always written in KiCad 9's dialect: footprints
  `(version 20241229)`, symbols `(version 20241209)`. Every supported KiCad
  loads them, and `validate_library` checks them with the oldest installed
  kicad-cli plus the board builder's pcbnew. It holds every footprint to that
  floor: one above `(version 20241229)` is a problem even where KiCad 10
  would build it, because the KiCad 9 board build cannot.
- A library saved by KiCad 10 or 11 (a re-saved vendor download, a KiCad 11
  import) is converted into KiCad 9's dialect with
  `python server/tools/kicad_dialect.py fp <src.pretty> <dst.pretty> --check`
  (`sym <src.kicad_sym> <dst.kicad_sym> --check` for symbols). It refuses what
  KiCad 9 cannot express, naming the item; `--check` proves the result with
  KiCad 9's own kicad-cli (the same pad / pin table). Or author the part
  again with `author_footprint`.

## 8. KiCad 11 without global library tables

A KiCad 11 nightly that has never been opened has no global
`sym-lib-table`, so its schematic API finds no stock symbol. Nothing has to
be configured for the pipeline:

- `build_schematic_from_netlist(netlist, backend="auto")` (the default, and
  the harness's G2) then rebuilds the schematic on the file backend. The
  response's `fallback` is `{from: "api", to: "file", reason, decision}` and
  the decision (stage `G2`, source `auto`, reversible) is appended once to the
  netlist's `<stem>.decisions.jsonl`; the harness copies `fallback` into its
  report's `schematic` entry.
- `backend="api"` asked for by name does not fall back: the failure says why
  and how to get the tables.
- The plugin never edits the KiCad configuration. For the API backend, open
  KiCad 11 once, or copy `sym-lib-table` and `fp-lib-table` from
  `<install>/share/kicad/template` into its config directory. The interactive
  schematic tools on the API backend have no fallback; their error names the
  missing tables (`create_schematic(path, backend="file")` writes a KiCad
  9/10 file instead).

## 9. The tools

MCP tools (`kicad-generator` server). The library tools return
`{ok, ..., assumptions}`: `assumptions` are the defaults they took instead of
asking (D5 entries `{id, topic, value, rationale, confidence, override}`); a
refusal is `{ok: false, reason}` with nothing written.

| Tool | Does |
|---|---|
| `author_footprint(netlist, template, preset, params, name, library, register, overwrite, opt_in)` | Renders a footprint from a part preset or a template, validates it (oldest kicad-cli parse check + pcbnew FootprintLoad; a missing KiCad is a failure) in a temporary copy, then writes it into the project library; a different existing file is replaced only with `overwrite=True` (`state`: new / unchanged / replaced). A preset also returns `netlist_hints`, `strategy` and `pin_groups`. |
| `author_symbol(netlist, spec, preset, library, footprint, register, pins_sidecar, overwrite, opt_in)` | Writes a symbol (one pin per pad, pad number = pin number; power pins of one name and type stacked on the first) into the project library, validated by kicad-cli `sym upgrade`; returns `pin_groups`, `stacked` and the sidecar it wrote. `footprint` sets the Footprint property; a preset defaults it to its footprint in the project library holding it. |
| `validate_library(path, expected_pads)` | Validates a `.pretty`, a `.kicad_mod` or a `.kicad_sym` with KiCad itself (dialect, courtyard, silk, copper overlap, the kicad-cli re-save, pcbnew FootprintLoad); `expected_pads` is a count or `{name: count}`. |
| `resolve_libraries(netlist, materialize)` | Section 2's outcome per nickname; `materialize=True` also writes the managed rows. |
| `compare_footprints(a, b, netlist, tolerance)` | Pad-by-pad difference of two footprints (`nick:name` lib_ids or .kicad_mod paths): moved, resized, renumbered, missing pads. |
| `build_pin_sidecars(symbols, out_dir, symbols_dir, netlist)` | Section 5. Also a stock symbol-id check: it fails for a symbol stock lacks. |
| `record_decision(netlist, stage, question, decision, rationale, confidence, source, alternatives, reversible, override)` | Appends a decision (e.g. a part choice, stage `parts`) to `<stem>.decisions.jsonl`. |
| `review_report(netlist)` | The human review, with every part's library source. |

Python helpers (server/tools, stdlib only): `part_presets.preset_info(name)`
describes a preset; `lga_strategy.apply_hints(design, hints, ref="U1")`
places a preset's `netlist_hints` at the part and returns a new netlist.
