#!/usr/bin/env python3
"""Fab outputs that carry the stackup (ADR D7.12, rule R24; task D7-22).

Called after kicad-cli wrote the Gerbers and job file, with the physical-design artifact
(``<netlist stem>.stackup.json``, D7.4), its ``stackup`` section or a ResolvedStackup:
``fix_gbrjob_file_attributes`` (copper FileFunctions from each Gerber's %TF header: KiCad
9.0.4 numbers inner copper by internal layer id; a byte-identical no-op on 10.x),
``expected_thickness_rows`` / ``check_gbrjob`` (G5 blockers), ``ipc2581_needed``,
``stackup_csv``, ``fab_notes_md``, ``write_outputs`` and ``netlist_stackup_from_gbrjob``
(the D7.2.2 ``layers`` form of a job file, for Gerber-only reconstruction).
Byte-stable (no timestamps); never mutates its inputs. Stdlib only.
"""
from __future__ import annotations

import csv
import functools
import io
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import materials as mat
import stackup_kicad as sk

__all__ = ["CSV_COLUMNS", "CSV_SUFFIX", "GBRJOB_SUFFIX", "IPC2581_SUFFIX", "NOTES_SUFFIX",
           "check_gbrjob", "expected_thickness_rows", "fab_notes_md", "fix_gbrjob_file_attributes",
           "fixed_gbrjob_text", "gerber_file_function", "ipc2581_needed", "ipc2581_reasons",
           "netlist_stackup_from_gbrjob", "stackup_csv", "write_outputs"]

GBRJOB_SUFFIX = "-job.gbrjob"
CSV_SUFFIX = "_stackup.csv"
NOTES_SUFFIX = "_fab_notes.md"
IPC2581_SUFFIX = "_ipc2581.xml"
THICKNESS_TOL_MM = 0.001
VALUE_REL_TOL = 1e-4               # Dk / loss tangent as KiCad prints them (6 digits)
HEADER_LINES = 400                 # a Gerber's attribute header ends before this
MASK_NAMES = {"F": "Top Solder Mask", "B": "Bottom Solder Mask"}
CSV_COLUMNS = ("layer", "type", "kind", "material", "material_id", "construction",
               "thickness_mm", "dk", "dk_basis", "dk_freq_ghz", "df", "role", "net", "oz",
               "locked", "source")
GLASS_STYLES = ("106", "1067", "1078", "1080", "1086", "1500", "1506", "2113", "2116", "2313",
                "3313", "7628")
FR4_CATEGORY = "fr4"
HYBRID_PARTNERS = ("fr4", "high-speed")
X2_ROLES = ("plane", "signal", "mixed")
TG155_NOTE = "select FR-4 TG155 for JLC impedance builds"
ROGERS_BONDING_SOURCE = "Rogers RO4400 bondply data sheet PUB 92-150, processing guide PUB 92-005"
SINGLE_SIDED_NOTE = ("single-sided: B.Cu holds only through-hole pad lands; order PTH/NPTH per "
                     "the fab's single-sided option *(fab wording unverified)*")
LEGACY_NOTE = ("legacy stackup shorthand {h, er, t}: no KiCad stackup block is written (ADR "
               "D7.5 step 6), so the job file carries KiCad's default stackup; its rows and "
               "BoardThickness are not checked")
STATUS_TEXT = {"fd": "FD verified", "closed_form": "closed form",
               "fallback_unverified": "**UNVERIFIED** (class C fallback)"}
TARGET_TEXT = {"z0": "Z0", "zdiff": "Zdiff"}
_GLASS = "|".join(GLASS_STYLES)
FR4_HINT = re.compile(rf"fr-?4|\b(?:{_GLASS})\b", re.I)
PREPREG_HINT = re.compile(rf"\b(?:bond\w*|pp|{_GLASS})\b", re.I)
_FILE_FUNCTION = re.compile(r"^(?:%TF\.|G04 #@! TF\.)FileFunction,(.+?)\*%?$")
_COPPER_FUNCTION = re.compile(r"^Copper,L(\d+),(Top|Inr|Bot)(?:,(\w+))?")
_FF_VALUE = re.compile(r'"FileFunction"\s*:\s*"((?:[^"\\]|\\.)*)"')
_LAYER_IN_PATH = re.compile(r"(?:^|[-_./\\])(F|B|In(\d{1,2}))_Cu\.[^.\\/]+$")
_NORM = re.compile(r"[^a-z0-9]")


# ---------------------------------------------------------------- inputs and formats
def _stackup(artifact: Any) -> Mapping[str, Any]:
    """The stackup section (ResolvedStackup.to_dict() form) of any accepted input."""
    if hasattr(artifact, "to_dict") and hasattr(artifact, "copper"):
        return artifact.to_dict()
    if not isinstance(artifact, Mapping):
        raise TypeError("artifact must be the physical-design artifact, its stackup section or "
                        f"a ResolvedStackup, got {type(artifact).__name__}")
    if "copper" in artifact and "dielectrics" in artifact:
        return artifact
    if "stackup" not in artifact or artifact["stackup"] is None:
        raise ValueError("no stackup: not a physical-design artifact, or its design has "
                         "ST-E1..E8 errors (fix the stackup at G1 before the fab outputs)")
    return artifact["stackup"]


