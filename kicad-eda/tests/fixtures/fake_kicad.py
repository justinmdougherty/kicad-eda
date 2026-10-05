"""Fakes and samples for the foreign-import tests (test_foreign_formats.py,
test_foreign_import.py; docs/ROADMAP_DECISIONS.md D4, F6).

* FAKE_CLI: a fake kicad-cli (a Python script behind a .cmd / shell launcher,
  so every call is a REAL subprocess) in three flavours that answer `--help`
  the way KiCad 9.0.4, 10.0.5 and the 10.99 nightly do, and import / convert
  by writing small KiCad-shaped files, JSON reports and console lines (only
  the nightly flavour writes <stem>-import-fps.pretty and an fp-lib-table, as
  the real 10.0.5 `pcb import` writes neither; like 10.0.5, the k10 flavour
  writes <stem>.kicad_dru beside an Eagle input). FAKE_* markers inside an
  input file steer it: FAKE_CRASH, FAKE_UNKNOWN, FAKE_NO_OUTPUT,
  FAKE_ERRORS=n (console Error: lines), FAKE_REPORT_ERROR, FAKE_BAD_REPORT
  (a report that is not JSON), FAKE_BAD_TABLE (an unreadable fp-lib-table),
  FAKE_FOOTPRINTS=n / FAKE_CLAIM=n (a report count that disagrees with the
  file), FAKE_SYMBOLS=n, FAKE_FP_EMPTY=n / FAKE_FP_BARE=n (schematic
  Footprint fields left empty / unqualified), FAKE_ITEMS=n. Every call is
  appended to $FAKE_KICAD_LOG as a JSON list.
* STUB_PCBNEW: a stub pcbnew for running swig_load_foreign.py under the venv
  Python. It records whether kicad_quiet was imported first, and
  $STUB_PCBNEW_CFG (JSON: version, copper, footprints, zones=[{layers, net,
  rule_area}]) makes it a 9.x or 10.x pcbnew with the zones the
  inner_plane_loss / zones_on_disabled_layers diagnostics read. fake_swig
  instead replaces the whole swig_load_foreign child by a result line with
  chosen F46-03a diagnostics (e.g. the exact inner_plane_loss of a real
  KiCad 9 Altium import).
* Sample writers for Eagle / Altium / EasyEDA Std / EasyEDA Pro / .PrjPcb
  inputs, and the live-tier fixtures: discovered installs and the real sample
  files.

Live samples come ONLY from $KICAD_EDA_FOREIGN_SAMPLES (unset or a file
missing -> the test skips; samples are never downloaded and, being
third-party CC-BY-SA designs, never committed). Expected layout below it:

    eagle/Qwiic_Button.brd, eagle/Qwiic_Button.sch
    foreign/altium/eDP_adapter_dvt1.PcbDoc, foreign/altium/power.SchDoc,
    foreign/altium/it6251_core.SchDoc, foreign/altium/eDP_adapter_dvt1.PrjPcb
    foreign/easyeda/pcb_watch.json, foreign/easyeda/sch_watch.json
    foreign/eepro/yuzuki.epro                      (EasyEDA Pro project)
    foreign/Qwiic_Button-Panel.*                   (Gerbers of the panel)
    adafruit/Adafruit Feather M0 RFMxx.brd, adafruit/Adafruit Feather M0 RFMxx.sch
    libs/SparkFun-IC-Memory.lbr, libs/ESP32-WROOM-32.PcbLib,
    libs/Sensor_Distance.lib, libs/LS2K0300_Footprint.elibz2

Plain factories (not pytest fixtures): each test module wraps the ones it
needs in its own small fixtures.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from typing import Callable

import pytest

import foreign_formats as ff
import kicad_env as env

SAMPLES_ENV = "KICAD_EDA_FOREIGN_SAMPLES"


def samples_dir() -> Path | None:
    """$KICAD_EDA_FOREIGN_SAMPLES as a Path; None when it is unset or blank."""
    root = os.environ.get(SAMPLES_ENV, "").strip()
    return Path(root) if root else None


SAMPLES = samples_dir()          # snapshot at import; sample() re-reads the variable

# ---------------------------------------------------------------------------
# A fake kicad-cli: flavour k9 | k10 | k11, behaviour driven by FAKE_* markers
# inside the input file. Every call is appended to $FAKE_KICAD_LOG as JSON.
# ---------------------------------------------------------------------------
FAKE_CLI = r'''
import json, os, re, sys
from pathlib import Path

FLAVOR, ARGS = sys.argv[1], sys.argv[2:]
VERSION = {"k9": "9.0.4", "k10": "10.0.5", "k11": "10.99.0-4025-gfake"}[FLAVOR]
BOARD_VER = {"k9": 20241229, "k10": 20260206, "k11": 20260901}[FLAVOR]
GEN_VER = {"k9": "9.0", "k10": "10.0", "k11": "10.99"}[FLAVOR]
FP_VER = {"k9": 20241229, "k10": 20260206, "k11": 20260901}[FLAVOR]
SYM_VER = {"k9": 20241209, "k10": 20251024, "k11": 20260830}[FLAVOR]
log = os.environ.get("FAKE_KICAD_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps([FLAVOR] + ARGS) + "\n")

TOP = {"k9": "Usage: kicad-cli [--version] [--help] {fp,jobset,pcb,sch,sym,version}",
       "k10": "Usage: kicad-cli [--version] [--help] {fp,jobset,pcb,sch,sym,version}",
       "k11": "Usage: kicad-cli [--version] [--help] {api-server,fp,gerber,import,jobset,"
              "mergetool,pcb,sch,sym,version}"}[FLAVOR]
PCB_HELP = ("Usage: pcb import [--help] [--output OUTPUT_FILE] [--format FORMAT] "
            "[--report-format FORMAT] [--report-file FILE]%s INPUT_FILE\n\n"
            "Import a non-KiCad PCB file to KiCad format\n\n"
            "  --format         Input format hint: auto, pads, altium, eagle, cadstar, "
            "fabmaster, pcad, solidworks (default: auto) [nargs=0..1] [default: \"auto\"]\n"
            "  --report-format  Import report format: none, json, text (default: none)\n%s")
SCH_HELP = ("Usage: sch import [--help] [--output OUTPUT_FILE] [--format FORMAT] "
            "[--report-format FORMAT] [--report-file FILE] INPUT_FILE\n\n"
            "  --format         Input format hint: auto, altium, eagle, cadstar, easyeda, "
            "easyedapro, ltspice, pads, diptrace, pcad, orcad (default: auto) [nargs=0..1]\n"
            "  --report-format  Import report format: none, json, text (default: none)\n")


def out(text, code=0, err=""):
    if text:
        print(text)
    if err:
        print(err, file=sys.stderr)
    sys.exit(code)


def opt(name, default=""):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


def helptext(words):
    if FLAVOR == "k9" and words in (["import"], ["pcb", "import"], ["sch", "import"]):
        parent = {"import": TOP}.get(words[0], "Usage: %s [--help] {drc,export,render}" % words[0])
        out("Failed to parse 'import', did you mean '%s'\n%s" %
            ("fp" if words == ["import"] else "export", parent), 1)
    if FLAVOR == "k9" and words[1:] == ["upgrade"]:
        out("input: 1 argument(s) expected. 0 provided.\nUsage: upgrade [--help] "
            "[--output OUTPUT_DIR] [--force] INPUT_DIR\n\nUpgrades the library", 1)
    if FLAVOR == "k10" and words == ["import"]:
        out("Failed to parse 'import', did you mean 'fp'\n" + TOP, 1)
    if FLAVOR == "k10" and words == ["sch", "import"]:
        out("Usage: kicad-cli sch [--help] {erc,export,upgrade}\n\nSchematics", 0)
    if words == ["import"]:
        out("Usage: kicad-cli import [--help] [--output PROJECT] [--layer-map FILE] "
            "INPUT_FILES...\n\nImport non-KiCad board and/or schematic files")
    if words == ["pcb", "import"]:
        lm = FLAVOR == "k11"
        out(PCB_HELP % (" [--layer-map FILE]" if lm else "",
                        "  --layer-map      JSON file mapping source layer names\n" if lm else ""))
    if words == ["sch", "import"]:
        out(SCH_HELP)
    if words[1:] == ["upgrade"]:
        out("Usage: %s upgrade [--help] [--output OUTPUT_DIR] [--force] INPUT_FILE_OR_DIR"
            % words[0])
    out("Failed to parse '%s'\n%s" % (words[-1], TOP), 1)


def markers(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text, dict(re.findall(r"FAKE_(\w+)=(\w+)", text))


def report(path, data, text=""):
    if opt("--report-format") == "json":
        body = "{not json" if "FAKE_BAD_REPORT" in text else json.dumps(data)
        Path(opt("--report-file")).write_text(body, encoding="utf-8")


def import_library(dst, nick, text):
    """The nightly's library-form footprint library + project fp-lib-table
    (kicad-cli 10.0.5 `pcb import` writes neither)."""
    lib = dst.parent / (nick + ".pretty")
    lib.mkdir(exist_ok=True)
    (lib / "R_0.kicad_mod").write_text('(footprint "R_0"\n\t(version %d)\n)\n' % FP_VER)
    table = ("(fp_lib_table (lib (name broken" if "FAKE_BAD_TABLE" in text else
             '(fp_lib_table\n\t(version 7)\n\t(lib (name "%s") (type "KiCad") (uri '
             '"${KIPRJMOD}/%s.pretty") (options "kicad_import_cache=1") (descr ""))\n)\n'
             % (nick, nick))
    (dst.parent / "fp-lib-table").write_text(table, encoding="utf-8")


def pcb_import():
    src, dst = Path(ARGS[-1]), Path(opt("-o"))
    text, m = markers(src)
    print("Importing '%s' using %s format..." % (src, opt("--format")))
    if "FAKE_CRASH" in text:
        out("", 127, "Unhandled exception class: std::bad_function_call")
    if "FAKE_UNKNOWN" in text:
        out("No PCB importer recognizes the file format of '%s'" % src, 3)
    n = int(m.get("FOOTPRINTS", 3))
    nick = src.stem + "-import-fps"
    if FLAVOR == "k10" and opt("--format") == "eagle":     # 10.0.5: <stem>.kicad_dru by the source
        src.with_suffix(".kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    for i in range(int(m.get("ERRORS", 0))):
        print("18:00:%02d: Error: Unknown layer %d in footprint" % (i, i), file=sys.stderr)
    if "FAKE_NO_OUTPUT" not in text:
        fps = "".join('\t(footprint "%s:R_%d"\n\t\t(layer "F.Cu")\n\t)\n' % (nick, i)
                      for i in range(n))
        dst.write_text('(kicad_pcb\n\t(version %d)\n\t(generator "pcbnew")\n'
                       '\t(generator_version "%s")\n%s)\n' % (BOARD_VER, GEN_VER, fps),
                       encoding="utf-8")
        if FLAVOR == "k11":
            import_library(dst, nick, text)
        print("Successfully saved imported board to '%s'" % dst)
    claimed = int(m.get("CLAIM", n))
    report(dst, {"errors": ["report error"] if "FAKE_REPORT_ERROR" in text else [],
                 "warnings": [], "output_file": dst.name, "source_file": src.name,
                 "source_format": opt("--format"),
                 "statistics": {"footprints": claimed, "tracks": 7, "vias": 2, "zones": 1},
                 "layer_mapping": {"Top": {"kicad_layer": "F.Cu", "method": "auto"},
                                   "Bottom": {"kicad_layer": "B.Cu", "method": "auto"},
                                   "tKeepout": {"kicad_layer": "F.CrtYd", "method": "auto"}}},
           text)
    sys.exit(0)


def footprint_field(i, m, stem):
    """Footprint field of placed symbol i: FAKE_FP_EMPTY empty ones first, then
    FAKE_FP_BARE unqualified ones, then '<stem>:R_0603'."""
    empty, bare = int(m.get("FP_EMPTY", 0)), int(m.get("FP_BARE", 0))
    return "" if i < empty else "R_0603" if i < empty + bare else stem + ":R_0603"


def sch_import():
    src, dst = Path(ARGS[-1]), Path(opt("-o"))
    text, m = markers(src)
    print("Importing '%s' using %s format..." % (src, opt("--format")))
    n = int(m.get("SYMBOLS", 4))
    for i in range(int(m.get("ERRORS", 0))):
        print("17:00:48: Error: No library symbol found for schematic symbol 'R%dP 10k'." % i,
              file=sys.stderr)
    print("16:58:58: Setting schematic symbol 'R1 10k' library identifier to 'x:R'.",
          file=sys.stderr)
    lib_symbols = ('\t(lib_symbols\n\t\t(symbol "%s-import:R"\n\t\t\t(property "Footprint" "")'
                   '\n\t\t\t(property "Description" "a (paren) in a string")\n\t\t)\n\t)\n'
                   % dst.stem)
    syms = "".join('\t(symbol\n\t\t(lib_id "%s-import:R")\n\t\t(property "Footprint" '
                   '"%s")\n\t)\n' % (dst.stem, footprint_field(i, m, dst.stem))
                   for i in range(n))
    dst.write_text('(kicad_sch\n\t(version 20260830)\n\t(generator "eeschema")\n%s%s)\n'
                   % (lib_symbols, syms), encoding="utf-8")
    sheets = 1
    if opt("--format") == "easyedapro":
        (dst.parent / "1_POWER.kicad_sch").write_text(
            '(kicad_sch\n\t(version 20260830)\n\t(symbol\n\t\t(lib_id "p:C")\n\t)\n)\n')
        sheets, n = 2, n + 1
    if opt("--format") == "eagle":
        (dst.parent / (dst.stem + "-eagle-import.kicad_sym")).write_text("(kicad_symbol_lib)")
        (dst.parent / "sym-lib-table").write_text(
            '(sym_lib_table\n\t(version 7)\n\t(lib (name "%s-eagle-import") (type "KiCad") '
            '(uri "${KIPRJMOD}/%s-eagle-import.kicad_sym") (options "") (descr ""))\n)\n'
            % (dst.stem, dst.stem), encoding="utf-8")
    print("Successfully saved imported schematic to '%s'" % dst)
    report(dst, {"errors": [], "warnings": [], "output_file": dst.name,
                 "source_file": src.name, "source_format": opt("--format"),
                 "statistics": {"renamed_board_nets": 0, "sheets": sheets, "symbols": n}})
    sys.exit(0)


def upgrade(kind):
    src, dst = Path(ARGS[-1]), Path(opt("-o"))
    if dst.exists():
        out("Output path must not conflict with existing path", 4)
    if not dst.parent.is_dir():
        out("", 2, "18:40:10: Error: Directory '%s' couldn't be created\nUnable to convert library"
            % dst)
    text, m = markers(src)
    if FLAVOR == "k9" and src.suffix.lower() == ".elibz2":
        out("Unable to convert library", 2)
    n = int(m.get("ITEMS", 2))
    if kind == "fp":
        dst.mkdir()
        for i in range(n):
            (dst / ("FP_%d.kicad_mod" % i)).write_text(
                '(footprint "FP_%d"\n\t(version %d)\n\t(generator "pcbnew")\n)\n' % (i, FP_VER))
    else:
        body = "".join('\t(symbol "S_%d"\n\t\t(symbol "S_%d_0_1")\n\t)\n' % (i, i)
                       for i in range(n))
        dst.write_text('(kicad_symbol_lib\n\t(version %d)\n%s)\n' % (SYM_VER, body))
    sys.exit(0)


if ARGS == ["version"]:
    out(VERSION)
if ARGS and ARGS[-1] in ("--help", "-h"):
    helptext(ARGS[:-1])
if ARGS[:2] == ["pcb", "import"]:
    pcb_import()
if ARGS[:2] == ["sch", "import"] and FLAVOR == "k11":
    sch_import()
if ARGS[:2] in (["fp", "upgrade"], ["sym", "upgrade"]):
    upgrade(ARGS[0])
out("Failed to parse '%s'\n%s" % (ARGS[0] if ARGS else "", TOP), 1)
'''

VERSIONS = {"k9": ("9.0.4", (9, 0, 4)), "k10": ("10.0.5", (10, 0, 5)),
            "k11": ("10.99.0-4025-gfake", (10, 99, 0))}

STUB_PCBNEW = '''\
import json
import os
import sys

QUIET_FIRST = "kicad_quiet" in sys.modules
CALLS = os.environ.get("STUB_PCBNEW_CALLS")
CFG = json.loads(os.environ.get("STUB_PCBNEW_CFG") or "{}")
VERSION = CFG.get("version", "9.0.4")
K10 = not VERSION.startswith("9.")
F_Cu, B_Cu, Eco1_User = 0, 2, 21
NAMES = {0: "F.Cu", 2: "B.Cu", 21: "Eco1.User"}
for _k in range(1, 31):
    globals()["In%d_Cu" % _k] = 2 * _k + 2
    NAMES[2 * _k + 2] = "In%d.Cu" % _k
IDS = {name: layer for layer, name in NAMES.items()}


def _log(*parts):
    if CALLS:
        with open(CALLS, "a", encoding="utf-8") as f:
            f.write(" ".join(str(p) for p in parts) + "\\n")


class PCB_IO_MGR:                       # 10.0 inserted ALLEGRO=3 and shifted the rest
    KICAD_SEXP = 1
    ALTIUM_DESIGNER, EAGLE, EASYEDA, EASYEDAPRO = (6, 8, 9, 10) if K10 else (5, 7, 8, 9)


def Version():
    return VERSION


def IsCopperLayer(layer):
    return layer in (0, 2) or (4 <= layer <= 62 and layer % 2 == 0)


class _Item:
    def __init__(self, cls):
        self._cls = cls

    def GetClass(self):
        return self._cls


class _LSet:
    def __init__(self, layers):
        self._layers = layers

    def Seq(self):
        return tuple(self._layers)


class _Zone:
    def __init__(self, spec):
        self._spec = spec

    def GetIsRuleArea(self):
        return bool(self._spec.get("rule_area"))

    def GetLayerSet(self):
        return _LSet([IDS[name] for name in self._spec["layers"]])

    def GetNetname(self):
        return self._spec.get("net", "")


class BOARD:
    def __init__(self, n):
        self.n = n

    @staticmethod
    def GetStandardLayerName(layer):
        return NAMES[layer]

    def GetFootprints(self):
        return [object()] * self.n

    def GetTracks(self):
        return [_Item("PCB_TRACK")] * 4 + [_Item("PCB_VIA")] * 2

    def GetNetCount(self):
        return 7

    def GetAreaCount(self):
        return len(CFG.get("zones", []))

    def GetCopperLayerCount(self):
        return CFG.get("copper", 2)

    def Zones(self):
        return [_Zone(z) for z in CFG.get("zones", [])]


def LoadBoard(path, kind=None):
    _log("LoadBoard", os.path.basename(path), kind, "quiet_first=%s" % QUIET_FIRST)
    text = open(path, encoding="utf-8", errors="replace").read()
    if "FAKE_NONE" in text:
        return None
    if "FAKE_RAISE" in text:
        raise OSError("Error: cannot read the board file")
    print("12:00:00: Altium layer Internal Plane 1 has no KiCad equivalent.", file=sys.stderr)
    return BOARD(CFG.get("footprints", 5))


def SaveBoard(path, board):
    _log("SaveBoard", os.path.basename(path))
    fps = "".join('\\t(footprint "lib:F%d"\\n\\t)\\n' % i for i in range(board.n))
    version, generator = ("20260206", "10.0") if K10 else ("20241229", "9.0")
    with open(path, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb\\n\\t(version %s)\\n\\t(generator "pcbnew")\\n'
                '\\t(generator_version "%s")\\n' % (version, generator) + fps + ')\\n')
    with open(path[:-len(".kicad_pcb")] + ".kicad_pro", "w", encoding="utf-8") as f:
        f.write("{}")
    return True
'''


def launcher(directory: Path, name: str, script: Path, flavor: str) -> Path:
    """An executable `name` that runs `script flavor <args>` with this Python."""
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{script}" {flavor} %*\r\n'
                        f'exit /b %ERRORLEVEL%\r\n', encoding="utf-8")
        return path
    path = directory / name
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {flavor} "$@"\n',
                    encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def log_cli_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $FAKE_KICAD_LOG at a fresh file; every fake kicad-cli call lands there."""
    log = tmp_path / "cli_calls.jsonl"
    monkeypatch.setenv("FAKE_KICAD_LOG", str(log))
    return log


