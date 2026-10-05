"""Regenerate the kimport fixture project (tests/test_kicad_import.py).

Not part of the test run: a maintainer tool, kept so the committed files can
be rebuilt and audited. It needs the plugin venv and KiCad 9.0 (symbols,
footprints, kicad-cli and the bundled Python with pcbnew)::

    .venv/Scripts/python.exe tests/fixtures/kicad/make_kimport.py [--xml-only] [kicad9_root]

Steps: sch_build turns DESIGN into kimport.kicad_sch (KiCad 9 format, every
symbol embedded); the schematic is then edited the way a designer would (C2
DNP, R2 excluded from the BOM, TP1 excluded from the board, LCSC / MPN /
Tolerance fields); KiCad 9's kicad-cli exports kimport.xml (export_xml); and
make_kimport_board.py builds kimport.kicad_pcb with KiCad 9's pcbnew, every
pad on the net the XML gives it. ``--xml-only`` redoes just the XML export
from the committed schematic. Self-made, so no third-party licence applies
to these files.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = HERE / "kimport"
STEM = "kimport"

DESIGN = {
    "name": STEM,
    "components": [
        {"ref": "U1", "lib_id": "Amplifier_Operational:LM358", "value": "LM358",
         "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"},
        {"ref": "J1", "lib_id": "Connector_Generic:Conn_01x06", "value": "IO",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"},
        {"ref": "J2", "lib_id": "Connector_Generic:Conn_01x02", "value": "AUX",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"},
        {"ref": "C1", "lib_id": "Device:C", "value": "100nF",
         "footprint": "Capacitor_SMD:C_0603_1608Metric"},
        {"ref": "C2", "lib_id": "Device:C", "value": "1uF",
         "footprint": "Capacitor_SMD:C_0603_1608Metric"},
        {"ref": "R1", "lib_id": "Device:R", "value": "1k",
         "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"ref": "R2", "lib_id": "Device:R", "value": "10k",
         "footprint": "Resistor_SMD:R_0603_1608Metric"},
        {"ref": "TP1", "lib_id": "Connector:TestPoint", "value": "TP",
         "footprint": "TestPoint:TestPoint_Pad_D1.0mm"},
    ],
    "nets": {
        "P5V": [["J1", "1"], ["U1", "8"], ["C1", "1"], ["C2", "1"]],
        "GND": [["J1", "2"], ["U1", "4"], ["C1", "2"], ["C2", "2"], ["R2", "2"], ["J2", "1"],
                ["TP1", "1"]],
        "IN_A": [["J1", "3"], ["R1", "1"]],
        "IN_A_R": [["R1", "2"], ["U1", "3"]],
        "OUT_A": [["U1", "1"], ["U1", "2"], ["J1", "4"]],
        "IN_B": [["J1", "5"], ["U1", "5"], ["R2", "1"]],
        "OUT_B": [["U1", "6"], ["U1", "7"]],
    },
    # J2.2 carries a no-connect flag; J1.6 is left unconnected without one
    "no_connect": [["J2", "2"]],
}
# ref -> {schematic flag: new value}, ref -> extra fields
FLAGS = {"C2": {"dnp": "yes"}, "R2": {"in_bom": "no"}, "TP1": {"on_board": "no"}}
FIELDS = {"R1": {"LCSC": "C21190", "Tolerance": "1%"}, "U1": {"MPN": "LM358DR2G"}}
PROJECT = {"meta": {"filename": f"{STEM}.kicad_pro", "version": 1}}


def run(argv: list[str]) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({r.returncode}): {r.stdout[-800:]} "
                           f"{r.stderr[-800:]}")
    return r.stdout


def edit_schematic(text: str) -> str:
    """Apply FLAGS and FIELDS to every unit block of the named symbols."""
    def block(m: re.Match) -> str:
        body = m.group(0)
        ref = re.search(r'\(property "Reference" "([^"]+)"', body)
        if not ref:
            return body
        for flag, value in FLAGS.get(ref.group(1), {}).items():
            body = re.sub(rf"\({flag} \w+\)", f"({flag} {value})", body, count=1)
        extra = "".join(f'\t\t(property "{k}" "{v}" (at 0 0 0) (effects (font (size 1.27 1.27))'
                        f' (hide yes)))\n' for k, v in FIELDS.get(ref.group(1), {}).items())
        return re.sub(r'(\t\t\(property "Footprint"[^\n]*\n)', lambda f: f.group(1) + extra,
                      body, count=1)
    return re.sub(r"\t\(symbol\n.*?\n\t\)\n", block, text, flags=re.S)


def pad_nets(xml_text: str) -> dict[str, str]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    off_board = {c.get("ref") for c in root.iterfind("components/comp")
                 if any(p.get("name") == "exclude_from_board" for p in c.findall("property"))}
    return {f"{n.get('ref')}.{n.get('pin')}": net.get("name")
            for net in root.iterfind("nets/net") for n in net.findall("node")
            if n.get("ref") not in off_board}


_SOURCE = re.compile(r"(<design>\s*<source>)[^<]*(</source>)")


def export_xml(cli: Path) -> Path:
    """kimport.xml: kicad-cli's kicadxml export of a temp copy of the
    schematic WITH its .kicad_pro, so every Sheetfile is project-relative
    ("kimport.kicad_sch"); the design's absolute <source> path is cut to the
    file name, so no machine path is committed."""
    xml = OUT / f"{STEM}.xml"
    with tempfile.TemporaryDirectory() as tmp:
        for ext in (".kicad_sch", ".kicad_pro"):
            shutil.copy2(OUT / f"{STEM}{ext}", Path(tmp) / f"{STEM}{ext}")
        run([str(cli), "sch", "export", "netlist", "--format", "kicadxml", "-o", str(xml),
             str(Path(tmp) / f"{STEM}.kicad_sch")])
    text, n = _SOURCE.subn(rf"\g<1>{STEM}.kicad_sch\g<2>", xml.read_text(encoding="utf-8"))
    if n != 1 or tempfile.gettempdir() in text:
        raise RuntimeError(f"{xml}: the export still holds a temp path")
    xml.write_text(text, encoding="utf-8", newline="\n")
    return xml


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--xml-only"]
    kicad = Path(args[0] if args else "C:/Program Files/KiCad/9.0")
    share = kicad / "share" / "kicad"
    cli, python = kicad / "bin" / "kicad-cli.exe", kicad / "bin" / "python.exe"
    if "--xml-only" in argv:
        print(export_xml(cli))
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    sch = OUT / f"{STEM}.kicad_sch"
    with tempfile.TemporaryDirectory() as tmp:
        netlist = Path(tmp) / "netlist.json"
        netlist.write_text(json.dumps(DESIGN), encoding="utf-8")
        run([sys.executable, str(ROOT / "server" / "tools" / "sch_build.py"), str(netlist),
             str(sch), str(share / "symbols")])
        sch.write_text(edit_schematic(sch.read_text(encoding="utf-8")), encoding="utf-8",
                       newline="\n")
        (OUT / f"{STEM}.kicad_pro").write_text(json.dumps(PROJECT, indent=2) + "\n",
                                               encoding="utf-8", newline="\n")
        xml = export_xml(cli)
        nets = Path(tmp) / "pad_nets.json"
        nets.write_text(json.dumps(pad_nets(xml.read_text(encoding="utf-8"))), encoding="utf-8")
        print(run([str(python), str(HERE / "make_kimport_board.py"), str(nets),
                   str(OUT / f"{STEM}.kicad_pcb"), str(share / "footprints")]).strip())
    (OUT / f"{STEM}.kicad_prl").unlink(missing_ok=True)     # SaveBoard's UI state: not a source
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
