#!/usr/bin/env python3
"""Refresh the vendored kipy (server/vendor/kipy) from kicad-python + KiCad protos.

The PyPI kicad-python (0.8.0) predates the schematic API: its kipy.schematic
cannot import. The vendored copy is kicad-python main with protobuf classes
generated from KiCad master's api/proto, which is what the KiCad 11 nightly
speaks. This script reproduces that build end-to-end, never touching the
checkout it is given (it works on a copy):

  1. clone kicad-python (or copy --checkout) into the work dir
  2. fetch KiCad <ref> api/proto via the GitLab archive API (or --proto-archive)
     and unpack it over <checkout>/kicad/api/proto
  3. create a tools venv with protoc-wheel-0==29, protoletariat==3.3.*,
     mypy-protobuf==3.6.* (or reuse --tools-venv)
  4. run tools/generate_protos.py (protoc + mypy stubs + protoletariat)
  5. rewrite "from (common|board|schematic)... import" to
     "from kipy.proto.\\1... import" in *_pb2.py / *_pb2.pyi (protoletariat does
     not rewrite on Windows); the .pyi stubs also get their bare
     common./board./schematic. references qualified
  6. restore the tracked kipy/proto/**/__init__.py (the re-export shims)
  7. write kipy/kicad_api_version.py = "unknown" (no submodule to describe;
     "unknown" disables kipy's version check instead of tripping it)
  8. copy kipy/ to --dest (default server/vendor/kipy), LICENSE to
     kipy-LICENSE, and refresh the origin block in server/vendor/README.md

Usage:
  python server/tools/update_kipy.py                       # full recipe from the network
  python server/tools/update_kipy.py --checkout DIR --proto-archive proto.tar.gz
  python server/tools/update_kipy.py --checkout DIR --skip-generate   # re-vendor as is
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KICAD_PYTHON_REPO = "https://gitlab.com/kicad/code/kicad-python.git"
KICAD_ARCHIVE_URL = ("https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/"
                     "repository/archive.tar.gz?sha={ref}&path=api/proto")
TOOL_PINS = ("protoc-wheel-0==29", "protoletariat==3.3.*", "mypy-protobuf==3.6.*")
DEFAULT_DEST = Path(__file__).resolve().parents[1] / "vendor"
API_VERSION_VALUE = "unknown"       # must match kipy.kicad._UNKNOWN_API_VERSION
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "build", "dist", "*.egg-info")
README_BEGIN = "<!-- origin:begin -->"
README_END = "<!-- origin:end -->"

_FROM_RE = re.compile(r"^from (common|board|schematic)((?:\.\w+)*) import ", re.M)
_REF_RE = re.compile(r"(?<![\w.])(common|board|schematic)\.((?:\w+\.)*\w+_pb2)\b")


class UpdateError(RuntimeError):
    """A recipe step failed; the message says which and why."""


@dataclass(frozen=True)
class Origin:
    kicad_python_commit: str
    kicad_python_version: str
    kicad_ref: str
    kicad_proto_commit: str
    fetched: str

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _log(msg: str) -> None:
    print(f"[update_kipy] {msg}", flush=True)


def _check(argv: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    """Run a step; a non-zero exit becomes an UpdateError carrying the output."""
    r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError(f"{' '.join(argv)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r.stdout


# ----------------------------------------------------------------------------
# 1. checkout
# ----------------------------------------------------------------------------
def prepare_checkout(work: Path, checkout: Path | None) -> Path:
    """A private copy of kicad-python in work/ (clone, or copy of --checkout)."""
    dest = work / "kicad-python"
    if checkout is not None:
        if not (checkout / "kipy").is_dir():
            raise UpdateError(f"--checkout {checkout} has no kipy/ package")
        _log(f"copying checkout {checkout} -> {dest}")
        shutil.copytree(checkout, dest, ignore=COPY_IGNORE, symlinks=True)
        return dest
    _log(f"cloning {KICAD_PYTHON_REPO}")
    _check(["git", "clone", "--depth", "1", KICAD_PYTHON_REPO, str(dest)])
    return dest


def checkout_commit(checkout: Path) -> str:
    try:
        return _check(["git", "rev-parse", "HEAD"], cwd=checkout).strip()
    except UpdateError as e:
        _log(f"no git metadata in checkout ({e.args[0].splitlines()[0]})")
        return "unknown"


def checkout_version(checkout: Path) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (checkout / "pyproject.toml").read_text(), re.M)
    return m.group(1) if m else "unknown"


# ----------------------------------------------------------------------------
# 2. protos
# ----------------------------------------------------------------------------
def fetch_proto_archive(work: Path, ref: str, archive: Path | None) -> Path:
    if archive is not None:
        if not archive.is_file():
            raise UpdateError(f"--proto-archive {archive} does not exist")
        return archive
    url = KICAD_ARCHIVE_URL.format(ref=ref)
    target = work / "kicad-api-proto.tar.gz"
    _log(f"fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, target.open("wb") as out:
            shutil.copyfileobj(resp, out)
    except OSError as e:
        raise UpdateError(f"cannot download KiCad protos from {url}: {e}") from e
    return target


def unpack_protos(archive: Path, checkout: Path) -> str:
    """Unpack */api/proto/** into <checkout>/kicad/api/proto; returns the KiCad
    commit encoded in the archive's top-level directory name."""
    proto_dir = checkout / "kicad" / "api" / "proto"
    if proto_dir.exists():
        shutil.rmtree(proto_dir)
    proto_dir.mkdir(parents=True)
    commit = "unknown"
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if "proto" not in parts or not member.isfile():
                continue
            top = parts[0]
            m = re.match(r"^kicad-.*-([0-9a-f]{40})-api-proto$", top)
            commit = m.group(1) if m else commit
            rel = Path(*parts[parts.index("proto") + 1:])
            out = proto_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise UpdateError(f"cannot read {member.name} from {archive}")
            out.write_bytes(src.read())
    found = sorted(p.relative_to(proto_dir).as_posix() for p in proto_dir.rglob("*.proto"))
    if not found:
        raise UpdateError(f"{archive} holds no .proto files under */api/proto/")
    _log(f"unpacked {len(found)} .proto files (KiCad commit {commit})")
    return commit


