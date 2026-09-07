#!/usr/bin/env bash
#
# System-level dependencies for the Neucleus pharmaceutical multi-agent system.
#
# Scope is deliberately narrow: this brings a clean macOS machine to the point
# where `uv run python scripts/bootstrap.py` can run. Anything project-specific
# (uv sync, .env, migrations, seed data) belongs to bootstrap.py, not here, so
# that the two layers can be re-run independently when one of them breaks.
#
# Usage:
#   ./scripts/install-dependencies.sh            install what is missing
#   ./scripts/install-dependencies.sh --check    report only, change nothing
#
set -euo pipefail

# The whole point of this script is other machines, so nothing may assume where
# the checkout lives.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

STEP="startup"
# Capture $? first: any command in the handler (echo included) overwrites it.
on_error() {
  local rc=$1 line=$2
  echo ""
  echo "FAILED during: ${STEP} (line ${line}, exit ${rc})"
  echo "Nothing was rolled back; fix the cause and re-run -- this script is idempotent."
  exit "$rc"
}
trap 'on_error $? $LINENO' ERR

CHECK_MODE=0
for arg in "$@"; do
  case "$arg" in
    --check|--dry-run) CHECK_MODE=1 ;;
    -h|--help) sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

# Parallel arrays rather than an associative array: /bin/bash on macOS is 3.2.
RES_NAME=(); RES_VER=(); RES_STATUS=(); RES_REQ=()
record() { RES_NAME+=("$1"); RES_VER+=("$2"); RES_STATUS+=("$3"); RES_REQ+=("$4"); }

section() { STEP="$1"; echo ""; echo "== $1"; }
ok()   { echo "   OK       $*"; }
info() { echo "   ..       $*"; }
warn() { echo "   WARN     $*"; }
todo() { echo "   ACTION   $*"; }

# Every mutating call funnels through here so that --check cannot change anything.
would() {
  if [ "$CHECK_MODE" -eq 1 ]; then echo "   WOULD    $*"; return 0; fi
  echo "   RUN      $*"
  "$@"
}

# Give the exact ~/.zshrc line, but only when the directory is genuinely absent
# from PATH -- unconditional advice trains people to ignore it.
path_hint() {
  case ":${PATH}:" in *":$1:"*) return 0 ;; esac
  todo "$2 is not on your PATH. Add this to ~/.zshrc and open a new shell:"
  echo "              $3"
}

# --------------------------------------------------------------------------
section "Platform"
# Postgres via Homebrew and brew services (LaunchAgents) are macOS-only; on
# another platform this would half-work and leave a confusing mess, so refuse.
if [ "$(uname -s)" != "Darwin" ]; then
  echo "   This project targets macOS. Detected: $(uname -s)."
  echo "   Install PostgreSQL 17, Python 3.12, uv and Node with your platform's package manager instead."
  exit 1
fi
ok "macOS $(sw_vers -productVersion) ($(uname -m))"
echo ""
if [ "$CHECK_MODE" -eq 1 ]; then
  echo "   CHECK MODE: reporting only, nothing will be installed or started."
else
  echo "   Privileges: the Homebrew installer asks for your admin password (sudo)"
  echo "   if Homebrew is absent, and the optional LibreOffice cask may too."
  echo "   Nothing else here needs sudo, and LibreOffice is asked for first."
fi

# --------------------------------------------------------------------------
section "Homebrew"
BREW=""
if command -v brew >/dev/null 2>&1; then
  BREW="$(command -v brew)"
