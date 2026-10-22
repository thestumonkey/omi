#!/usr/bin/env bash
# [fork-only] Deploy the CPU diarizer to the self-hosted cluster (namespace omi).
# The upstream chart always renders a GKE BackendConfig, which this cluster
# does not have, so render it, drop that object (and the helm test pod), apply.
# It also applies the model cache disk (local_model_cache_pvc.yaml) first.
# Extra arguments go to helm template, e.g. --set image.tag=cpu-abc.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
kubectl -n omi apply -f local_model_cache_pvc.yaml
helm template diarizer . -n omi -f local_omi_diarizer_values.yaml "$@" \
  | python3 -c '
import sys
docs = sys.stdin.read().split("\n---")
keep = [d for d in docs if "kind: BackendConfig" not in d and "helm.sh/hook" not in d]
sys.stdout.write("\n---".join(keep))
' \
  | kubectl -n omi apply -f -
