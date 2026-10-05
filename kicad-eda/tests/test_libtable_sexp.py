"""libtable_sexp — the S-expression layer under libtable: the tokenizer, LibRow /
LibTable, parse_table / read_table / format_table / write_table, LibTableError
and header_value (a file header's ``(generator ...)`` / ``(version ...)``).

The table excerpts are verbatim from the KiCad 9.0 / 10.99 templates and from
tables the KiCad 11 nightly ``pcb import`` / ``sch import`` wrote; test_libtable
imports them (and the live-tier install helper) too. libtable re-exports this
layer for the wave-1 consumers. Both modules are stdlib-only (KiCad 9's
bundled Python imports them). Live tier (skipped without KiCad): every
installed KiCad's full templates parse.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import kicad_env as env
import libtable
import libtable_sexp as sx

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"
OUR_MODULES = {"libtable", "libtable_sexp"}

# --- verbatim excerpts ------------------------------------------------------
# KiCad 9.0 template/global fp-lib-table: the OLD unquoted row syntax, no
# version line, and "Kicad" (lower-case k) on the first row.
K9_FP = """(fp_lib_table
  (lib (name Audio_Module)(type Kicad)(uri ${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty)(options "")(descr "Audio Module footprints"))
  (lib (name Connector_BarrelJack)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Connector_BarrelJack.pretty)(options "")(descr "(DC) barrel jack connector footprints"))
  (lib (name Resistor_SMD)(type KiCad)(uri ${KICAD9_FOOTPRINT_DIR}/Resistor_SMD.pretty)(options "")(descr "Resistor footprints, surface mount (SMD)"))
)
"""  # noqa: E501
# KiCad 9.0 template sym-lib-table: quoted, no spaces between tokens.
K9_SYM = """(sym_lib_table
  (lib (name "Device")(type "KiCad")(uri "${KICAD9_SYMBOL_DIR}/Device.kicad_sym")(options "")(descr "Generic symbols for common devices"))
  (lib (name "power")(type "KiCad")(uri "${KICAD9_SYMBOL_DIR}/power.kicad_sym")(options "")(descr "Power symbols, special power flags"))
)
"""  # noqa: E501
# KiCad 10.99 template: (version 7), tab indent, spaces between tokens (CRLF
# as installed on Windows).
K11_FP = ("(fp_lib_table\r\n\t(version 7)\r\n"
          '\t(lib (name "MountingHole") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/'
          'MountingHole.pretty") (options "") (descr "Mechanical fasteners"))\r\n'
          '\t(lib (name "Resistor_SMD") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/'
          'Resistor_SMD.pretty") (options "") (descr "Resistor footprints, surface mount '
          '(SMD)"))\r\n)\r\n')
K11_DEVICE_ROW = ('(lib (name "Device") (type "KiCad") (uri "${KICAD10_SYMBOL_DIR}/'
                  'Device.kicad_sym") (options "") (descr "Generic symbols for common devices"))')
# Written by the KiCad 11 nightly: `pcb import` (Altium + EasyEDA into one dir)
# and `sch import` of an EasyEDA Pro project.
IMPORT_FP = """(fp_lib_table
\t(version 7)
\t(lib (name "eDP_adapter_dvt1-import-fps") (type "KiCad") (uri "${KIPRJMOD}/eDP_adapter_dvt1-import-fps.pretty") (options "kicad_import_cache=1") (descr ""))
\t(lib (name "pcb_watch-import-fps") (type "KiCad") (uri "${KIPRJMOD}/pcb_watch-import-fps.pretty") (options "kicad_import_cache=1") (descr ""))
)
"""  # noqa: E501
IMPORT_SYM = """(sym_lib_table
\t(version 7)
\t(lib (name "yuzuki-import-syms") (type "KiCad") (uri "${KIPRJMOD}/yuzuki-import-syms.kicad_sym") (options "kicad_import_cache=1") (descr ""))
)
"""  # noqa: E501


# --- parsing both syntaxes ----------------------------------------------------
def test_parses_both_row_syntaxes_kicad9_to_11_and_import_caches():
    t = sx.parse_table(K9_FP)                                   # KiCad 9, old unquoted
    assert t.tag == "fp_lib_table" and t.version is None
    assert [r.name for r in t.rows] == ["Audio_Module", "Connector_BarrelJack", "Resistor_SMD"]
    first = t.rows[0]
    assert first.type == "Kicad" and first.is_kicad          # legacy spelling still KiCad
    assert first.uri == "${KICAD9_FOOTPRINT_DIR}/Audio_Module.pretty"
    assert first.options == "" and first.descr == "Audio Module footprints"
    assert t.rows[1].descr == "(DC) barrel jack connector footprints"
    assert not any(r.disabled or r.hidden or r.managed for r in t.rows)
    t = sx.parse_table(K9_SYM)                                  # KiCad 9, quoted
    assert t.tag == "sym_lib_table" and t.version is None
    assert {r.name: r.uri for r in t.rows} == {
        "Device": "${KICAD9_SYMBOL_DIR}/Device.kicad_sym",
        "power": "${KICAD9_SYMBOL_DIR}/power.kicad_sym"}
    t = sx.parse_table(K11_FP)                                  # (version 7), tabs, CRLF
    assert t.version == 7 and [r.name for r in t.rows] == ["MountingHole", "Resistor_SMD"]
    assert t.rows[1].descr == "Resistor footprints, surface mount (SMD)"
    fp, sym = sx.parse_table(IMPORT_FP), sx.parse_table(IMPORT_SYM)
    assert all(r.import_cache for r in fp.rows + sym.rows)
    assert fp.rows[0].option_map == {"kicad_import_cache": "1"}
    assert not sx.LibRow("X", "u", options="a=1|b").import_cache
    assert sx.LibRow("X", "u", options="a=1|b").option_map == {"a": "1", "b": ""}
    assert sx.parse_options(" a = 1 || b ") == {"a": "1", "b": ""}
    assert [sx.is_import_cache(o) for o in ("kicad_import_cache=1", "kicad_import_cache=0",
                                            "kicad_import_cache=false", "")] == [
        True, False, False, False]


def test_flags_escapes_and_unknown_tokens_round_trip():
    text = ('(fp_lib_table (version 7)\n'
            '  (lib (name "My \\"Lib\\"")(type "KiCad")(uri "C:\\\\libs\\\\x.pretty")'
            '(options "")(descr "d")(disabled)(hidden)(future_token 1))\n'
            '  (future_top (a b))\n)\n')
    t = sx.parse_table(text)
    row = t.rows[0]
    assert row.name == 'My "Lib"' and row.uri == "C:\\libs\\x.pretty"
    assert row.disabled and row.hidden
    out = sx.format_table(t)
    assert t.extra == ((1, "(future_top (a b))"),)          # verbatim, after one row
    assert row.raw in out and out.index(row.raw) < out.index("(future_top (a b))")
    assert sx.parse_table(out) == t
    built = sx.LibRow('My "Lib"', "C:\\libs\\x.pretty", disabled=True, hidden=True)
    assert sx.parse_table(f"(fp_lib_table {built.to_sexp()})").rows[0] == built
    raw = sx.parse_table('(fp_lib_table (lib (name a)(type KiCad)(uri "C:\\libs\\x")'
                         '(descr "1\\n2")))').rows[0]    # unknown pairs stay as written
    assert (raw.uri, raw.descr) == ("C:\\libs\\x", "1\n2")


def test_new_rows_are_written_in_kicad11_style():
    row = sx.LibRow("Device", "${KICAD10_SYMBOL_DIR}/Device.kicad_sym",
                    descr="Generic symbols for common devices")
    assert row.to_sexp() == K11_DEVICE_ROW
    text = sx.format_table(sx.LibTable("sym_lib_table", (row,)))
    assert text == f"(sym_lib_table\n\t(version 7)\n\t{K11_DEVICE_ROW}\n)\n"
    assert sx.LibRow("M", "m.pretty", descr=sx.MANAGED_DESCR).managed


@pytest.mark.parametrize("text, needle", [
    ("", "empty"),
    ("(fp_lib_table (lib (name a)(type KiCad)(uri b))", "unclosed"),
    ("(fp_lib_table))", "unbalanced"),
    ("(fp_lib_table) (sym_lib_table)", "after the table"),
    ('(fp_lib_table (lib (name "a)))', "unterminated"),
    ("(kicad_pcb (version 1))", "not a library table"),
    ("(fp_lib_table (lib (name a)(type KiCad)))", "uri"),
    ("(fp_lib_table (lib (name a)(uri b)))", "type"),
    ("(fp_lib_table (lib (name a)(name b)(type KiCad)(uri c)))", "duplicate"),
    ("(fp_lib_table (version seven))", "version"),
    ("(fp_lib_table (lib name))", "malformed"),
    ("(fp_lib_table (lib (name a)(type KiCad)(uri b)(disabled yes)))", "disabled"),
    ("(fp_lib_table stray)", "stray"),
    ('(fp_lib_table (lib (name a b)(type KiCad)(uri c)))', "one value"),
    ("(fp_lib_table (lib (type KiCad)(uri b)))", "no .name"),
    ("lib (fp_lib_table)", "expected '\\('"),
])
def test_malformed_tables_fail_loudly(text, needle):
    with pytest.raises(sx.LibTableError, match=f"(?i)bad.table.*{needle}"):
        sx.parse_table(text, source="bad.table")


def test_table_file_errors_name_the_file_and_a_failed_write_leaves_no_debris(tmp_path,
                                                                             monkeypatch):
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table (lib", encoding="utf-8")
    with pytest.raises(sx.LibTableError, match="fp-lib-table"):
        sx.read_table(table)
    with pytest.raises(sx.LibTableError, match="cannot read library table"):
        sx.read_table(tmp_path / "missing" / "sym-lib-table")
    table.write_text(K9_FP, encoding="utf-8")

    def locked(src, dst):
        raise PermissionError("the file is open in another program")
    monkeypatch.setattr(sx.os, "replace", locked)
    with pytest.raises(PermissionError):
        sx.write_table(table, sx.LibTable("fp_lib_table", (sx.LibRow("X", "x.pretty"),)))
    assert [p.name for p in tmp_path.iterdir()] == ["fp-lib-table"]
    assert table.read_text(encoding="utf-8") == K9_FP


def test_write_then_read_round_trips_through_a_file(tmp_path: Path):
    t = sx.parse_table(K11_FP)
    sx.write_table(tmp_path / "fp-lib-table", t)
    assert sx.read_table(tmp_path / "fp-lib-table") == t
    (tmp_path / "bom").write_text(K9_SYM, encoding="utf-8-sig")       # Notepad's BOM
    assert [r.name for r in sx.read_table(tmp_path / "bom").rows] == ["Device", "power"]


# --- header_value: a library file's (generator ...) / (version ...) ----------------
SYM_HEAD = ('(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "kicad-eda")\n'
            '\t(generator_version "9.0")\n\t(symbol "R" (property "Reference" "R"')  # cut off
FP_HEAD = ('(footprint "LGA-79 \\"Quectel\\"" (version 20241229) (generator "kicad-eda") '
           '(layer "F.Cu") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))\n')


@pytest.mark.parametrize("text, tag, want", [
    (SYM_HEAD, "generator", "kicad-eda"),          # a truncated file head is enough
    (SYM_HEAD, "version", "20241209"),
    (SYM_HEAD, "generator_version", "9.0"),
    (FP_HEAD, "generator", "kicad-eda"),           # the quoted name atom is skipped
    (FP_HEAD, "layer", "F.Cu"),
    ("(footprint R (version 20221018) (generator pcbnew) (layer F.Cu))", "generator",
     "pcbnew"),                                    # KiCad 6-8 wrote it bare
    ("\ufeff(kicad_symbol_lib (generator \"kicad_symbol_editor\"))", "generator",
     "kicad_symbol_editor"),
    ("(module R_0603 (layer F.Cu) (tedit 5F68FEEE))", "generator", None),   # KiCad 5
    ('(kicad_symbol_lib (version 1) (symbol "X" (generator "kicad-eda")))', "generator",
     None),                                        # nested deeper: not the header
    ('(kicad_symbol_lib (generator "a" "b"))', "generator", None),          # not (tag value)
    ('(kicad_symbol_lib (generator (x)))', "generator", None),
    ('(kicad_symbol_lib (version 1)) (generator "kicad-eda")', "generator", None),  # after root
    ('generator "kicad-eda"', "generator", None),
    ('(kicad_symbol_lib (generator "kicad-eda', "generator", None),          # unterminated
    ("(kicad_symbol_lib))) (generator x)", "generator", None),
    ("", "generator", None),
])
def test_header_value_reads_only_the_root_lists_own_items(text, tag, want):
    assert sx.header_value(text, tag) == want


# --- libtable keeps the wave-1 API ------------------------------------------------
SEXP_API = ("LibRow", "LibTable", "LibTableError", "parse_table", "read_table",
            "format_table", "write_table", "TABLE_VERSION", "MANAGED_DESCR",
            "IMPORT_CACHE_OPTION")
CONSUMER_API = ("read_table", "TABLE_FILES", "MAP_KEYS", "LIB_SUFFIX", "SYMBOL", "FOOTPRINT",
                "write_minimal_kicad_pro", "expand_vars", "LibTableError", "LibraryResolver")


def test_libtable_reexports_the_sexp_layer_for_wave1_consumers():
    for name in SEXP_API:                     # the very same objects, not copies
        assert getattr(libtable, name) is getattr(sx, name), name
    for name in CONSUMER_API:   # foreign_formats, foreign_import, kicad_import_fplib, vision_*
        assert hasattr(libtable, name), name
    assert callable(libtable.LibraryResolver.for_design)
    assert issubclass(libtable.LibraryNotFoundError, sx.LibTableError)
    with pytest.raises(libtable.LibTableError, match="empty"):
        libtable.parse_table("")


# --- stdlib only (KiCad 9's bundled Python imports both modules) ----------------------
@pytest.mark.parametrize("module", sorted(OUR_MODULES))
def test_module_imports_only_the_standard_library_and_loads_without_site_packages(module):
    tree = ast.parse((TOOLS / f"{module}.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            names.add(node.module.split(".")[0])
    foreign = names - OUR_MODULES - set(sys.stdlib_module_names)
    assert names and not foreign, foreign
    code = (f"import sys; sys.path.insert(0, sys.argv[1]); import {module}; "
            "extra = {m.split('.')[0] for m in sys.modules} - set(sys.stdlib_module_names)"
            f" - set({sorted(OUR_MODULES)!r}) - {{'__main__'}}; print(sorted(extra))")
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(TOOLS)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


# --- live tier ------------------------------------------------------------------------
FAMILIES = {"9": lambda v: v[0] == 9, "10": lambda v: v[0] == 10 and v[1] < 99,
            "11": lambda v: v >= (10, 99, 0)}


def kicad_install(family: str) -> env.KiCadInstall:
    """The first discovered KiCad of ``family`` ("9" | "10" | "11"), else skip."""
    for inst in env.discover():
        if FAMILIES[family](inst.version_tuple):
            return inst
    pytest.skip(f"no KiCad {family} install")


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_live_full_templates_parse(family):
    inst = kicad_install(family)
    for name in ("sym-lib-table", "fp-lib-table"):
        path = inst.share / "template" / name
        text = path.read_text(encoding="utf-8")
        table = sx.read_table(path)
        assert len(table.rows) == text.count("(lib (name") > 50
        assert len({r.name for r in table.rows}) == len(table.rows)
        assert all(r.uri.startswith("${KICAD") and r.is_kicad for r in table.rows)