# A fresh install is only on PATH after shellenv runs, so probe both prefixes.
elif [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew
elif [ -x /usr/local/bin/brew ]; then BREW=/usr/local/bin/brew
fi

if [ -n "$BREW" ]; then
  ok "already installed ($("$BREW" --version | head -1)) at ${BREW}"
  record "Homebrew" "$("$BREW" --version | head -1 | awk '{print $2}')" "OK" "required"
elif [ "$CHECK_MODE" -eq 1 ]; then
  info "WOULD install Homebrew from https://brew.sh (interactive, prompts for sudo)"
  record "Homebrew" "-" "MISSING" "required"
else
  todo "Installing Homebrew. This WILL prompt for your admin password."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew; else BREW=/usr/local/bin/brew; fi
  record "Homebrew" "$("$BREW" --version | head -1 | awk '{print $2}')" "OK" "required"
fi

BREW_PREFIX=""
if [ -n "$BREW" ]; then
  # Make brew usable for the rest of this run even if the login shell is not set up.
  eval "$("$BREW" shellenv)"
  BREW_PREFIX="$("$BREW" --prefix)"
  path_hint "${BREW_PREFIX}/bin" "Homebrew" "eval \"\$(${BREW} shellenv)\""
else
  warn "Homebrew unavailable; the dependencies below cannot be installed."
fi

# Install a formula only when genuinely absent -- this is what makes re-runs cheap.
brew_ensure() {
  [ -n "$BREW" ] || return 1
  "$BREW" list --versions "$1" >/dev/null 2>&1 && return 0
  would "$BREW" install "$1"
}

# Shared shape for the plain CLI dependencies: check, install if missing, record.
# $1 table label, $2 command, $3 formula, $4 field of `cmd --version` to report.
ensure_cli() {
  local label="$1" cmd="$2" formula="$3" field="$4" ver pre="already "
  if ! command -v "$cmd" >/dev/null 2>&1; then
    pre=""
    brew_ensure "$formula" || true
    hash -r 2>/dev/null || true
  fi
  if command -v "$cmd" >/dev/null 2>&1; then
    ver="$("$cmd" --version 2>&1 | head -1 | awk -v f="$field" '{print $f}')"
    ok "${pre}installed (${ver}) at $(command -v "$cmd")"
    record "$label" "$ver" "OK" "required"
  else
    record "$label" "-" "MISSING" "required"
  fi
}

# --------------------------------------------------------------------------
section "Python 3.12"
# The project pins 3.12; a newer python3 already on PATH is not a substitute.
ensure_cli "Python 3.12" python3.12 python@3.12 2

# --------------------------------------------------------------------------
section "uv"
# Via Homebrew rather than Astral's curl installer so upgrades stay in one
# package manager. The curl script remains the documented fallback.
ensure_cli "uv" uv uv 2
if ! command -v uv >/dev/null 2>&1 && [ "$CHECK_MODE" -eq 0 ]; then
  todo "brew did not provide uv. Fallback: curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "              then: export PATH=\"\$HOME/.local/bin:\$PATH\" in ~/.zshrc"
fi

# --------------------------------------------------------------------------
section "Node.js"
# OpenClaw bundles its own Node runtime; this one is for the tooling around it.
# Any working node counts, so an existing nvm/volta install is left alone.
ensure_cli "Node.js" node node 1

# --------------------------------------------------------------------------
section "PostgreSQL 17"
PG_OK=0
PG_PRE="already "
if [ -z "$BREW" ] || ! "$BREW" list --versions postgresql@17 >/dev/null 2>&1; then PG_PRE=""; fi
if brew_ensure postgresql@17 && [ -n "$BREW" ] && "$BREW" list --versions postgresql@17 >/dev/null 2>&1; then
  PGVER="$("$BREW" list --versions postgresql@17 | awk '{print $2}')"
  ok "${PG_PRE}installed (postgresql@17 ${PGVER})"
  PG_OK=1
  record "PostgreSQL 17" "$PGVER" "OK" "required"
else
  record "PostgreSQL 17" "-" "MISSING" "required"
fi

# postgresql@17 is keg-only, so psql and pg_dump are never linked into the prefix.
if [ -d "${BREW_PREFIX}/opt/postgresql@17/bin" ]; then
  path_hint "${BREW_PREFIX}/opt/postgresql@17/bin" "psql (keg-only)" \
    "export PATH=\"${BREW_PREFIX}/opt/postgresql@17/bin:\$PATH\""
fi

# bootstrap.py connects to the database, so a stopped service is a hard failure.
pg_started() {
  "$BREW" services list 2>/dev/null | awk '$1=="postgresql@17"{print $2}' | grep -q '^started$'
}
if [ "$PG_OK" -eq 0 ]; then
  record "postgres service" "-" "MISSING" "required"
elif pg_started; then
  ok "service already running (brew services)"
  record "postgres service" "started" "OK" "required"
else
  would "$BREW" services start postgresql@17
  if [ "$CHECK_MODE" -eq 1 ]; then
    record "postgres service" "stopped" "MISSING" "required"
  else
    sleep 2  # launchd reports 'started' asynchronously
    if pg_started; then
      record "postgres service" "started" "OK" "required"
    else
      record "postgres service" "stopped" "MISSING" "required"
      warn "service did not reach 'started'; check: brew services info postgresql@17"
    fi
  fi
fi

# --------------------------------------------------------------------------
section "OpenClaw CLI"
OPENCLAW=""
if command -v openclaw >/dev/null 2>&1; then
  OPENCLAW="$(command -v openclaw)"
elif [ -x "${HOME}/.openclaw/bin/openclaw" ]; then
  OPENCLAW="${HOME}/.openclaw/bin/openclaw"   # installed but not exported
fi

if [ -n "$OPENCLAW" ]; then
  OCVER="$("$OPENCLAW" --version 2>/dev/null | head -1 | awk '{print $2}')"
  [ -n "$OCVER" ] || OCVER="unknown"
  ok "already installed (OpenClaw ${OCVER}) at ${OPENCLAW}"
  record "OpenClaw" "$OCVER" "OK" "required"
  path_hint "${HOME}/.openclaw/bin" "openclaw" "export PATH=\"\$HOME/.openclaw/bin:\$PATH\""
else
  # Deliberately not automated: no non-interactive install command for OpenClaw
  # could be verified from the project or the installed copy. Inventing a
  # curl|bash URL that 404s -- or that a stranger registers later -- is a far
  # worse failure than an honest manual step.
  todo "OpenClaw is not installed and this script will NOT guess how to install it."
  echo "              Follow the official instructions: https://docs.openclaw.ai"
  echo "              Then re-run this script to verify. Expected at: ~/.openclaw/bin/openclaw"
  record "OpenClaw" "-" "MANUAL" "required"
fi

# --------------------------------------------------------------------------
section "LibreOffice (optional)"
# Used only for PDF export; the system is fully usable without it.
if [ -d "/Applications/LibreOffice.app" ] || { [ -n "$BREW" ] && "$BREW" list --cask libreoffice >/dev/null 2>&1; }; then
  ok "already installed"
  record "LibreOffice" "present" "OK" "optional"
elif [ "$CHECK_MODE" -eq 1 ]; then
  info "not installed. WOULD offer: brew install --cask libreoffice (optional, declinable)"
  record "LibreOffice" "-" "SKIPPED" "optional"
elif [ -z "$BREW" ] || [ ! -t 0 ]; then
  # No Homebrew, or no terminal to ask on (CI): skip rather than block.
  info "skipping (optional): needs Homebrew and an interactive terminal."
  record "LibreOffice" "-" "SKIPPED" "optional"
else
  echo "   LibreOffice enables PDF export only. The cask is large and may prompt for sudo."
  printf "   Install it now? [y/N] "
  read -r reply || reply="n"
  case "$reply" in
    [Yy]*) "$BREW" install --cask libreoffice; record "LibreOffice" "present" "OK" "optional" ;;
    *)     info "declined; skipping (optional)."; record "LibreOffice" "-" "SKIPPED" "optional" ;;
  esac
