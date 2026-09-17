#!/usr/bin/env bash
# kicad-eda external dependency installer (macOS / Linux).
# Idempotent: checks each dependency first, installs only what's missing.
# Python packages are handled automatically by scripts/launch-server.js;
# this script covers the three EXTERNAL tools the pipeline needs and REPORTS
# the optional KiCad 11 nightly (schematic API backend) without installing it.
#
#   ./install-deps.sh             check + install (brew / apt / dnf)
#   ./install-deps.sh --check     report status, change nothing
set -uo pipefail

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1
OK=1
NIGHTLY_URL="https://downloads.kicad.org/kicad/"   # windows/ macos/ linux/ ... explore/nightlies

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

# --- KiCad 11 nightly (OPTIONAL: schematic API backend; reported, never installed)
# A nightly calls itself 10.99.x and adds `kicad-cli api-server`, which is what
# the plugin's schematic backend launches. Distro nightlies ship kicad-cli-nightly.
NIGHTLY_CLI=""; NIGHTLY_VER=""; NIGHTLY_MM=""
for cand in "$(command -v kicad-cli-nightly 2>/dev/null || true)" \
            /usr/lib/kicad-nightly/bin/kicad-cli \
            /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli \
            "$(command -v kicad-cli 2>/dev/null || true)"; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  ver="$("$cand" version 2>/dev/null | head -n1)"
  major="${ver%%.*}"; rest="${ver#*.}"; minor="${rest%%.*}"; minor="${minor%%-*}"
  case "$major" in ''|*[!0-9]*) continue;; esac
  case "$minor" in ''|*[!0-9]*) continue;; esac
  if { [ "$major" -ge 11 ] || { [ "$major" -eq 10 ] && [ "$minor" -ge 99 ]; }; } \
     && "$cand" api-server --help >/dev/null 2>&1; then
    NIGHTLY_CLI="$cand"; NIGHTLY_VER="$ver"; NIGHTLY_MM="$major.$minor"; break
  fi
done
if [ -n "$NIGHTLY_CLI" ]; then
  echo "[ok] KiCad 11 nightly (optional, schematic API): $NIGHTLY_VER ($NIGHTLY_CLI)"
  case "$(uname -s)" in
    Darwin) NIGHTLY_CFG="$HOME/Library/Preferences/kicad/$NIGHTLY_MM";;
    *)      NIGHTLY_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/kicad/$NIGHTLY_MM";;
  esac
  if [ ! -f "$NIGHTLY_CFG/sym-lib-table" ]; then
    echo "[warn] $NIGHTLY_CFG/sym-lib-table missing — open KiCad 11 once, or copy sym-lib-table and fp-lib-table from the install's share/kicad/template into that folder; until then the headless schematic API finds no symbol libraries"
  fi
else
  echo "[--] KiCad 11 nightly not found (optional: schematic API backend; the plugin writes schematic files itself without it). Not auto-installed — nightlies live under $NIGHTLY_URL"
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