# ----------------------------------------------------------------------------
# 3. tools venv
# ----------------------------------------------------------------------------
def scripts_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def tools_venv(work: Path, python: str, reuse: Path | None) -> Path:
    """A venv holding protoc, protol and protoc-gen-mypy; returns its venv root."""
    if reuse is not None:
        if not scripts_dir(reuse).is_dir():
            raise UpdateError(f"--tools-venv {reuse} is not a virtualenv")
        return reuse
    venv = work / "tools-venv"
    _log(f"creating tools venv {venv}")
    _check([python, "-m", "venv", str(venv)])
    vpy = scripts_dir(venv) / ("python.exe" if os.name == "nt" else "python")
    _check([str(vpy), "-m", "pip", "install", "--quiet", *TOOL_PINS])
    return venv


def _tool(venv: Path, name: str) -> Path:
    exe = scripts_dir(venv) / (f"{name}.exe" if os.name == "nt" else name)
    if not exe.is_file():
        raise UpdateError(f"{exe} missing: is {name} installed in the tools venv?")
    return exe


# ----------------------------------------------------------------------------
# 4-7. generate, rewrite, restore, version file
# ----------------------------------------------------------------------------
def generate(checkout: Path, venv: Path) -> None:
    """tools/generate_protos.py with protoc/protol from the venv and the venv's
    Scripts dir first on PATH (protoc finds protoc-gen-mypy there)."""
    env = dict(os.environ)
    env["PATH"] = str(scripts_dir(venv)) + os.pathsep + env.get("PATH", "")
    vpy = scripts_dir(venv) / ("python.exe" if os.name == "nt" else "python")
    _log("generating protobuf classes")
    _check([str(vpy), "tools/generate_protos.py",
            "--protoc", str(_tool(venv, "protoc")), "--protol", str(_tool(venv, "protol"))],
           cwd=checkout, env=env)


def rewrite_imports(proto_dir: Path) -> int:
    """Qualify the generated modules' cross-imports under kipy.proto; idempotent.
    Returns the number of files changed."""
    changed = 0
    for path in sorted(proto_dir.rglob("*_pb2.py*")):
        with path.open(encoding="utf-8", newline="") as f:     # keep LF as generated
            text = f.read()
        new = _FROM_RE.sub(r"from kipy.proto.\1\2 import ", text)
        if path.suffix == ".pyi":
            new = _REF_RE.sub(r"kipy.proto.\1.\2", new)
        if new != text:
            with path.open("w", encoding="utf-8", newline="") as f:
                f.write(new)
            changed += 1
    return changed


def snapshot_inits(proto_dir: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in sorted(proto_dir.rglob("__init__.py"))}


