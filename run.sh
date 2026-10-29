#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
if [[ ! -f .venv/.setup-complete ]]; then
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  touch .venv/.setup-complete
fi
if [[ ! -t 0 ]] && { [[ -z "${GITHUB_USERNAME:-}" ]] || [[ -z "${GITHUB_PASSWORD:-}" ]] || [[ -z "${GITHUB_TOTP_SECRET:-}" ]]; }; then
  echo "Set GITHUB_USERNAME, GITHUB_PASSWORD, and GITHUB_TOTP_SECRET for non-interactive runs." >&2
  exit 2
fi
if [[ -z "${GITHUB_USERNAME:-}" ]]; then
  read -rp "GitHub username: " GITHUB_USERNAME
  export GITHUB_USERNAME
fi
if [[ -z "${GITHUB_PASSWORD:-}" ]]; then
  read -rsp "GitHub password: " GITHUB_PASSWORD
  echo
  export GITHUB_PASSWORD
fi
if [[ -z "${GITHUB_TOTP_SECRET:-}" ]]; then
  read -rsp "GitHub TOTP setup key: " GITHUB_TOTP_SECRET
  echo
  export GITHUB_TOTP_SECRET
fi
.venv/bin/python -m portal_runner.runner "$@"