def read_calls(log: Path) -> list[list[str]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def work_calls(log: Path, *words: str) -> list[list[str]]:
    """Calls of `kicad-cli <words> ...` that did work (capability probes excluded)."""
    return [c for c in read_calls(log)
            if c[1:1 + len(words)] == list(words) and "--help" not in c]


def stub_pcbnew_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(python, calls log): the venv Python with a stub pcbnew on PYTHONPATH
    (children inherit it); the stub appends its LoadBoard/SaveBoard calls.
    It is a pcbnew 9.0.4 with a 2-layer board and no zones until
    configure_stub() says otherwise."""
    stub = tmp_path / "stub_pcbnew"
    stub.mkdir()
    (stub / "pcbnew.py").write_text(STUB_PCBNEW, encoding="utf-8")
    calls = tmp_path / "pcbnew_calls.txt"
    monkeypatch.setenv("PYTHONPATH", str(stub))
    monkeypatch.setenv("STUB_PCBNEW_CALLS", str(calls))
    monkeypatch.delenv("STUB_PCBNEW_CFG", raising=False)
    return Path(sys.executable), calls


def configure_stub(monkeypatch: pytest.MonkeyPatch, **cfg: object) -> None:
    """Steer the stub pcbnew of the next children: version ("9.0.4" | "10.0.5"),
    copper (layer count), footprints, zones ([{layers, net, rule_area}])."""
    monkeypatch.setenv("STUB_PCBNEW_CFG", json.dumps(cfg))


def read_stub_calls(calls: Path) -> str:
    return calls.read_text(encoding="utf-8") if calls.is_file() else ""


def fake_swig(monkeypatch: pytest.MonkeyPatch, write_board: bool = True,
              **result: object) -> None:
    """Replace the swig_load_foreign.py child (foreign_formats.run_child) by
    one that prints the script's result line with `result` (the F46-03a
    fields, e.g. inner_plane_loss) and writes a D0 board unless told not to;
    every other child still runs for real."""
    real_run = ff.run_child

    def run(argv, timeout_s, cwd=None):
        if not str(argv[1]).endswith("swig_load_foreign.py"):
            return real_run(argv, timeout_s, cwd)
        if write_board:
            Path(argv[3]).write_text('(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
                                     '\t(generator_version "9.0")\n)\n', encoding="utf-8")
        line = {"ok": True, "plugin": argv[4], "input": argv[2], "output": argv[3],
                "pcbnew": "9.0.4", "footprints": 0, "tracks": 0, "vias": 0, "nets": 0,
                "zones": 0, "copper_layers": 4, "importer_side_files": [],
                "zones_on_disabled_layers": {}, "inner_plane_loss": None, **result}
        return env.ProbeResult(0, json.dumps(line) + "\n", "")

    monkeypatch.setattr(ff, "run_child", run)


def fake_installer(tmp_path: Path) -> Callable[..., env.KiCadInstall]:
    """make("k9" | "k10" | "k11", python=None) -> an env.KiCadInstall whose
    kicad-cli is the fake of that flavour (python: a bundled-Python stand-in)."""
    script = tmp_path / "fake_kicad_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")

    def make(flavor: str, python: Path | None = None) -> env.KiCadInstall:
        bin_dir = tmp_path / f"kicad_{flavor}" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        cli = launcher(bin_dir, "kicad-cli", script, flavor)
        version, vt = VERSIONS[flavor]
        return env.KiCadInstall(version=version, version_tuple=vt, root=bin_dir.parent,
                                kicad_cli=cli, python=python)
    return make


# --- sample design files for the unit tier -----------------------------------
EAGLE_HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n' \
             '<eagle version="9.5.2">\n<drawing>\n'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def eagle_brd(d: Path, extra: str = "", name: str = "Qwiic_Button") -> Path:
    return write(d / f"{name}.brd", EAGLE_HEAD + "<board/>\n" + extra + "</drawing></eagle>\n")


def eagle_sch(d: Path, extra: str = "", name: str = "Qwiic_Button") -> Path:
    return write(d / f"{name}.sch", EAGLE_HEAD + "<schematic/>\n" + extra +
                  "</drawing></eagle>\n")


def altium(d: Path, name: str, extra: str = "") -> Path:
    return write(d / name, "\xd0\xcf\x11\xe0 fake OLE " + extra)


def easyeda_std(d: Path, name: str, doctype: str, extra: str = "") -> Path:
    head = {"head": {"docType": doctype}} if doctype == "3" else {"docType": doctype}
    return write(d / name, json.dumps(head, indent=2) + "\n" + extra)


def easyeda_pro(d: Path, name: str = "yuzuki.epro", board: bool = True,
                sheets: bool = True) -> Path:
    path = d / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SHEET/", "")
        z.writestr("PCB/", "")
        if sheets:
            z.writestr("SHEET/abc/1.esch", "sheet")
        if board:
            z.writestr("PCB/def.epcb", "board")
    return path


def prjpcb(d: Path) -> Path:
    return write(d / "eDP_adapter_dvt1.PrjPcb",
                  "[Design]\nVersion=1.0\n\n[Document1]\nDocumentPath=power.SchDoc\n"
                  "AnnotationEnabled=1\n\n[Document2]\nDocumentPath=it6251_core.SchDoc\n\n"
                  "[Document3]\nDocumentPath=eDP_adapter_dvt1.PcbDoc\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Live tier: real KiCad installs and the sample files on disk (skip if absent)
# ---------------------------------------------------------------------------
def nightly_install(installs: list[env.KiCadInstall]) -> env.KiCadInstall:
    found = next((i for i in installs if i.version_tuple >= (10, 99, 0)), None)
    if found is None:
        pytest.skip("no KiCad 11 nightly (kicad-cli 10.99+) installed")
    return found


def kicad9_install(installs: list[env.KiCadInstall]) -> env.KiCadInstall:
    found = next((i for i in installs if i.version_tuple[0] == 9 and i.python), None)
    if found is None:
        pytest.skip("no KiCad 9 install with a pcbnew Python")
    return found


def sample(*parts: str) -> Path:
    """A sample file under $KICAD_EDA_FOREIGN_SAMPLES (layout: module docstring);
    skips when the variable is unset or the file is missing."""
    root = samples_dir()
    if root is None:
        pytest.skip(f"${SAMPLES_ENV} is not set (sample files are never downloaded by tests)")
    path = root.joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"sample file missing (never downloaded by tests): {path}")
    return path
