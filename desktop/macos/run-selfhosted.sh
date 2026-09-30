#!/usr/bin/env bash
# [fork-only] Build and open the self-hosted Mac app ("omi-selfhosted") in one step.
#
#   desktop/macos/run-selfhosted.sh          # from the repo root, or ./run-selfhosted.sh here
#
# Uses the deployed cluster backends: no local backend, no tunnel. Server
# addresses come from desktop/macos/.env.app (gitignored; keys:
# OMI_PYTHON_API_URL, OMI_AUTH_API_URL, OMI_DESKTOP_API_URL,
# OMI_FIREBASE_REST_BASE_URL, FIREBASE_API_KEY). Extra arguments go to run.sh.
set -euo pipefail
cd "$(dirname "$0")"

if ! grep -q '^OMI_FIREBASE_REST_BASE_URL=' .env.app 2>/dev/null; then
    echo "ERROR: $(pwd)/.env.app is missing or has no OMI_FIREBASE_REST_BASE_URL." >&2
    echo "       Without it the app signs in against Omi's cloud, not Casdoor." >&2
    exit 1
fi

export OMI_APP_NAME="${OMI_APP_NAME:-omi-selfhosted}"
export OMI_SKIP_BACKEND=1
export OMI_SKIP_TUNNEL=1
exec ./run.sh "$@"