def restore_inits(snapshot: dict[Path, bytes]) -> None:
    for path, content in snapshot.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def write_version_file(kipy_dir: Path, value: str = API_VERSION_VALUE) -> None:
    (kipy_dir / "kicad_api_version.py").write_text(
        "# Generated by server/tools/update_kipy.py, do not edit.\n"
        f'KICAD_API_VERSION = "{value}"\n', encoding="utf-8")


# ----------------------------------------------------------------------------
# 8. install into server/vendor
# ----------------------------------------------------------------------------
def install_vendor(checkout: Path, dest: Path) -> None:
    kipy_src = checkout / "kipy"
    kipy_dst = dest / "kipy"
    _log(f"installing {kipy_src} -> {kipy_dst}")
    if kipy_dst.exists():
        shutil.rmtree(kipy_dst)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(kipy_src, kipy_dst, ignore=COPY_IGNORE)
    license_src = checkout / "LICENSE"
    if not license_src.is_file():
        raise UpdateError(f"{license_src} missing: refusing to vendor without the MIT licence")
    shutil.copyfile(license_src, dest / "kipy-LICENSE")


def update_readme(readme: Path, origin: Origin) -> None:
    """Rewrite the block between the origin markers (README kept by hand otherwise)."""
    block = "\n".join([README_BEGIN,
                       "```json", json.dumps(origin.as_dict(), indent=2), "```",
                       README_END])
    if not readme.is_file():
        readme.write_text(f"# Vendored kipy\n\n{block}\n", encoding="utf-8")
        return
    text = readme.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(README_BEGIN) + r".*?" + re.escape(README_END), re.S)
    if not pattern.search(text):
        raise UpdateError(f"{readme} has no {README_BEGIN} ... {README_END} block")
    readme.write_text(pattern.sub(lambda _m: block, text), encoding="utf-8")


def verify_import(dest: Path, python: str) -> None:
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import kipy, kipy.schematic, "
            "kipy.schematic_types; print(kipy.__file__)")
    out = _check([python, "-B", "-c", code, str(dest)])     # -B: no __pycache__ in vendor
    _log(f"import check: {out.strip()}")


# ----------------------------------------------------------------------------
def run(args: argparse.Namespace, work: Path) -> Origin:
    checkout = prepare_checkout(work, args.checkout)
    proto_commit = "unchanged"
    if not args.skip_generate:
        archive = fetch_proto_archive(work, args.kicad_ref, args.proto_archive)
        proto_commit = unpack_protos(archive, checkout)
        venv = tools_venv(work, args.python, args.tools_venv)
        inits = snapshot_inits(checkout / "kipy" / "proto")
        generate(checkout, venv)
        restore_inits(inits)
    changed = rewrite_imports(checkout / "kipy" / "proto")
    _log(f"import rewrite touched {changed} generated files")
    write_version_file(checkout / "kipy")
    install_vendor(checkout, args.dest)
    origin = Origin(kicad_python_commit=checkout_commit(checkout),
                    kicad_python_version=checkout_version(checkout),
                    kicad_ref=args.kicad_ref, kicad_proto_commit=proto_commit,
                    fetched=args.fetched)
    update_readme(args.dest / "README.md", origin)
    verify_import(args.dest, args.python)
    return origin


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkout", type=Path, help="reuse this kicad-python checkout (copied)")
    p.add_argument("--kicad-ref", default="master", help="KiCad ref for api/proto")
    p.add_argument("--proto-archive", type=Path, help="reuse a downloaded api/proto tar.gz")
    p.add_argument("--tools-venv", type=Path, help="reuse a venv with protoc/protol/mypy")
    p.add_argument("--skip-generate", action="store_true",
                   help="do not fetch/generate; vendor the checkout's protos as they are")
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="server/vendor dir")
    p.add_argument("--work", type=Path, help="scratch dir (default: a temp dir)")
    p.add_argument("--python", default=sys.executable, help="Python for the tools venv")
    p.add_argument("--fetched", default=datetime.date.today().isoformat(),
                   help="date recorded in the README origin block")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.work is not None:
            args.work.mkdir(parents=True, exist_ok=True)
            origin = run(args, args.work)
        else:
            with tempfile.TemporaryDirectory(prefix="update_kipy_") as tmp:
                origin = run(args, Path(tmp))
    except UpdateError as e:
        print(f"update_kipy: {e}", file=sys.stderr)
        return 1
    print(json.dumps(origin.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
