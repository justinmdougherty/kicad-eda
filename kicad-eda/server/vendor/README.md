# server/vendor — third-party code shipped with the plugin

## kipy (kicad-python)

`kipy/` is the official KiCad IPC-API Python binding, **kicad-python**, vendored
from its `main` branch because the PyPI release (0.8.0) predates the schematic
API: `kipy.schematic` does not import there. The copy here carries protobuf
classes generated from KiCad master's `api/proto`, which is what the KiCad 11
nightly (`kicad-cli api-server`, 10.99.x) speaks. Licence: MIT, see
`kipy-LICENSE` (copyright The KiCad Developers).

`kicad_server.py` puts this directory at the **front** of `sys.path` before the
first `import kipy` (`kicad_env.use_vendored_kipy()`), so the vendored copy wins
over any pip-installed kicad-python. Do not `pip install kicad-python` into the
server venv; its runtime deps are pinned in `server/requirements.txt`
(`protobuf`, `pynng`, `jsonschema`, `zstandard`, `typing_extensions`).

### Origin

Refreshed by `server/tools/update_kipy.py`; the block below is rewritten on
every run.

<!-- origin:begin -->
```json
{
  "kicad_python_commit": "8c8752791179bf16db6abb1381e52d735b6b8c31",
  "kicad_python_version": "0.9.0.dev0",
  "kicad_ref": "master",
  "kicad_proto_commit": "032d9b012400e2e0f1dd08d02b5f486afe9814a3",
  "fetched": "2026-09-17"
}
```
<!-- origin:end -->

### What differs from the upstream checkout

- `kipy/proto/**/*_pb2.py` / `.pyi` are generated (not tracked upstream).
  Their cross-imports are rewritten from `from common.types import ...` to
  `from kipy.proto.common.types import ...` because protoletariat's in-place
  rewrite does nothing on Windows; the `.pyi` stubs additionally have their
  bare `common.`/`board.`/`schematic.` references qualified so type checkers
  resolve them.
- `kipy/kicad_api_version.py` is `"unknown"`: there is no KiCad submodule to
  `git describe`, and `"unknown"` makes `KiCad.check_version()` a no-op instead
  of raising `FutureVersionError` against the nightly.
- `__pycache__`, `build/`, `dist/` and egg-info are not copied.

### Regenerating

```sh
# full recipe: clone kicad-python main, fetch KiCad master api/proto via the
# GitLab archive API, build a temp venv with protoc-wheel-0==29
# protoletariat==3.3.* mypy-protobuf==3.6.*, generate, rewrite, vendor
.venv/Scripts/python.exe server/tools/update_kipy.py

# offline: reuse a checkout, a downloaded archive and a venv that already has
# protoc / protol / protoc-gen-mypy
.venv/Scripts/python.exe server/tools/update_kipy.py \
    --checkout ../kicad-python --proto-archive proto.tar.gz --tools-venv .venv-k11

# re-vendor a checkout whose protos are already generated (no fetch / protoc)
.venv/Scripts/python.exe server/tools/update_kipy.py --checkout ../kicad-python --skip-generate
```

The script never edits the checkout it is given (it works on a copy), ends with
an import check of `kipy.schematic` from the vendored path, and prints the
origin JSON it wrote into this file. After a refresh run the API tier of the
test-suite (`tests/test_sch_api.py`) against the nightly.
