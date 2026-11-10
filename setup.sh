#!/usr/bin/env bash
# setup.sh - create/refresh the project virtualenv and install dependencies.
#
#   ./setup.sh                 create .venv, install requirements, check Chrome
#   source ./setup.sh          same, then leave the venv active in your shell
#   ./setup.sh --recreate      delete and rebuild .venv
#   ./setup.sh --help          all options
#
# Uses `uv` when available (fast), otherwise python -m venv + pip.

# Script location; zsh sets $0 to the sourced file, bash uses BASH_SOURCE.
_FMV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

_fmv_setup() (
  set -euo pipefail

  local script_dir venv_dir req_file python_bin recreate=0 install_browser=1 use_uv=1
  script_dir="$_FMV_DIR"
  venv_dir="${VENV_DIR:-$script_dir/.venv}"
  req_file="$script_dir/requirements.txt"
  python_bin="${PYTHON:-python3}"

  readonly MIN_PY_MAJOR=3 MIN_PY_MINOR=9

  if [[ -t 1 ]]; then
    local c_info=$'\e[1;34m' c_ok=$'\e[1;32m' c_warn=$'\e[1;33m' c_err=$'\e[1;31m' c_off=$'\e[0m'
  else
    local c_info='' c_ok='' c_warn='' c_err='' c_off=''
  fi
  info() { printf '%s==>%s %s\n' "$c_info" "$c_off" "$*"; }
  ok()   { printf '%s ok%s %s\n' "$c_ok" "$c_off" "$*"; }
  warn() { printf '%swarn%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
  die()  { printf '%serror%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

  usage() {
    cat <<EOF
Usage: ./setup.sh [options]     (or: source ./setup.sh [options])

Options:
  -p, --python PATH   Python interpreter to build the venv from (default: python3)
  -r, --recreate      Delete the existing venv and rebuild it
      --no-browser    Skip the Google Chrome check/install for Playwright
      --no-uv         Use python -m venv + pip even if uv is installed
  -h, --help          Show this help

Environment:
  VENV_DIR            Venv location (default: ./.venv)
  PYTHON              Same as --python
EOF
  }

  while (($#)); do
    case "$1" in
      -p|--python)  [[ $# -ge 2 ]] || die "$1 needs a value"; python_bin="$2"; shift 2 ;;
      -r|--recreate) recreate=1; shift ;;
      --no-browser) install_browser=0; shift ;;
      --no-uv)      use_uv=0; shift ;;
      -h|--help)    usage; exit 0 ;;
      *)            usage >&2; die "unknown option: $1" ;;
    esac
  done

  # --- preflight -----------------------------------------------------------
  command -v "$python_bin" >/dev/null 2>&1 || die "python not found: $python_bin (use --python)"
  [[ -f "$req_file" ]] || die "missing $req_file"

  "$python_bin" - "$MIN_PY_MAJOR" "$MIN_PY_MINOR" <<'PY' || die "Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} required"
import sys
sys.exit(0 if sys.version_info[:2] >= (int(sys.argv[1]), int(sys.argv[2])) else 1)
PY
  ok "$("$python_bin" --version 2>&1) at $(command -v "$python_bin")"

  if ((use_uv)) && ! command -v uv >/dev/null 2>&1; then
    use_uv=0
  fi

  # --- venv ----------------------------------------------------------------
  if ((recreate)) && [[ -d "$venv_dir" ]]; then
    info "Removing $venv_dir"
    rm -rf -- "$venv_dir"
  fi

  if [[ -x "$venv_dir/bin/python" ]]; then
    ok "Reusing venv $venv_dir"
  else
    info "Creating venv $venv_dir"
    if ((use_uv)); then
      uv venv --quiet --python "$python_bin" "$venv_dir"
    else
      "$python_bin" -m venv --upgrade-deps "$venv_dir"
    fi
  fi

  local vpy="$venv_dir/bin/python"

  # --- dependencies --------------------------------------------------------
  info "Installing requirements"
  if ((use_uv)); then
    uv pip install --quiet --python "$vpy" -r "$req_file"
  else
    "$vpy" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$vpy" -m pip install --quiet --disable-pip-version-check -r "$req_file"
  fi
  ok "Installed: $("$vpy" -c 'from importlib.metadata import version as v; print(*(f"{p}=={v(p)}" for p in ("playwright", "requests")))')"

  # --- browser -------------------------------------------------------------
  # fmv_grab.py launches Playwright with channel="chrome" (real Google Chrome),
  # so a system Chrome is needed rather than Playwright's bundled Chromium.
  if ((install_browser)); then
    if [[ -d "/Applications/Google Chrome.app" ]] \
       || command -v google-chrome >/dev/null 2>&1 \
       || command -v google-chrome-stable >/dev/null 2>&1; then
      ok "Google Chrome found"
    else
      info "Google Chrome not found; installing via Playwright (may prompt for sudo)"
      "$vpy" -m playwright install chrome || warn "Chrome install failed; install Google Chrome manually"
    fi
  fi

  ok "Setup complete"
)

# --- entrypoint: works when executed or sourced (bash or zsh) ---------------
_fmv_is_sourced() {
  if [[ -n "${ZSH_EVAL_CONTEXT:-}" ]]; then
    [[ "$ZSH_EVAL_CONTEXT" == *:file* ]]
  else
    [[ "${BASH_SOURCE[0]}" != "$0" ]]
  fi
}

if _fmv_is_sourced; then
  if _fmv_setup "$@"; then
    case " $* " in *" -h "*|*" --help "*) ;; *)
      # shellcheck disable=SC1091
      source "${VENV_DIR:-$_FMV_DIR/.venv}/bin/activate"
      echo "venv active: $VIRTUAL_ENV  (run 'deactivate' to leave)"
    esac
  fi
  unset -f _fmv_setup _fmv_is_sourced
  unset _FMV_DIR
else
  _fmv_setup "$@" || exit $?
  echo
  echo "Activate the venv in your shell with:"
  echo "  source .venv/bin/activate"
fi