fi

# --------------------------------------------------------------------------
STEP="final verification"
echo ""
echo "== Summary"
printf "   %-18s %-14s %-8s %s\n" "DEPENDENCY" "VERSION" "STATUS" "REQUIRED"
printf "   %-18s %-14s %-8s %s\n" "------------------" "--------------" "--------" "--------"
FAILED=0
i=0
while [ "$i" -lt "${#RES_NAME[@]}" ]; do
  printf "   %-18s %-14s %-8s %s\n" "${RES_NAME[$i]}" "${RES_VER[$i]}" "${RES_STATUS[$i]}" "${RES_REQ[$i]}"
  if [ "${RES_REQ[$i]}" = "required" ] && [ "${RES_STATUS[$i]}" != "OK" ]; then
    FAILED=$((FAILED + 1))
  fi
  i=$((i + 1))
done
echo ""

if [ "$FAILED" -gt 0 ]; then
  echo "   ${FAILED} required dependency(ies) not satisfied -- see the sections above."
  [ "$CHECK_MODE" -eq 1 ] && echo "   Re-run without --check to install what can be automated."
  exit 1
fi

if [ "$CHECK_MODE" -eq 1 ]; then
  echo "   All required system dependencies present. Nothing was changed."
else
  echo "   All required system dependencies present."
fi
echo ""
echo "   Next step (the project layer, not this script's job):"
echo "       cd \"$PROJECT_ROOT\" && uv run python scripts/bootstrap.py"
exit 0
