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
#   docker build -t kicad-eda .
#   docker run --rm kicad-eda                                  # regression suite
#   docker run --rm -v %cd%\myboard:/work kicad-eda \
#     /opt/venv/bin/python /plugin/pipeline/run_pipeline.py \
#     /work/netlist.json 50 50                                  # your board

FROM kicad/kicad:10.0
USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-jre python3-venv python3-pip xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY kicad-eda/ /plugin/

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --quiet -r /plugin/server/requirements.txt

WORKDIR /plugin
CMD ["/opt/venv/bin/python", "/plugin/pipeline/run_all.py"]
