#!/bin/bash
# run-native.sh — run the desktop Rust backend NATIVELY on macOS against the
# self-hosted cluster (Casdoor + MongoDB + Redis), for the desktop-app dev loop.
#
# The committed .env targets the in-cluster / docker-compose deployment, so it
# uses Docker service names (mongodb, redis, casdoor) that don't resolve from a
# laptop. This wrapper rewrites ONLY the hostnames to localhost and relies on
# `kubectl port-forward` for Mongo/Redis. Secrets are derived from .env — never
# duplicated here.
#
# Prereqs (run these in separate terminals, or they're started by run.sh):
#   kubectl -n root port-forward svc/mongodb     27017:27017
#   kubectl -n root port-forward svc/redis-master 6379:6379
#
# Casdoor is reached over Tailscale at its public URL (CASDOOR_ENDPOINT in .env),
# so it needs no port-forward.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $(pwd)" >&2
  exit 1
fi

# Pull MONGODB_URL from .env and swap the docker host for localhost (keeps pw/db/opts).
ENV_MONGO_URL="$(grep -E '^MONGODB_URL=' .env | head -1 | cut -d= -f2-)"
NATIVE_MONGO_URL="$(printf '%s' "$ENV_MONGO_URL" | sed -E 's#@[^:/]+:#@localhost:#')"

# Host/AI overrides that win over .env (dotenvy does not override existing env vars).
export MONGODB_URL="$NATIVE_MONGO_URL"
export REDIS_DB_HOST="127.0.0.1"
export CASDOOR_INTERNAL_URL=""          # skip in-cluster JWKS URL; use public CASDOOR_ENDPOINT
export FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-omi-selfhosted}"  # cosmetic post-shim; presence required
export USE_VERTEX_AI="false"            # no Google creds; LLM via self-hosted Ollama below
export SELF_HOSTED="true"               # own LLM, zero per-call cost → bypass trial paywall / chat quota
# Self-hosted Ollama (lemonade) — the Gemini proxy translates to its native /api/* .
# Model names must match `GET ${OLLAMA_URL}/api/tags` (NOT the OpenAI /v1 registry).
export OLLAMA_URL="${OLLAMA_URL:-https://gpu.spangled-kettle.ts.net}"
export OLLAMA_CHAT_MODEL="${OLLAMA_CHAT_MODEL:-Meta-Llama-3.1-8B-Instruct-GGUF-Q4_K_M}"
export OLLAMA_EMBED_MODEL="${OLLAMA_EMBED_MODEL:-Meta-Llama-3.1-8B-Instruct-GGUF-Q4_K_M}"
export PORT="${PORT:-10201}"
export RUST_LOG="${RUST_LOG:-omi_desktop_backend=info,tower_http=info}"

echo "── native backend config ──────────────────────────────"
echo "  MONGODB_URL      = ${MONGODB_URL%%@*}@<redacted-host>"
echo "  REDIS_DB_HOST    = $REDIS_DB_HOST"
echo "  CASDOOR_ENDPOINT = $(grep -E '^CASDOOR_ENDPOINT=' .env | cut -d= -f2-)"
echo "  USE_VERTEX_AI    = $USE_VERTEX_AI"
echo "  PORT             = $PORT"
echo "────────────────────────────────────────────────────────"

exec cargo run "$@"
