# [fork-only] Build and deploy the self-hosted backend to the cluster (namespace omi).
#
#   make -C backend release            # build + push images, then deploy them
#   make -C backend build              # build + push images for the current commit
#   make -C backend deploy             # deploy images already pushed for TAG
#   make -C backend status             # what is running now
#   make -C backend diarizer           # build + deploy the CPU diarizer
#
# Images are tagged with the commit (TAG, default: current HEAD), so what runs
# can always be traced to git. It refuses uncommitted changes and commits that
# are not on main: deploying either one is how a later deploy from main
# silently removes your work. ALLOW_DIRTY=1 / ALLOW_OFF_MAIN=1 override.

REPO      := $(abspath $(CURDIR)/..)
REGISTRY  ?= 192.168.1.42:32000
NAMESPACE ?= omi
TAG       ?= $(shell git -C "$(REPO)" rev-parse --short=10 HEAD)
BASE      ?= python:3.11-slim
DOCKER    := DOCKER_BUILDKIT=1 docker build --platform linux/amd64 --build-arg PYTHON_BASE_IMAGE=$(BASE)

BACKEND_IMAGE := $(REGISTRY)/backend-listen
DESKTOP_IMAGE := $(REGISTRY)/desktop-backend-py
DIARIZER_TAG  ?= cpu-$(TAG)

.DEFAULT_GOAL := help
.PHONY: help check build deploy release status diarizer

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  TAG=$(TAG)  REGISTRY=$(REGISTRY)  NAMESPACE=$(NAMESPACE)"

check: ## Refuse uncommitted work or commits that are not on main
	@if [ -z "$(ALLOW_DIRTY)" ] && [ -n "$$(git -C "$(REPO)" status --porcelain --untracked-files=no -- backend)" ]; then \
		echo "ERROR: backend/ has uncommitted changes; commit them first (or ALLOW_DIRTY=1)." >&2; exit 1; fi
	@if [ -z "$(ALLOW_OFF_MAIN)" ] && ! git -C "$(REPO)" merge-base --is-ancestor "$(TAG)" main; then \
		echo "ERROR: $(TAG) is not on main; merge it first (or ALLOW_OFF_MAIN=1)." >&2; exit 1; fi

build: check ## Build and push the backend and desktop-backend images for TAG
	cd "$(REPO)" && $(DOCKER) -f backend/Dockerfile -t $(BACKEND_IMAGE):$(TAG) .
	docker push $(BACKEND_IMAGE):$(TAG)
	cd "$(REPO)" && $(DOCKER) -f backend/Dockerfile.desktop_backend -t $(DESKTOP_IMAGE):$(TAG) .
	docker push $(DESKTOP_IMAGE):$(TAG)

deploy: check ## Deploy TAG: MinIO chart, backend chart, pusher and desktop backend
	helm upgrade omi charts/minio -n $(NAMESPACE) -f charts/minio/local_omi_minio_values.yaml
	helm upgrade omi-backend charts/backend-listen -n $(NAMESPACE) \
		-f charts/backend-listen/local_omi_backend_listen_values.yaml \
		--set image.repository=$(BACKEND_IMAGE) --set image.tag=$(TAG)
	kubectl -n $(NAMESPACE) set image deploy/pusher '*=$(BACKEND_IMAGE):$(TAG)'
	kubectl -n $(NAMESPACE) set image deploy/desktop-backend-py '*=$(DESKTOP_IMAGE):$(TAG)'
	kubectl -n $(NAMESPACE) rollout status deploy/omi-backend --timeout=10m
	kubectl -n $(NAMESPACE) rollout status deploy/pusher --timeout=10m
	kubectl -n $(NAMESPACE) rollout status deploy/desktop-backend-py --timeout=10m

release: build deploy ## Build, push and deploy the current commit

status: ## Show the image and state of each backend
	@kubectl -n $(NAMESPACE) get deploy omi-backend pusher desktop-backend-py diarizer \
		-o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image'

diarizer: ## Build and deploy the CPU diarizer (charts/diarizer/deploy-local.sh)
	cd "$(REPO)" && DOCKER_BUILDKIT=1 docker build --platform linux/amd64 \
		-f backend/diarizer/Dockerfile.cpu -t $(REGISTRY)/diarizer:$(DIARIZER_TAG) .
	docker push $(REGISTRY)/diarizer:$(DIARIZER_TAG)
	charts/diarizer/deploy-local.sh --set image.tag=$(DIARIZER_TAG)