def _lines(artifact: Any) -> Mapping[str, Any] | None:
    """The artifact's ``lines`` (None when only a stackup was given)."""
    plain = isinstance(artifact, Mapping) and "copper" not in artifact
    return artifact["lines"] if plain and isinstance(artifact.get("lines"), Mapping) else None


_shipped_library = functools.lru_cache(maxsize=1)(mat.load_library)


def _library(materials: mat.MaterialLibrary | None) -> mat.MaterialLibrary:
    return materials if materials is not None else _shipped_library()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _g(value: Any) -> str:
    """A number as fab files show it (six significant digits, no trailing zeros)."""
    number = _number(value)
    return "" if number is None else f"{number:.6g}"


def _ply_label(ply: Mapping[str, Any]) -> str:
    """The material name KiCad writes for a ply (stackup_kicad.ply_label)."""
    return sk.ply_label(SimpleNamespace(label=ply.get("label"),
                                        construction=ply.get("construction")))


def _mask_label(mask: Mapping[str, Any]) -> str | None:
    return sk.mask_label(mask.get("material")) if mask.get("source") != "none" else None


def _plies(st: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [p for d in st["dielectrics"] for p in d["plies"]]


def _ply_names(prefix: str, n: int) -> list[str]:
    """``prefix``, or ``prefix (j/n)`` for each ply of a multi-ply dielectric (KiCad's)."""
    return [prefix + (f" ({j + 1}/{n})" if n > 1 else "") for j in range(n)]


def _loads(text: str, label: str) -> dict[str, Any]:
    try:
        job = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a Gerber job file (.gbrjob JSON): {exc}") from exc
    if not isinstance(job, dict):
        raise ValueError(f"{label} is not a Gerber job file (.gbrjob JSON object)")
    return job


def _read_job(gbrjob: Any) -> tuple[Mapping[str, Any], str]:
    """(job, label): a parsed job file, its JSON text, or a path to it."""
    if isinstance(gbrjob, Mapping):
        return gbrjob, "a Gerber job file"
    if isinstance(gbrjob, str) and gbrjob.lstrip().startswith("{"):
        return _loads(gbrjob, "the text"), "a Gerber job file"
    path = Path(gbrjob)
    return (_loads(path.read_bytes().decode("utf-8", errors="replace"), path.name),
            f"Gerber job file {path.name}")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- fix_gbrjob_file_attributes
def gerber_file_function(path: str | os.PathLike[str]) -> str | None:
    """A Gerber's own FileFunction (``%TF.FileFunction,...*%``, or the ``G04 #@! TF.``
    comment form without X2) from its attribute header, else None."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for k, line in enumerate(fh):
            text = line.strip()
            if k >= HEADER_LINES or text.startswith(("%AD", "G04 APERTURE LIST")):
                break
            m = _FILE_FUNCTION.match(text)
            if m:
                return m.group(1)
    return None


def _rewrite(text: str, attrs: Sequence[Any], functions: Mapping[str, str]) -> str:
    """Replace only the FileFunction values that change, in place (KiCad writes the key
    only in FilesAttributes); a KiCad-style re-serialisation (2-space JSON, same line
    ends) when the entries cannot be matched one to one."""
    matches = list(_FF_VALUE.finditer(text))
    if len(matches) != len(attrs) or not all(isinstance(e, Mapping) and "FileFunction" in e
                                             for e in attrs):
        new = [{**e, "FileFunction": functions[e["Path"]]}
               if isinstance(e, Mapping) and e.get("Path") in functions else e for e in attrs]
        newline = "\r\n" if "\r\n" in text else "\n"
        body = json.dumps({**_loads(text, "the job file"), "FilesAttributes": new}, indent=2,
                          ensure_ascii=False).replace("\n", newline)
        return body + (newline if text.endswith("\n") else "")
    pieces, last = [], 0
    for m, entry in zip(matches, attrs, strict=True):
        value = functions.get(entry.get("Path"))
        if value is not None and value != entry.get("FileFunction"):
            pieces += [text[last:m.start(1)], json.dumps(value, ensure_ascii=False)[1:-1]]
            last = m.end(1)
    return "".join(pieces) + text[last:]


def fixed_gbrjob_text(text: str, functions: Mapping[str, str]
                      ) -> tuple[str, list[dict[str, str]]]:
    """(Job-file text with each FilesAttributes entry named in ``functions`` given that
    FileFunction, the changes ``[{path, from, to}]``); pure, ``text`` when nothing differs."""
    attrs = _loads(text, "the job file").get("FilesAttributes")
    if not isinstance(attrs, list):
        raise ValueError("the job file has no FilesAttributes list")
    changes = [{"path": e["Path"], "from": e.get("FileFunction"), "to": functions[e["Path"]]}
               for e in attrs if isinstance(e, Mapping) and e.get("Path") in functions
               and functions[e["Path"]] != e.get("FileFunction")]
    return (_rewrite(text, attrs, functions), changes) if changes else (text, [])


def _gerber_functions(job: Mapping[str, Any], folder: Path) -> tuple[dict[str, str], list[str]]:
    functions: dict[str, str] = {}
    problems: list[str] = []
    for entry in job.get("FilesAttributes") or ():
        if not (isinstance(entry, Mapping)
                and str(entry.get("FileFunction", "")).startswith("Copper,")):
            continue
        rel, written = str(entry.get("Path", "")), entry["FileFunction"]
        if not (folder / rel).is_file():
            problems.append(f"{rel}: the Gerber file is missing beside the job file; its "
                            f"FileFunction {written!r} was not checked")
            continue
        found = gerber_file_function(folder / rel)
        if found is None or not found.startswith("Copper,"):
            problems.append(f"{rel}: no copper %TF.FileFunction in the Gerber header (found "
                            f"{found!r}); the job file entry {written!r} was left as written")
        else:
            functions[rel] = found
    return functions, problems


def fix_gbrjob_file_attributes(gbrjob: str | os.PathLike[str],
                               gerber_dir: str | os.PathLike[str] | None = None
                               ) -> dict[str, Any]:
    """Rewrite the job file's copper FileFunctions from each Gerber's own header (Gerbers
    in ``gerber_dir``, default beside the job file) -> ``{path, changed [{path, from, to}],
    problems, written}``; the file is written only when a value changes."""
    path = Path(gbrjob)
    folder = Path(gerber_dir) if gerber_dir is not None else path.parent
    text = path.read_bytes().decode("utf-8")
    functions, problems = _gerber_functions(_loads(text, path.name), folder)
    new_text, changes = fixed_gbrjob_text(text, functions)
    if changes:
        _atomic_write(path, new_text.encode("utf-8"))
    return {"path": str(path), "changed": changes, "problems": problems,
            "written": bool(changes)}


# ---------------------------------------------------------------- expected rows and check
def _row(kind: str, name: str, thickness: Any, material: str | None = None,
         dk: Any = None, df: Any = None) -> dict[str, Any]:
    return {"type": kind, "name": name, "thickness": float(thickness or 0.0),
            "material": material, "dk": _number(dk), "df": _number(df)}


def _mask_row(mask: Mapping[str, Any], side: str) -> dict[str, Any]:
    on = mask.get("source") != "none"
    return _row("SolderMask", MASK_NAMES[side], mask.get("over_substrate"), _mask_label(mask),
                mask.get("dk") if on else None, mask.get("df") if on else None)


def expected_thickness_rows(artifact: Any) -> tuple[dict[str, Any], ...]:
    """The job-file Thickness rows the stackup makes KiCad write, top to bottom: ``{type,
    name (KiCad's), thickness, material, dk, df}``, copper + every ply + 2 masks (0 mm
    without mask). ValueError for the legacy shorthand (it writes no KiCad block)."""
    st = _stackup(artifact)
    if st.get("legacy") is not None:
        raise ValueError(LEGACY_NOTE)
    copper, dies = st["copper"], st["dielectrics"]
    rows = [_mask_row(st["mask"], "F")]
    for k, cu in enumerate(copper):
        rows.append(_row("Copper", cu["name"], cu["thickness"]))
        if k < len(dies):
            names = _ply_names(f"{cu['name']}/{copper[k + 1]['name']}", len(dies[k]["plies"]))
            rows += [_row("Dielectric", name, p["thickness"], _ply_label(p), p.get("er"),
                          p.get("df")) for name, p in zip(names, dies[k]["plies"], strict=True)]
    return (*rows, _mask_row(st["mask"], "B"))


def _material_problems(where: str, got: Mapping[str, Any], want: Mapping[str, Any],
                       constrained: bool) -> list[str]:
    """Material, Dk and loss tangent of one row; KiCad writes Dk / Df only when the
    stackup has (dielectric_constraints yes), so they must be there only then."""
    text = got.get("Material")
    out = ([] if text is not None and str(text).strip().lower() == want["material"].lower()
           else [f"{where}: material {text!r}, the stackup has {want['material']!r}"])
    for key, label, value in (("DielectricConstant", "Dk", want["dk"]),
                              ("LossTangent", "loss tangent", want["df"])):
        found = _number(got.get(key))
        same = found is not None and value is not None and math.isclose(
            found, value, rel_tol=VALUE_REL_TOL, abs_tol=1e-6)
        if value is not None and (constrained or key in got) and not same:
            out.append(f"{where}: {label} {got.get(key)!r}, the stackup has {value:g}")
    return out


def _one_row(index: int, got: Mapping[str, Any], want: Mapping[str, Any],
             constrained: bool) -> list[str]:
    where = f"thickness row {index} ({want['name']})"
    if got.get("Type") != want["type"]:
        return [f"{where}: expected a {want['type']} row, the job file has {got.get('Type')} "
                f"{got.get('Name')!r}"]
    thickness = _number(got.get("Thickness"))
    out = ([] if thickness is not None
           and abs(thickness - want["thickness"]) <= THICKNESS_TOL_MM + 1e-9 else
           [f"{where}: thickness {got.get('Thickness')} mm, the stackup has "
            f"{want['thickness']:g} mm (+/- {THICKNESS_TOL_MM:g})"])
    return out + (_material_problems(where, got, want, constrained)
                  if want["material"] is not None else [])


def _row_problems(rows: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]],
                  constrained: bool) -> list[str]:
    if not rows:
        return ["the job file has no Thickness rows: KiCad writes them only when the stackup "
                "block lists all the technical layers (F/B SilkS, Paste, Mask; ADR D7.1); "
                "rebuild the board so the managed stackup block is spliced in"]
    out = ([] if len(rows) == len(expected) else
           [f"the job file has {len(rows)} thickness rows, the stackup needs {len(expected)} "
            "(copper + every ply + 2 masks)"])
    for k, (got, want) in enumerate(zip(rows, expected, strict=False)):
        out += _one_row(k + 1, got, want, constrained)
    return out


def _spec_problems(specs: Mapping[str, Any], st: Mapping[str, Any]) -> list[str]:
    out = []
    count = st["kicad_copper_count"]
    if _number(specs.get("LayerNumber")) != count:
        out.append(f"GeneralSpecs.LayerNumber is {specs.get('LayerNumber')}, the stackup has "
                   f"{count} copper layers")
    has, want = specs.get("ImpedanceControlled", False) is True, bool(st["impedance_controlled"])
    if has is not want:
        out.append(f"GeneralSpecs.ImpedanceControlled is {str(has).lower()}, the stackup "
                   f"{'requires' if want else 'does not require'} impedance control (KiCad "
                   "writes it from (dielectric_constraints yes))")
    finish = st.get("finish")
    kicad = sk.FINISH_TO_KICAD.get(finish, finish) if finish else None
    if kicad and st.get("legacy") is None and specs.get("Finish") != kicad:
        out.append(f"GeneralSpecs.Finish is {specs.get('Finish')!r}, the stackup orders "
                   f"{kicad!r}")
    return out


def _thickness_problem(specs: Mapping[str, Any], st: Mapping[str, Any]) -> list[str]:
    value, want = _number(specs.get("BoardThickness")), float(st["board_thickness_computed"])
    if value is not None and abs(value - want) <= THICKNESS_TOL_MM + 1e-9:
        return []
    return [f"GeneralSpecs.BoardThickness is {specs.get('BoardThickness')} mm, the stackup sums "
            f"to {want:g} mm (copper + plies + 2 masks, +/- {THICKNESS_TOL_MM:g})"]


def _position(path: str, side: str, count: int) -> int | None:
    """The L number a copper Gerber's position gives (Top 1, Bot n, InK K+1 by file name)."""
    if side in ("Top", "Bot"):
        return 1 if side == "Top" else count
    m = _LAYER_IN_PATH.search(path)
    return int(m.group(2)) + 1 if m and m.group(2) else None


def _numbering_problems(job: Mapping[str, Any]) -> list[str]:
    found = [(str(e.get("Path", "")), m) for e in job.get("FilesAttributes") or ()
             if isinstance(e, Mapping)
             and (m := _COPPER_FUNCTION.match(str(e.get("FileFunction", ""))))]
    count = int(_number((job.get("GeneralSpecs") or {}).get("LayerNumber")) or len(found))
    out = []
    for path, m in found:
        number, want = int(m.group(1)), _position(path, m.group(2), count)
        inner_ok = m.group(2) != "Inr" or 2 <= number <= count - 1
        if (want is not None and number != want) or not inner_ok:
            out.append(f"{path}: FileFunction {m.group(0)!r} numbers the copper L{number}, its "
                       f"position is L{want if want is not None else '?'} (KiCad 9.0.4 numbers "
                       "inner copper by its internal layer id): run fix_gbrjob_file_attributes "
                       "before zipping")
    return out


def check_gbrjob(gbrjob: Any, artifact: Any) -> dict[str, Any]:
    """Does the job file (path, JSON text or object) carry the stackup (R24)? -> ``{ok,
    rows, expected_rows, problems, notes}``. For the legacy shorthand (expected_rows None)
    only LayerNumber, ImpedanceControlled and the copper numbering are checked."""
    job, _ = _read_job(gbrjob)
    st = _stackup(artifact)
    specs = job.get("GeneralSpecs") if isinstance(job.get("GeneralSpecs"), Mapping) else {}
    rows = [r for r in job.get("MaterialStackup") or ()
            if isinstance(r, Mapping) and "Thickness" in r]
    problems = _spec_problems(specs, st) + _numbering_problems(job)
    want = None if st.get("legacy") is not None else expected_thickness_rows(st)
    if want is not None:
        problems += (_row_problems(rows, want, bool(st["impedance_controlled"]))
                     + _thickness_problem(specs, st))
    return {"ok": not problems, "rows": len(rows),
            "expected_rows": None if want is None else len(want), "problems": problems,
            "notes": [LEGACY_NOTE] if want is None else []}


# ---------------------------------------------------------------- materials, IPC-2581, hybrids
def _material_of(ply: Mapping[str, Any], lib: mat.MaterialLibrary) -> str | None:
    """The ply's library material: its id, else the product its label names (longest)."""
    if ply.get("material_id") in lib:
        return ply["material_id"]
    text = _NORM.sub("", " ".join(str(x) for x in (ply.get("label"), ply.get("construction"))
                                  if x).lower())
    hits = [(len(p), mid) for mid in lib.ids()
            if lib.materials[mid].get("category") != "mask"
            and len(p := _NORM.sub("", str(lib.materials[mid].get("product", "")).lower())) >= 3
            and p in text]
    return max(hits)[1] if hits else None


def _category(ply: Mapping[str, Any], lib: mat.MaterialLibrary) -> str | None:
    """fr4 | high-speed | rf-laminate, or None when the label names nothing known."""
    mid = _material_of(ply, lib)
    if mid is not None:
        return lib.materials[mid].get("category")
    return FR4_CATEGORY if FR4_HINT.search(_ply_label(ply)) else None


def ipc2581_reasons(artifact: Any, *, materials: mat.MaterialLibrary | None = None
                    ) -> list[str]:
    """Why the fab package needs ``<stem>_ipc2581.xml`` (D7.12): more than 2 copper
    layers, a non-FR-4 material (an unidentified label counts), impedance control."""
    st, lib = _stackup(artifact), _library(materials)
    count = st["kicad_copper_count"]
    other = sorted({_ply_label(p) for p in _plies(st) if _category(p, lib) != FR4_CATEGORY})
    return ([f"{count} copper layers (more than 2)"] if count > 2 else []) + (
        [f"non-FR-4 material: {', '.join(other)}"] if other else []) + (
        ["impedance control required"] if st.get("impedance_controlled") else [])


def ipc2581_needed(artifact: Any, *, materials: mat.MaterialLibrary | None = None) -> bool:
    """True when ``ipc2581_reasons`` gives a reason."""
    return bool(ipc2581_reasons(artifact, materials=materials))


def _hybrid(st: Mapping[str, Any], lib: mat.MaterialLibrary
            ) -> tuple[list[str], list[str], list[str]]:
    """(RF laminates, FR-4 class materials, RF vendors) of a hybrid build, else empty."""
    rf, other, vendors = set(), set(), set()
    for ply in _plies(st):
        cat, mid = _category(ply, lib), _material_of(ply, lib)
        if cat == "rf-laminate":
            rf.add(_ply_label(ply))
            vendors.add(str(lib.materials[mid].get("vendor")) if mid else "the laminate vendor")
        elif cat in HYBRID_PARTNERS:
            other.add(_ply_label(ply))
    return (sorted(rf), sorted(other), sorted(vendors)) if rf and other else ([], [], [])


# ---------------------------------------------------------------- <stem>_stackup.csv
def _csv_row(**values: Any) -> list[str]:
    return [values.get(c, "") for c in CSV_COLUMNS]


def _mask_csv(mask: Mapping[str, Any], side: str) -> list[str]:
    on = mask.get("source") != "none"
    return _csv_row(layer=f"{side}.Mask", type="solder_mask", material=_mask_label(mask) or "",
                    thickness_mm=_g(mask.get("over_substrate")),
                    dk=_g(mask.get("dk")) if on else "", df=_g(mask.get("df")) if on else "",
                    dk_basis=(mask.get("basis") or "") if on else "",
                    source=mask.get("source") or "")


def _dielectric_csv(die: Mapping[str, Any]) -> list[list[str]]:
    def ghz(hz: Any) -> str:
        return _g(_number(hz) / 1e9) if _number(hz) else ""
    names = _ply_names(die["name"], len(die["plies"]))
    return [_csv_row(layer=name, type="dielectric", kind=die.get("kind") or "",
                     material=_ply_label(p), material_id=p.get("material_id") or "",
                     construction=p.get("construction") or "", thickness_mm=_g(p["thickness"]),
                     dk=_g(p.get("er")), dk_basis=p.get("er_basis") or "",
                     dk_freq_ghz=ghz(p.get("er_freq_hz")), df=_g(p.get("df")),
                     locked="yes" if die.get("locked") else "no", source=p.get("er_source") or "")
            for name, p in zip(names, die["plies"], strict=True)]


def stackup_csv(artifact: Any) -> str:
    """``<stem>_stackup.csv``: masks, copper and every ply top to bottom (material, Dk with
    basis and characterised frequency, Df, thickness), then a ``total`` row; LF ends."""
    st = _stackup(artifact)
    rows = [list(CSV_COLUMNS), _mask_csv(st["mask"], "F")]
    for k, cu in enumerate(st["copper"]):
        rows.append(_csv_row(layer=cu["name"], type="copper", thickness_mm=_g(cu["thickness"]),
                             role=cu.get("role") or "", net=cu.get("net") or "",
                             oz=_g(cu.get("oz"))))
        if k < len(st["dielectrics"]):
            rows += _dielectric_csv(st["dielectrics"][k])
    rows += [_mask_csv(st["mask"], "B"),
             _csv_row(layer="total", type="total", thickness_mm=_g(st["board_thickness_computed"]))]
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


# ---------------------------------------------------------------- <stem>_fab_notes.md
def _build_text(st: Mapping[str, Any]) -> str:
    preset, source = st.get("preset"), st.get("source")
    texts = {"preset": f"preset `{preset}`",
             "policy": f"napkin layer-policy default, preset `{preset}`",
             "default": f"default template `{preset}` (no stackup declared)",
             "layers": "custom layer list (stackup.layers)",
             "legacy": ("legacy shorthand {h, er, t}: no KiCad stackup block; order the fab's "
                        f"standard {st['kicad_copper_count']}-layer build")}
    return texts.get(source, str(source))


def _mask_text(st: Mapping[str, Any]) -> str:
    mask = st["mask"]
    values = (f"{_mask_label(mask)}, Er {_g(mask.get('dk'))}, {_g(mask.get('over_substrate'))} mm "
              f"over substrate, {_g(mask.get('over_trace'))} mm over traces")
    texts = {"fab": f"the fab's solder mask ({st.get('fab')}): {values}; the impedance includes it",
             "generic": f"generic LPI (typical values: {values}): the fab publishes none, "
                        "confirm its mask thickness and Er",
             "declared": f"as declared: {values}",
             "none": "none (mask: null, ST-W9): the impedance was computed without solder mask"}
    if st.get("legacy") is not None:
        return ("not modelled: the legacy shorthand computes an unmasked outer microstrip "
                "(ST-W7); a standard mask lowers Z0 by about 3.5 %")
    return texts.get(str(mask.get("source")), str(mask.get("source")))


def _thickness_text(st: Mapping[str, Any]) -> str:
    computed, ordered = st["board_thickness_computed"], st.get("board_thickness_ordered")
    tail = f"computed {_g(computed)} mm (copper + dielectrics + 2 solder masks)"
    if ordered is None:
        return f"ordered: not stated; {tail}"
    return f"ordered {_g(ordered)} mm, {tail}: {computed - ordered:+.4f} mm vs ordered"


def _stackup_section(st: Mapping[str, Any], stem: str, ipc: Sequence[str]) -> list[str]:
    tol = st.get("impedance_tolerance_pct")
    impedance = ("not required" if not st.get("impedance_controlled") else
                 f"required, +/-{_g(tol)} %" if tol is not None else "required")
    rationale = st.get("rationale") or "not stated (declare stackup.rationale)"
    if st.get("origin") == "layer_policy":
        rationale += " (napkin layer-policy default, recorded as a decision)"
    single = " (single-sided: KiCad 2-layer, B.Cu unused)" if st["copper_layers"] == 1 else ""
    ipc_text = (f"`{stem}{IPC2581_SUFFIX}` ({'; '.join(ipc)})" if ipc
                else "not needed (2 copper layers, FR-4 only, no impedance control)")
    return ["## Stackup", "", f"- Build: {_build_text(st)}", f"- Fab: {st.get('fab') or 'none'}",
            f"- Copper layers: {st['copper_layers']}{single}",
            f"- Layer-count rationale: {rationale}", f"- Board thickness: {_thickness_text(st)}",
            f"- Finish: {st.get('finish') or 'not specified'}",
            f"- Impedance control: {impedance}", f"- Solder mask: {_mask_text(st)}",
            f"- Layer table (material, Dk/Df with basis and frequency, thickness): "
            f"`{stem}{CSV_SUFFIX}`", f"- IPC-2581: {ipc_text}", ""]


def _net_title_and_tolerance(net: str, rec: Mapping[str, Any], st: Mapping[str, Any],
                             design: Mapping[str, Any] | None) -> tuple[str, str]:
    """'NET / PARTNER' for a diff pair; the net's z_tol_pct, else the stackup's."""
    spec = ((design or {}).get("net_specs") or {}).get(net)
    spec = spec if isinstance(spec, Mapping) else {}
    partner = spec.get("diff_pair")
    tol = next((t for t in (spec.get("z_tol_pct"), st.get("impedance_tolerance_pct"))
                if t is not None), None)
    return (f"{net} / {partner}" if partner and rec.get("kind") == "diff" else net,
            f"+/-{_g(tol)} %" if tol is not None else "-")


def _reference(layer: Mapping[str, Any], st: Mapping[str, Any]) -> str:
    nets = {c["name"]: c.get("net") for c in st["copper"]}
    ref = layer.get("ref") or {}
    names = [*ref.get("above", ()), *ref.get("below", ())]
    return ", ".join(f"{n} {nets.get(n) or ''}".strip() for n in names) or "-"


def _layer_row(title: str, name: str, lay: Mapping[str, Any], tol: str,
               st: Mapping[str, Any]) -> str:
    kind = TARGET_TEXT.get(str(lay.get("target_kind")), str(lay.get("target_kind")))
    gap = (f"{_g(lay['pair_gap'])} pair" if lay.get("pair_gap") is not None else
           f"{_g(lay['ground_gap'])} to GND" if lay.get("ground_gap") is not None else "-")
    return (f"| {title} | {name} | {lay.get('line')} | {_g(lay.get('z_target'))} ohm {kind} | "
            f"{_g(lay.get('width'))} | {gap} | {tol} | {_reference(lay, st)} | "
            f"{lay.get('accuracy_class')} ({_g(lay.get('est_error_pct'))} %) | "
            f"{STATUS_TEXT.get(str(lay.get('status')), str(lay.get('status')))} |")


def _line_rows(net: str, rec: Mapping[str, Any], st: Mapping[str, Any],
               design: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    """The impedance table rows of one net (one per layer) and its flagged lines."""
    title, tol = _net_title_and_tolerance(net, rec, st, design)
    layers, order = rec.get("layers") or {}, [c["name"] for c in st["copper"]]
    if not layers:
        return [f"| {title} | - | {rec.get('line') or '-'} | {_g(rec.get('z_target'))} ohm | - "
                f"| - | {tol} | - | - | no geometry ({rec.get('status') or 'see the G1 issues'})"
                " |"], []
    names = sorted(layers, key=lambda n: order.index(n) if n in order else len(order))
    rows = [_layer_row(title, n, {"line": rec.get("line"), **layers[n]}, tol, st) for n in names]
    flagged = [f"- **UNVERIFIED**: {title} on {n} ({layers[n].get('line')}, class "
               f"{layers[n].get('accuracy_class')} fallback, est. error "
               f"{_g(layers[n].get('est_error_pct'))} %)"
               for n in names if layers[n].get("status") == "fallback_unverified"]
    return rows, flagged


def _impedance_section(st: Mapping[str, Any], lines: Mapping[str, Any] | None,
                       design: Mapping[str, Any] | None) -> list[str]:
    head = ["## Controlled impedance", ""]
    if lines is None:
        return head + ["No impedance data: the stackup was given without the physical-design "
                       "artifact (its `lines` section).", ""]
    if not lines:
        return head + ["No controlled-impedance nets in this design.", ""]
    parts = [_line_rows(net, lines[net], st, design) for net in sorted(lines)]
    rows, flagged = [r for p in parts for r in p[0]], [f for p in parts for f in p[1]]
    table = ["| Net | Layer | Line | Target | Width (mm) | Gap (mm) | Tolerance | Reference | "
             "Accuracy | Status |", "|---|---|---|---|---|---|---|---|---|---|", *rows, ""]
    if flagged:
        table += ["Flagged lines (not verified by a field solver): ask the fab to confirm these "
                  "widths with its field solver, or install numpy + scipy and re-run the "
                  "resolver with FD.", "", *flagged, ""]
    return head + table


def _instructions(st: Mapping[str, Any], lib: mat.MaterialLibrary) -> list[str]:
    out = []
    if st.get("fab") == "jlcpcb" and st.get("impedance_controlled"):
        out.append(f"- {TG155_NOTE} *(unverified, gsuberland/jlcpcb_autogenerated_stackups "
                   "README)*")
    rf, other, vendors = _hybrid(st, lib)
    if rf:
        source = f" ({ROGERS_BONDING_SOURCE})" if "Rogers" in vendors else ""
        out.append(f"- Hybrid build ({', '.join(rf)} with {', '.join(other)}): bonding to FR-4 "
                   f"cores: fab to confirm with {' / '.join(vendors)}{source}")
    if st["copper_layers"] == 1:
        out.append(f"- {SINGLE_SIDED_NOTE}")
    if st["mask"].get("source") == "none" and st.get("legacy") is None:
        out.append("- **open the solder mask over the listed impedance lines** (the stackup "
                   "declares mask: null; the impedance was computed without mask)")
    return ["## Fab instructions", "", *(out or ["- none"]), ""]


def fab_notes_md(artifact: Any, *, design: Mapping[str, Any] | None = None,
                 stem: str | None = None, human_steps: Sequence[str] = (),
                 materials: mat.MaterialLibrary | None = None) -> str:
    """``<stem>_fab_notes.md`` (ADR D7.12): build, rationale, thicknesses, mask source,
    the impedance table per net and layer (fallbacks flagged), fab instructions (TG155,
    hybrid, single-sided, mask null), human steps; ``design`` names diff-pair partners."""
    st, lib = _stackup(artifact), _library(materials)
    name = stem or "board"
    lines = [f"# Fab notes: {name}", "",
             f"Generated by kicad-eda from the physical-design artifact (ADR D7.12). Order the "
             f"board with these notes, `{name}{CSV_SUFFIX}` and the job file.", ""]
    lines += _stackup_section(st, name, ipc2581_reasons(st, materials=lib))
    lines += _impedance_section(st, _lines(artifact), design) + _instructions(st, lib)
    if human_steps:
        lines += ["## Human steps", "", *(f"- {s}" for s in human_steps), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_outputs(out_dir: str | os.PathLike[str], stem: str, artifact: Any, *,
                  design: Mapping[str, Any] | None = None, human_steps: Sequence[str] = (),
                  materials: mat.MaterialLibrary | None = None) -> dict[str, str]:
    """Write ``<stem>_stackup.csv`` and ``<stem>_fab_notes.md`` into ``out_dir`` (each only
    when its bytes change) -> ``{csv, notes}`` paths."""
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    texts = {"csv": (folder / f"{stem}{CSV_SUFFIX}", stackup_csv(artifact)),
             "notes": (folder / f"{stem}{NOTES_SUFFIX}",
                       fab_notes_md(artifact, design=design, stem=stem, human_steps=human_steps,
                                    materials=materials))}
    for path, text in texts.values():
        if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
            _atomic_write(path, text.encode("utf-8"))
    return {key: str(path) for key, (path, _) in texts.items()}


# ---------------------------------------------------------------- netlist_stackup_from_gbrjob
def _groups(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Copper rows and runs of dielectric rows (one dielectric's plies), checked to
    alternate copper first and last, each with a thickness."""
    groups: list[tuple[str, list[Mapping[str, Any]]]] = []
    for r in rows:
        if r.get("Type") == "Dielectric" and groups and groups[-1][0] == "dielectric":
            groups = [*groups[:-1], ("dielectric", [*groups[-1][1], r])]
        elif r.get("Type") in ("Copper", "Dielectric"):
            groups.append((str(r["Type"]).lower(), [r]))
    kinds = [g[0] for g in groups]
    if not kinds or kinds[0] != "copper" or kinds[-1] != "copper" or any(
            a == b for a, b in zip(kinds, kinds[1:], strict=False)):
        raise ValueError("the job file's MaterialStackup does not alternate copper and "
                         f"dielectric, copper first and last: {[r.get('Name') for r in rows]}")
    bare = [r.get("Name") for g in groups for r in g[1] if _number(r.get("Thickness")) is None]
    if bare:
        raise ValueError(f"the job file gives no thickness for {bare}: KiCad writes thicknesses "
                         "only when the stackup lists every technical layer")
    return groups


def _values(row: Mapping[str, Any], defaults: Sequence[float]) -> tuple[float, float]:
    """(Dk, loss tangent) of a job-file row, KiCad's defaults for what it leaves out."""
    dk, df = _number(row.get("DielectricConstant")), _number(row.get("LossTangent"))
    return (dk if dk else defaults[0]), (df if df is not None else defaults[1])


def _ply_entry(row: Mapping[str, Any], lib: mat.MaterialLibrary) -> dict[str, Any]:
    """One ply: the job file's Dk/Df (KiCad writes them only for impedance builds), else
    the library material its label names (Dk from the library), else KiCad's FR4."""
    text, thickness = str(row.get("Material") or "").strip(), _number(row["Thickness"])
    mid = _material_of({"label": text}, lib) if text and "DielectricConstant" not in row else None
    if mid is not None:
        glass = re.search(rf"\b(?:{_GLASS})\b", text)
        return {"thickness": thickness, "material": mid, "label": text,
                **({"construction": glass.group(0)} if glass else {})}
    name, *defaults = sk.KICAD_DEFAULT_DIELECTRIC
    dk, df = _values(row, defaults) if text else defaults
    return {"thickness": thickness, "material": text or name, "dk": dk, "df": df}


def _dielectric_entry(rows: Sequence[Mapping[str, Any]],
                      lib: mat.MaterialLibrary) -> dict[str, Any]:
    """One D7.2.2 dielectric; kind: core unless its labels or ply count say prepreg."""
    plies = [_ply_entry(r, lib) for r in rows]
    text = " ".join(f"{r.get('Material') or ''} {r.get('Name') or ''}" for r in rows).lower()
    kind = ("prepreg" if "prepreg" in text else "core" if "core" in text
            else "prepreg" if len(rows) > 1 or PREPREG_HINT.search(text) else "core")
    entry: dict[str, Any] = {"type": "dielectric", "kind": kind}
    return {**entry, **plies[0]} if len(plies) == 1 else {**entry, "plies": plies}


def _x2_roles(job: Mapping[str, Any], count: int) -> dict[int, str]:
    """{copper index: role} from X2 copper types (``Copper,L2,Inr,Plane``)."""
    roles: dict[int, str] = {}
    for entry in job.get("FilesAttributes") or ():
        m = (_COPPER_FUNCTION.match(str(entry.get("FileFunction", "")))
             if isinstance(entry, Mapping) else None)
        if m and str(m.group(3)).lower() in X2_ROLES:
            number = _position(str(entry.get("Path", "")), m.group(2), count) or int(m.group(1))
            if 1 <= number <= count:
                roles[number - 1] = str(m.group(3)).lower()
    return roles


def _mask_from_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The D7.2.2 ``mask`` of the first solder mask with a thickness (None: no mask)."""
    row = next((r for r in rows if r.get("Type") == "SolderMask"
                and (_number(r.get("Thickness")) or 0) > 0), None)
    if row is None:
        return None
    mask: dict[str, Any] = {"over_substrate": _number(row["Thickness"])}
    if row.get("Material"):
        dk, df = _values(row, sk.KICAD_MASK_DEFAULTS)
        mask = {**mask, "material": str(row["Material"]), "dk": dk, "df": df}
    return {**mask, "color": str(row["Color"])} if row.get("Color") else mask


def _stackup_object(job: Mapping[str, Any], layers: list[dict[str, Any]],
                    rows: Sequence[Mapping[str, Any]], label: str) -> dict[str, Any]:
    specs = job.get("GeneralSpecs") if isinstance(job.get("GeneralSpecs"), Mapping) else {}
    finish = specs.get("Finish")
    finish = sk.KICAD_TO_FINISH.get(finish) or (finish if finish in sk.FINISH_TO_KICAD else None)
    thickness = _number(specs.get("BoardThickness"))
    return {"layers": layers, **({"finish": finish} if finish else {}),
            "mask": _mask_from_rows(rows),
            **({"impedance_control": {"required": True}}
               if specs.get("ImpedanceControlled") is True else {}),
            **({"board_thickness": thickness} if thickness else {}),
            "origin": "designer", "rationale": f"imported from {label}"}


def netlist_stackup_from_gbrjob(gbrjob: Any, *, materials: mat.MaterialLibrary | None = None
                                ) -> dict[str, Any]:
    """A job file's MaterialStackup (path, text or object) as the D7.2.2 ``layers`` form:
    positional copper (role from an X2 copper type), plies with material label and Dk/Df,
    ``kind`` (heuristic), mask (None at 0 mm), finish, impedance control, board
    thickness; origin designer. ValueError for a missing or inconsistent stack."""
    job, label = _read_job(gbrjob)
    lib = _library(materials)
    rows = [r for r in job.get("MaterialStackup") or () if isinstance(r, Mapping)]
    if not rows:
        raise ValueError(f"{label} has no MaterialStackup: export the job file from a board "
                         "with a physical stackup")
    groups = _groups(rows)
    count = sum(1 for kind, _ in groups if kind == "copper")
    declared = _number((job.get("GeneralSpecs") or {}).get("LayerNumber"))
    if (declared is not None and int(declared) != count) or count % 2:
        raise ValueError(f"GeneralSpecs.LayerNumber is {declared} but the MaterialStackup lists "
                         f"{count} copper layers (a stackup has an even number 2..32)")
    names = ["F.Cu", *(f"In{k}.Cu" for k in range(1, count - 1)), "B.Cu"]
    roles, layers = _x2_roles(job, count), []
    for kind, members in groups:
        if kind == "dielectric":
            layers.append(_dielectric_entry(members, lib))
            continue
        k = sum(1 for e in layers if e["type"] == "copper")
        entry = {"type": "copper", "name": names[k], "thickness": _number(members[0]["Thickness"])}
        layers.append({**entry, "role": roles[k]} if k in roles else entry)
    return _stackup_object(job, layers, rows, label)
