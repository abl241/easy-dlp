#!/usr/bin/env bash
# One-command launcher for easy-dlp.
#
#   ./run.sh                  - launch the app (sets up .venv on first run)
#   ./run.sh --update         - upgrade dependencies inside the venv
#   ./run.sh --reset          - delete .venv and rebuild from scratch
#   ./run.sh --doctor         - print diagnostics (Python, ffmpeg, deps)
#
# Requirements on the user's machine:
#   - Python 3.10+ with the `venv` and `tkinter` stdlib modules.
#     macOS users: Homebrew's `python@3.12` includes tkinter; the system
#     `python3` and Homebrew's `python@3.14` may NOT.
#   - ffmpeg on PATH (macOS: `brew install ffmpeg`).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
PY_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

# --- Pick a usable Python with tkinter ----------------------------------- #
pick_python() {
  # Preference order: python3.12, python3.13, python3.11, python3.10, python3
  for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import tkinter; import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

build_venv() {
  local py
  if ! py="$(pick_python)"; then
    cat >&2 <<EOF
ERROR: Could not find a Python interpreter with tkinter and >= 3.10.

On macOS:
  brew install python-tk@3.12   # provides tkinter for python@3.12

On Debian/Ubuntu:
  sudo apt install python3-tk

On Windows:
  Reinstall Python from python.org with the "tcl/tk and IDLE" component.
EOF
    return 1
  fi
  echo "Creating virtualenv with $py ..."
  "$py" -m venv "$VENV_DIR"
  "$PIP_BIN" install --quiet --upgrade pip
  "$PIP_BIN" install --quiet -r requirements.txt
  echo "Virtualenv ready at $VENV_DIR"
}

reset_venv() {
  echo "Removing $VENV_DIR ..."
  rm -rf "$VENV_DIR"
  build_venv
}

update_deps() {
  if [[ ! -x "$PY_BIN" ]]; then
    build_venv
  fi
  "$PIP_BIN" install --quiet --upgrade -r requirements.txt
  echo "Dependencies updated."
}

doctor() {
  echo "=== easy-dlp doctor ==="
  echo "Project root: $PROJECT_ROOT"
  echo
  echo "-- Host pythons with tkinter --"
  for candidate in python3.10 python3.11 python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ver="$("$candidate" --version 2>&1 || true)"
      if "$candidate" -c "import tkinter" >/dev/null 2>&1; then
        echo "  OK   $candidate ($ver, tkinter present)"
      else
        echo "  FAIL $candidate ($ver, tkinter MISSING)"
      fi
    fi
  done
  echo
  echo "-- ffmpeg --"
  if command -v ffmpeg >/dev/null 2>&1; then
    echo "  on PATH: $(command -v ffmpeg)"
  else
    echo "  not on PATH"
  fi
  echo
  echo "-- venv --"
  if [[ -x "$PY_BIN" ]]; then
    echo "  python:  $("$PY_BIN" --version)"
    echo "  yt-dlp:  $("$PY_BIN" -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>&1 || echo 'not installed')"
    echo "  yt-dlp-ejs: $("$PY_BIN" -c 'import yt_dlp_ejs; print("ok")' 2>&1 || echo 'not installed')"
    echo "  deno:      $("$PY_BIN" -c 'from deno import find_deno_bin; print(find_deno_bin())' 2>&1 || echo 'not installed')"
    echo "  customtkinter: $("$PY_BIN" -c 'import customtkinter; print(customtkinter.__version__)' 2>&1 || echo 'not installed')"
  else
    echo "  not built yet (run ./run.sh)"
  fi
}

cmd="${1:-launch}"
case "$cmd" in
  --update|update)   update_deps ;;
  --reset|reset)     reset_venv ;;
  --doctor|doctor)   doctor ;;
  --help|-h|help)
    sed -n '2,12p' "$0"
    ;;
  launch|"")
    if [[ ! -x "$PY_BIN" ]]; then
      build_venv
    fi
    exec "$PY_BIN" main.py
    ;;
  *)
    echo "Unknown command: $cmd"
    echo "Try: ./run.sh --help"
    exit 2
    ;;
esac
