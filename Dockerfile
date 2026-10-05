# kicad-eda — self-contained pipeline image.
# Everything the headless concept->Gerbers flow needs in one container:
# KiCad 10 (kicad-cli + pcbnew SWIG python), a JRE for the bundled
# Freerouting 1.9.0 jar, and the server's Python deps. This is also the
# zero-step answer to "how do I install the dependencies" when sharing.
#
# default-jre (NOT -headless) + xvfb: Freerouting 1.x initializes Swing
# even in CLI mode, and the headless JRE build has no X11 toolkit at all —
# the server wraps the router in xvfb-run when no display is present.
#
# Two Pythons, never mixed: /usr/bin/python3 is KiCad's (pcbnew SWIG, no
# numpy) and is left exactly as the base image ships it; /opt/venv is the
# server venv (no pcbnew) with server/requirements.txt, the optional extras
# of server/requirements-optional.txt (PyMuPDF for the vision path; more in
# later waves) and pytest, so tests run in the image without a per-run install.
#
#   docker build -t kicad-eda .
#   docker run --rm kicad-eda                                  # regression suite
#   docker run --rm -v %cd%\myboard:/work kicad-eda \
#     /opt/venv/bin/python /plugin/pipeline/run_pipeline.py \
#     /work/netlist.json 50 50                                  # your board
#   docker run --rm --entrypoint sh kicad-eda -c \
#     "cd /plugin && /opt/venv/bin/python -m pytest -p no:cacheprovider tests/<file> -q"

# KiCad 10.0.5, pinned by digest so a re-tag of kicad/kicad:10.0 cannot change
# the toolchain under the regression suite. This is the multi-arch index the
# 10.0.5 facts in ROADMAP_DECISIONS / adr_d7 were verified on (kicad-eda:wave1).
# .github/workflows/regression.yml pins the same digest; bump both together,
# and only after `docker run --rm kicad-eda` (run_all) is ALL PASS on the new one.
FROM kicad/kicad:10.0@sha256:182c8005cb775a2c448a4c18681d489f1ff472a761885eba3e08b07e3c0564de
USER root

ARG PYTEST_VERSION=9.1.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-jre python3-venv python3-pip xvfb \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (their own layer): editing plugin code does not reinstall them.
COPY kicad-eda/server/requirements.txt kicad-eda/server/requirements-optional.txt \
     /plugin/server/

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --quiet --no-cache-dir \
        -r /plugin/server/requirements.txt \
        -r /plugin/server/requirements-optional.txt \
        "pytest==${PYTEST_VERSION}"

COPY kicad-eda/ /plugin/

WORKDIR /plugin
CMD ["/opt/venv/bin/python", "/plugin/pipeline/run_all.py"]
