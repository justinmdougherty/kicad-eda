"""decisions — the D5 runtime decision log (<stem>.decisions.jsonl) and the
netlist "assumptions" block validator.

Pure unit tier: stdlib + tmp_path only, no KiCad (the multi-process test
spawns plain Python writers).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import decisions as dec
import netlist_schema

TOOLS = Path(__file__).resolve().parents[1] / "server" / "tools"

D5_FIELDS = ("id", "ts", "stage", "question", "decision", "rationale", "alternatives",
             "confidence", "source", "reversible", "override")


def _fields(**over) -> dict:
    """A complete, valid set of caller fields (id/ts are generated)."""
    base = {"stage": "G2", "question": "U1 rail VIN has no bypass cap: add one?",
            "decision": "added 100nF C5 on VIN at U1",
            "rationale": "standard bypass; unambiguous remedy",
            "confidence": "high", "source": "policy:add_decoupling"}
    base.update(over)
    return base


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# --- vocabulary ---------------------------------------------------------------

def test_vocabulary_matches_d5():
    assert dec.STAGES == ("requirements", "parts", "G1", "G2", "G3", "G4", "G5", "routing")
    assert dec.CONFIDENCE_LEVELS == netlist_schema.CONFIDENCE_LEVELS == ("high", "med", "low")
    assert dec.ENTRY_FIELDS == D5_FIELDS


# --- decisions_path -----------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("work/netlist.json", "work/netlist.decisions.jsonl"),
    ("work/netlist", "work/netlist.decisions.jsonl"),
    ("work/netlist.kicad_pcb", "work/netlist.decisions.jsonl"),
    ("work/netlist.kicad_sch", "work/netlist.decisions.jsonl"),
    ("work/netlist.kicad_pro", "work/netlist.decisions.jsonl"),
    ("work/netlist.decisions.jsonl", "work/netlist.decisions.jsonl"),
    ("work/my.board.json", "work/my.board.decisions.jsonl"),
])
def test_decisions_path_resolves_the_stem(given, expected):
    assert dec.decisions_path(given) == Path(expected)


# --- append -------------------------------------------------------------------

def test_append_writes_one_complete_d5_line(tmp_path):
    netlist = tmp_path / "netlist.json"
    entry = dec.append_decision(netlist, **_fields(alternatives=["leave it", "10uF"]))
    log = tmp_path / "netlist.decisions.jsonl"
    lines = _lines(log)
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk == entry
    assert tuple(on_disk) == D5_FIELDS                    # every field, in D5 order
    assert re.fullmatch(r"dec-[0-9a-f]{12}", entry["id"])
    ts = datetime.fromisoformat(entry["ts"])
    assert ts.tzinfo is not None and ts.utcoffset().total_seconds() == 0
    assert entry["alternatives"] == ["leave it", "10uF"]
    assert entry["stage"] == "G2" and entry["confidence"] == "high"


def test_append_defaults_optional_fields(tmp_path):
    entry = dec.append_decision(tmp_path / "n.json", **_fields())
    assert entry["alternatives"] == []
    assert entry["reversible"] is True
    assert entry["override"] is None


def test_append_twice_reads_back_in_order_with_distinct_ids(tmp_path):
    stem = tmp_path / "board"
    a = dec.append_decision(stem, **_fields(stage="parts", confidence="low",
                                            override="components[U3].footprint"))
    b = dec.append_decision(stem, **_fields(stage="routing", reversible=False))
    got = dec.read_decisions(stem)
    assert got == [a, b]
    assert a["id"] != b["id"]
    assert got[0]["override"] == "components[U3].footprint"
    assert got[1]["reversible"] is False


def test_append_does_not_alias_caller_lists(tmp_path):
    alts = ["a", "b"]
    entry = dec.append_decision(tmp_path / "n.json", **_fields(alternatives=alts))
    alts.append("c")
    assert entry["alternatives"] == ["a", "b"]
    assert dec.read_decisions(tmp_path / "n.json")[0]["alternatives"] == ["a", "b"]


def test_append_accepts_a_tuple_of_alternatives(tmp_path):
    entry = dec.append_decision(tmp_path / "n.json", **_fields(alternatives=("x", "y")))
    assert entry["alternatives"] == ["x", "y"]


def test_append_treats_alternatives_none_as_none_recorded(tmp_path):
    """record_decision(..., alternatives=None) passes None straight through."""
    entry = dec.append_decision(tmp_path / "n.json", **_fields(alternatives=None))
    assert entry["alternatives"] == []
    assert dec.read_decisions(tmp_path / "n.json") == [entry]
    assert '"alternatives": []' in dec.decisions_path(tmp_path / "n.json").read_text("ascii")


def test_a_stored_null_alternatives_is_a_malformed_line(tmp_path):
    """Only append_decision's argument may be None: a stored entry always
    holds a list (append writes []), so a hand-edited or foreign null is a
    bad line, never an entry a reader has to special-case."""
    stem = tmp_path / "n.json"
    good = dec.append_decision(stem, **_fields())
    null = {**good, "id": "dec-00000000000f", "alternatives": None}
    with open(dec.decisions_path(stem), "a", encoding="ascii") as f:
        f.write(json.dumps(null) + "\n")
    assert dec.validate_decision(null) == ["alternatives must be a list of strings, got null"]
    log = dec.load_decisions(stem)
    assert log.entries == (good,)
    assert log.errors == (f"{dec.decisions_path(stem)}:2: alternatives must be a list of "
                          f"strings, got null",)
    with pytest.raises(dec.DecisionLogError, match="got null"):
        dec.read_decisions(stem)


def test_non_ascii_round_trips_and_the_log_stays_ascii(tmp_path):
    entry = dec.append_decision(tmp_path / "n.json",
                                **_fields(rationale="T_j = 125 °C max, θJA 40 °C/W"))
    assert dec.read_decisions(tmp_path / "n.json")[0]["rationale"] == entry["rationale"]
    raw = dec.decisions_path(tmp_path / "n.json").read_bytes()
    assert raw.decode("ascii") and raw.count(b"\n") == 1      # escaped: one line, pure ASCII


@pytest.mark.parametrize("separator", [" ", " ", "\u0085", "\x0b", "\x0c", "\r"])
def test_line_separator_characters_inside_a_field_round_trip(tmp_path, separator):
    """Text pasted from PDF datasheets carries U+2028/U+2029/U+0085, which
    str.splitlines() treats as line breaks."""
    stem = tmp_path / "n.json"
    first = dec.append_decision(stem, **_fields(rationale=f"datasheet 7.3:{separator}Vin max"))
    second = dec.append_decision(stem, **_fields(stage="G4"))
    assert dec.read_decisions(stem) == [first, second]
    assert dec.load_decisions(stem) == dec.DecisionLog((first, second), ())


def test_raw_separators_written_by_another_tool_still_read(tmp_path):
    """A writer that did not escape non-ASCII (ensure_ascii=False) is read too."""
    entry = {"id": "dec-0123456789ab", "ts": "2026-10-04T12:00:00+00:00",
             **_fields(rationale="a b\u0085c °C"), "alternatives": [], "reversible": True,
             "override": None}
    log = dec.decisions_path(tmp_path / "n.json")
    log.write_text(json.dumps(entry, ensure_ascii=False) + "\r\n", encoding="utf-8")
    assert dec.read_decisions(log) == [entry]


def test_a_torn_tail_inside_a_multibyte_character_costs_only_itself(tmp_path):
    stem = tmp_path / "n.json"
    log = dec.decisions_path(stem)
    good = dec.append_decision(stem, **_fields())
    torn = json.dumps({**good, "rationale": "125 °C"}, ensure_ascii=False).encode("utf-8")
    with open(log, "ab") as f:                              # a crash mid-character
        f.write(torn[:torn.index("°".encode("utf-8")) + 1])
    result = dec.load_decisions(stem)
    assert result.entries == (good,)
    assert len(result.errors) == 1 and f"{log}:2:" in result.errors[0]
    assert "UTF-8" in result.errors[0]
    new = dec.append_decision(stem, **_fields(stage="G4"))
    result = dec.load_decisions(stem)
    assert list(result.entries) == [good, new] and len(result.errors) == 1
    with pytest.raises(dec.DecisionLogError, match=re.escape(f"{log}:2:")):
        dec.read_decisions(stem)


def test_append_to_an_existing_empty_log(tmp_path):
    log = dec.decisions_path(tmp_path / "n.json")
    log.write_bytes(b"")
    entry = dec.append_decision(tmp_path / "n.json", **_fields())
    assert log.read_bytes() == (json.dumps(entry) + "\n").encode("ascii")


@pytest.mark.parametrize("over, needle", [
    ({"stage": "G6"}, "stage"),
    ({"stage": "g2"}, "stage"),
    ({"confidence": "medium"}, "confidence"),
    ({"question": ""}, "question"),
    ({"decision": "   "}, "decision"),
    ({"rationale": 42}, "rationale"),
    ({"source": None}, "source"),
    ({"alternatives": "10uF"}, "alternatives"),
    ({"alternatives": ["ok", ""]}, "alternatives[1]"),
    ({"alternatives": ["ok", 3]}, "alternatives[1]"),
    ({"reversible": "yes"}, "reversible"),
    ({"override": 5}, "override"),
    ({"override": ""}, "override"),
])
def test_append_rejects_bad_fields_and_writes_nothing(tmp_path, over, needle):
    stem = tmp_path / "n.json"
    with pytest.raises(dec.DecisionError) as e:
        dec.append_decision(stem, **_fields(**over))
    assert needle in str(e.value)
    assert not dec.decisions_path(stem).exists()


def test_append_names_every_missing_required_field(tmp_path):
    with pytest.raises(dec.DecisionError) as e:
        dec.append_decision(tmp_path / "n.json", stage="G3", decision="x")
    msg = str(e.value)
    for name in ("question", "rationale", "confidence", "source"):
        assert f"'{name}'" in msg


def test_append_rejects_unknown_and_generated_keys(tmp_path):
    with pytest.raises(dec.DecisionError, match="unknown"):
        dec.append_decision(tmp_path / "n.json", **_fields(confidance="high"))
    with pytest.raises(dec.DecisionError, match="generated"):
        dec.append_decision(tmp_path / "n.json", **_fields(id="mine"))
    with pytest.raises(dec.DecisionError, match="generated"):
        dec.append_decision(tmp_path / "n.json", **_fields(ts="2026-01-01T00:00:00+00:00"))


def test_append_after_a_torn_last_line_starts_a_new_line(tmp_path):
    log = tmp_path / "n.decisions.jsonl"
    good = dec.append_decision(tmp_path / "n.json", **_fields())
    with open(log, "a", encoding="utf-8", newline="\n") as f:
        f.write('{"id": "dec-torn", "stage": "G')          # a crash mid-write
    new = dec.append_decision(tmp_path / "n.json", **_fields(stage="G4"))
    result = dec.load_decisions(log)
    assert list(result.entries) == [good, new]
    assert len(result.errors) == 1 and ":2:" in result.errors[0]


# --- the writer lock (<log>.lock) -----------------------------------------------
BROKE = "decisions: broke a stale lock"                     # the stderr note


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_lock_path_sits_beside_the_log():
    assert dec.lock_path("work/n.json") == Path("work/n.decisions.jsonl.lock")
    assert dec.lock_path("work/n.decisions.jsonl") == Path("work/n.decisions.jsonl.lock")


def test_lock_constants_match_the_contract():
    assert dec.LOCK_RETRY_S == 0.01
    assert dec.LOCK_TIMEOUT_S == 10.0
    assert dec.LOCK_STALE_S == 60.0


def test_append_releases_its_lock(tmp_path):
    stem = tmp_path / "n.json"
    dec.append_decision(stem, **_fields())
    dec.append_decision(stem, **_fields(stage="G4"))
    assert not dec.lock_path(stem).exists()
    assert len(dec.read_decisions(stem)) == 2


def test_a_held_lock_times_out_with_decision_error_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dec, "LOCK_TIMEOUT_S", 0.3)
    stem = tmp_path / "n.json"
    first = dec.append_decision(stem, **_fields())
    before = dec.decisions_path(stem).read_bytes()
    lock = dec.lock_path(stem)
    lock.write_text("4242\n", encoding="ascii")             # another writer holds it
    start = time.monotonic()
    with pytest.raises(dec.DecisionError, match="lock") as e:
        dec.append_decision(stem, **_fields(stage="G4"))
    assert time.monotonic() - start >= 0.3
    assert str(lock) in str(e.value) and "nothing written" in str(e.value)
    assert dec.decisions_path(stem).read_bytes() == before   # nothing written
    assert lock.exists()                                     # a live lock is never taken over
    assert dec.read_decisions(stem) == [first]


def test_a_held_lock_with_no_log_yet_creates_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(dec, "LOCK_TIMEOUT_S", 0.05)
    stem = tmp_path / "n.json"
    dec.lock_path(stem).write_text("1\n", encoding="ascii")
    with pytest.raises(dec.DecisionError):
        dec.append_decision(stem, **_fields())
    assert not dec.decisions_path(stem).exists()


def test_a_stale_lock_is_broken_and_noted_on_stderr(tmp_path, capsys):
    stem = tmp_path / "n.json"
    lock = dec.lock_path(stem)
    lock.write_text("4242\n", encoding="ascii")             # a writer that crashed
    _age(lock, dec.LOCK_STALE_S + 30)
    entry = dec.append_decision(stem, **_fields())
    assert dec.read_decisions(stem) == [entry]
    assert not lock.exists()
    err = capsys.readouterr().err
    assert BROKE in err and str(lock) in err


def test_a_lock_younger_than_the_stale_age_is_not_broken(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dec, "LOCK_TIMEOUT_S", 0.1)
    stem = tmp_path / "n.json"
    lock = dec.lock_path(stem)
    lock.write_text("1\n", encoding="ascii")
    _age(lock, dec.LOCK_STALE_S - 10)
    with pytest.raises(dec.DecisionError):
        dec.append_decision(stem, **_fields())
    assert lock.exists() and BROKE not in capsys.readouterr().err


@pytest.mark.parametrize("removable, note", [
    (True, BROKE),                                           # reappears stale
    (False, "decisions: could not remove a stale lock"),     # will not go away
])
def test_a_stale_lock_is_broken_only_once_per_append(tmp_path, monkeypatch, capsys,
                                                     removable, note):
    """A stale lock that will not go away (or reappears stale) is broken
    once; after that the writer waits for the timeout like any held lock."""
    monkeypatch.setattr(dec, "LOCK_TIMEOUT_S", 0.2)
    removed = []
    monkeypatch.setattr(dec, "_remove_lock", lambda path: removed.append(path) or removable)
    stem = tmp_path / "n.json"
    lock = dec.lock_path(stem)
    lock.write_text("1\n", encoding="ascii")
    _age(lock, dec.LOCK_STALE_S + 30)
    with pytest.raises(dec.DecisionError):
        dec.append_decision(stem, **_fields())
    assert removed == [lock]
    assert capsys.readouterr().err.count(note) == 1
    assert not dec.decisions_path(stem).exists()


def test_a_lock_that_cannot_be_released_is_noted_and_the_entry_kept(tmp_path, monkeypatch,
                                                                     capsys):
    """The entry is already written: a release failure (Windows sharing) is
    noted on stderr, never raised; the next writer breaks the stale lock."""
    monkeypatch.setattr(dec, "LOCK_RELEASE_S", 0.05)
    monkeypatch.setattr(dec, "_remove_lock", lambda path: False)
    stem = tmp_path / "n.json"
    entry = dec.append_decision(stem, **_fields())
    assert dec.read_decisions(stem) == [entry]
    assert dec.lock_path(stem).exists()
    assert "decisions: could not remove the lock" in capsys.readouterr().err


def test_the_lock_is_taken_before_the_newline_check(tmp_path, monkeypatch):
    """The torn-tail check and the write run under the lock (a second writer
    could otherwise append between them)."""
    stem = tmp_path / "n.json"
    seen = []
    real = dec._ends_without_newline

    def spy(path):
        seen.append(dec.lock_path(stem).exists())
        return real(path)

    monkeypatch.setattr(dec, "_ends_without_newline", spy)
    dec.append_decision(stem, **_fields())
    assert seen == [True]


_WRITER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import decisions as dec
stem, index, count = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
work = Path(sys.argv[5])
(work / f"ready-{index}").touch()
deadline = time.monotonic() + 60
while not (work / "go").exists():
    if time.monotonic() > deadline:
        sys.exit("no go signal")
    time.sleep(0.002)
errors = 0
for i in range(count):
    try:
        dec.append_decision(stem, stage="routing", question=f"q {index}/{i}?",
                            decision=f"d {index}/{i}", rationale="contention test",
                            confidence="high", source=f"writer-{index}")
    except Exception as e:
        errors += 1
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
print(errors)
'''
WRITERS, APPENDS = 4, 200


def test_concurrent_writers_lose_no_entries(tmp_path):
    """F5a review round 3: 4 processes x 300 appends on Windows lost ~225
    entries silently ('a' mode is seek-then-write). With the lock: 4 x 200
    -> 800 valid entries, 0 errors."""
    stem, work = tmp_path / "n.json", tmp_path / "sync"
    work.mkdir()
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(TOOLS), str(stem), str(i),
                               str(APPENDS), str(work)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
             for i in range(WRITERS)]
    try:
        deadline = time.monotonic() + 60
        while len(list(work.glob("ready-*"))) < WRITERS and time.monotonic() < deadline:
            time.sleep(0.01)
        (work / "go").touch()                               # all start together
        outs = [p.communicate(timeout=180) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    for p, (out, err) in zip(procs, outs, strict=True):
        assert p.returncode == 0, err[-2000:]
        assert out.strip() == "0", err[-2000:]
    log = dec.load_decisions(stem)
    assert log.errors == ()
    assert len(log.entries) == WRITERS * APPENDS
    assert len({(e["source"], e["question"]) for e in log.entries}) == WRITERS * APPENDS
    assert not dec.lock_path(stem).exists()


# --- read / load --------------------------------------------------------------

def test_read_missing_log_is_empty(tmp_path):
    assert dec.read_decisions(tmp_path / "nothing.json") == []
    assert dec.load_decisions(tmp_path / "nothing.json") == dec.DecisionLog((), ())


def test_read_skips_blank_lines_and_tolerates_a_bom(tmp_path):
    entry = dec.append_decision(tmp_path / "n.json", **_fields())
    log = dec.decisions_path(tmp_path / "n.json")
    log.write_text("﻿\n" + json.dumps(entry) + "\n\n   \n", encoding="utf-8")
    assert dec.read_decisions(log) == [entry]


def test_read_rejects_a_corrupt_log_loudly_but_load_is_lenient(tmp_path):
    stem = tmp_path / "n.json"
    good = dec.append_decision(stem, **_fields())
    log = dec.decisions_path(stem)
    bad_entry = {**good, "id": "dec-000000000001", "confidence": "sure"}
    with open(log, "a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write("[1, 2]\n")
        f.write(json.dumps(bad_entry) + "\n")
    with pytest.raises(dec.DecisionLogError) as e:
        dec.read_decisions(stem)
    msg = str(e.value)
    assert f"{log}:2:" in msg and f"{log}:3:" in msg and f"{log}:4:" in msg
    assert "confidence" in msg
    result = dec.load_decisions(stem)
    assert list(result.entries) == [good]
    assert len(result.errors) == 3


def test_duplicate_ids_are_an_error(tmp_path):
    stem = tmp_path / "n.json"
    entry = dec.append_decision(stem, **_fields())
    with open(dec.decisions_path(stem), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    result = dec.load_decisions(stem)
    assert len(result.entries) == 1
    assert "duplicate id" in result.errors[0]


@pytest.mark.parametrize("over, needle", [
    ({"id": ""}, "id"),
    ({"ts": "yesterday"}, "ts"),
    ({"ts": "2026-10-04T12:00:00"}, "ts"),                  # no timezone
    ({"ts": 1759579200}, "timestamp string"),               # epoch seconds, not ISO
    ({"extra": 1}, "unknown"),
    ({"alternatives": None}, "alternatives must be a list of strings, got null"),
    ({"alternatives": "x"}, "alternatives must be a list of strings, got str"),
])
def test_validate_decision_checks_generated_fields(over, needle):
    entry = {"id": "dec-0123456789ab", "ts": "2026-10-04T12:00:00+00:00",
             **_fields(), "alternatives": [], "reversible": True, "override": None}
    assert dec.validate_decision(entry) == []
    problems = dec.validate_decision({**entry, **over})
    assert problems and needle in problems[0]


def test_validate_decision_requires_every_field():
    problems = dec.validate_decision({})
    for name in D5_FIELDS:
        assert any(f"'{name}'" in p for p in problems), name
    assert dec.validate_decision("x") == ["a decision entry must be a JSON object, got str"]


# --- assumptions --------------------------------------------------------------

def _assumption(**over) -> dict:
    base = {"id": "A1", "topic": "input power", "value": "USB-C 5 V sink",
            "rationale": "napkin default: USB-C is the cheapest universal input",
            "confidence": "med", "override": "net_specs.VBUS.voltage"}
    base.update(over)
    return base


def test_valid_assumptions_block():
    block = [_assumption(),
             _assumption(id="A2", topic="ambient", value=[0, 70], confidence="low",
                         override=None),
             _assumption(id="A3", topic="qty", value=5, confidence="high"),
             _assumption(id="A4", topic="regulatory", value={"fcc": "not assessed"})]
    del block[3]["override"]                                 # override is optional
    assert dec.validate_assumptions(block) == []
    assert dec.validate_assumptions(None) == []             # an absent block is valid


@pytest.mark.parametrize("block, needle", [
    ({"A1": "x"}, "must be a list"),
    (["A1"], "assumptions[0]: must be a JSON object"),
    ([_assumption(confidence="medium")], "confidence"),
    ([_assumption(value=None)], "value"),
    ([_assumption(id="")], "id"),
    ([_assumption(rationale="  ")], "rationale"),
    ([_assumption(override=3)], "override"),
    ([{"id": "A9"}], "missing"),
    ([_assumption(), _assumption(topic="other")], "duplicate id 'A1'"),
])
def test_invalid_assumptions_are_reported(block, needle):
    problems = dec.validate_assumptions(block)
    assert problems and any(needle in p for p in problems), problems


def test_assumption_messages_name_the_entry():
    problems = dec.validate_assumptions([_assumption(), _assumption(id="A7", confidence="x")])
    assert problems == ["assumptions[1] (id 'A7'): confidence must be one of high, med, low "
                        "(got 'x')"]


@pytest.mark.parametrize("block", [
    [_assumption()],
    [_assumption(confidence="medium")],
    [_assumption(value=None)],
    [_assumption(id="")],
    [_assumption(override=3)],
    [{"id": "A9", "topic": "t"}],
    ["A1"],
    [_assumption(value=3.5, override=None)],
])
def test_assumption_validator_agrees_with_the_netlist_schema(block):
    """Same verdict as the jsonschema contract on single-entry blocks (the
    schema cannot see duplicate ids or whitespace-only strings, see below)."""
    design = {"components": [{"ref": "R1", "lib_id": "Device:R",
                              "footprint": "Resistor_SMD:R_0603_1608Metric"}],
              "assumptions": block}
    schema_ok = not netlist_schema.validate(design)
    assert (dec.validate_assumptions(block) == []) == schema_ok


def test_assumption_validator_is_stricter_than_the_schema_where_documented():
    blank = [_assumption(rationale=" ")]
    dup = [_assumption(), _assumption()]
    for block in (blank, dup):
        design = {"components": [{"ref": "R1", "lib_id": "Device:R",
                                  "footprint": "Resistor_SMD:R_0603_1608Metric"}],
                  "assumptions": block}
        assert netlist_schema.validate(design) == []
        assert dec.validate_assumptions(block) != []


# --- purity -------------------------------------------------------------------

def test_module_is_stdlib_only_and_needs_no_kicad():
    tree = ast.parse((TOOLS / "decisions.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    local = {"netlist_schema"}
    assert imported - local <= set(sys.stdlib_module_names), imported
    assert "pcbnew" not in imported and "wx" not in imported
