#!/usr/bin/env bash
# kicad-eda external dependency installer (macOS / Linux).
# Idempotent: checks each dependency first, installs only what's missing.
# Python packages are handled automatically by scripts/launch-server.js;
# this script covers the three EXTERNAL tools the pipeline needs.
#
#   ./install-deps.sh             check + install (brew / apt / dnf)
#   ./install-deps.sh --check     report status, change nothing
set -uo pipefail

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1
OK=1

pkg_install() {
  if command -v brew >/dev/null 2>&1; then brew install "$1"
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get install -y "$2"
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y "$3"
  else echo "[FAIL] no brew/apt/dnf — install $1 manually"; return 1; fi
}

# --- KiCad 9/10 ---------------------------------------------------------------
if command -v kicad-cli >/dev/null 2>&1 \
   || [ -x "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" ]; then
  echo "[ok] KiCad: $(kicad-cli version 2>/dev/null | head -n1 || echo found)"
elif [ "$CHECK_ONLY" = 1 ]; then
  echo "[MISSING] KiCad — brew install --cask kicad | apt install kicad | dnf install kicad"
  OK=0
else
  echo "[..] Installing KiCad..."
  if command -v brew >/dev/null 2>&1; then brew install --cask kicad
  else pkg_install kicad kicad kicad; fi
fi

# --- Java 17-21 JRE (Freerouting 1.9.0 is bundled) -----------------------------
if command -v java >/dev/null 2>&1; then
  echo "[ok] Java: $(java -version 2>&1 | head -n1)"
elif [ "$CHECK_ONLY" = 1 ]; then
  echo "[MISSING] Java — brew install temurin@21 | apt install default-jre | dnf install java-21-openjdk"
  OK=0
else
  echo "[..] Installing Java..."
  if command -v brew >/dev/null 2>&1; then brew install --cask temurin@21
  else pkg_install temurin@21 default-jre java-21-openjdk; fi
fi

# --- Python 3.10+ (server venv builds itself on first launch) ------------------
if command -v python3 >/dev/null 2>&1; then
  echo "[ok] Python: $(python3 --version)"
elif [ "$CHECK_ONLY" = 1 ]; then
  echo "[MISSING] Python 3 — brew install python | apt install python3-venv"
  OK=0
else
  pkg_install python python3-venv python3
fi

# --- Freerouting jar (bundled — just verify) -----------------------------------
JAR="$(dirname "$0")/../server/tools/freerouting-1.9.0.jar"
if [ -f "$JAR" ]; then echo "[ok] Freerouting 1.9.0 jar bundled"
else echo "[MISSING] $JAR — re-install the plugin"; OK=0; fi

if [ "$OK" = 1 ]; then echo; echo "All dependencies present."
else echo; echo "Some dependencies missing — see above."; exit 1; fi
