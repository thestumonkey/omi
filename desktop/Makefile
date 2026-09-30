# [fork-only] Omi Desktop (macOS) against the self-hosted stack.
#
#   make -C desktop run-selfhosted          # build + open the app
#   make -C desktop run-selfhosted FULL=1   # force a full bundle rebuild
#   make -C desktop run-selfhosted APP_NAME=omi-test
#
# Uses the deployed cluster backends (Casdoor + MongoDB + local LLM): no local
# backend, no tunnel. Server addresses come from macos/.env.app (gitignored),
# which run.sh copies into the app bundle. It must set OMI_PYTHON_API_URL,
# OMI_AUTH_API_URL, OMI_DESKTOP_API_URL, OMI_FIREBASE_REST_BASE_URL and
# FIREBASE_API_KEY (any non-empty value). Without OMI_FIREBASE_REST_BASE_URL
# the app signs in against Omi's cloud instead of Casdoor.

APP_NAME ?= omi-selfhosted
MACOS    := $(CURDIR)/macos
ENV_APP  := $(MACOS)/.env.app

.DEFAULT_GOAL := help
.PHONY: help run-selfhosted

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  APP_NAME=$(APP_NAME)   FULL=1 forces a full rebuild"

run-selfhosted: ## Build and open the Mac app against the deployed self-hosted backends
	@grep -q '^OMI_FIREBASE_REST_BASE_URL=' "$(ENV_APP)" 2>/dev/null || { \
		echo "ERROR: $(ENV_APP) is missing or has no OMI_FIREBASE_REST_BASE_URL." >&2; exit 1; }
	cd "$(MACOS)" && OMI_APP_NAME="$(APP_NAME)" OMI_SKIP_BACKEND=1 OMI_SKIP_TUNNEL=1 \
		./run.sh $(if $(FULL),--full)
