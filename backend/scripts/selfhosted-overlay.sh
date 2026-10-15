#!/usr/bin/env bash
# [fork-only] Ship backend code changes onto the images that run now, without a
# full rebuild. Called by `make -C backend overlay REF=<commit>`.
#
# For each backend Deployment it reads the live image tag, finds the commit that
# image's code came from, and builds  FROM <live image>  plus the backend files
# that differ between that commit and REF. The new tag is <REF>-on-<full-build
# tag>, so `make status` still shows which code runs and which full build it
# sits on. Refuses changes an overlay cannot carry: dependency or Dockerfile
# changes, and deleted or renamed files. Use `make release` for those.
#
#   REF=<commit> [ONLY="pusher omi-backend"] [DRY_RUN=1] selfhosted-overlay.sh
set -euo pipefail

REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
REGISTRY=${REGISTRY:-192.168.1.42:32000}
NAMESPACE=${NAMESPACE:-omi}
REF=$(git -C "$REPO" rev-parse --short=10 "${REF:?REF=<commit> is required}^{commit}")

# deployment  container  image repository
TARGETS="omi-backend backend-listen backend-listen
pusher pusher backend-listen
desktop-backend-py desktop-backend desktop-backend-py"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
ROLLBACK=()

while read -r deploy container repo; do
  if [ -n "${ONLY:-}" ] && [[ " $ONLY " != *" $deploy "* ]]; then continue; fi

  live=$(kubectl -n "$NAMESPACE" get "deploy/$deploy" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name=='$container')].image}")
  live_tag=${live##*:}
  code=${live_tag%%-on-*}      # commit whose code the live image holds
  full=${live_tag##*-on-}      # the full build underneath any earlier overlays
  if ! git -C "$REPO" cat-file -e "$code^{commit}" 2>/dev/null; then
    echo "ERROR: $deploy runs $live; tag $live_tag is not a commit. Use make release." >&2
    exit 1
  fi

  changes=$(git -C "$REPO" diff --name-status "$code" "$REF" -- backend/ ':!backend/tests')
  if [ -z "$changes" ]; then
    echo "== $deploy: already runs the code of $REF ($live_tag), skipped"
    continue
  fi
  if grep -vE '^M|^A' <<< "$changes" >/dev/null; then
    echo "ERROR: $deploy: deleted or renamed files between $code and $REF:" >&2
    grep -vE '^M|^A' <<< "$changes" >&2
    exit 1
  fi
  files=$(cut -f2 <<< "$changes")
  if grep -E 'requirements[^/]*\.txt$|Dockerfile|pyproject\.toml$|/charts/' <<< "$files" >&2; then
    echo "ERROR: $deploy: the files above need a full build or a chart deploy: make release." >&2
    exit 1
  fi

  tag="$REF-on-$full"
  ctx="$WORK/$deploy"
  mkdir -p "$ctx"
  git -C "$REPO" archive "$REF" $files | tar -x -C "$ctx"
  {
    echo "FROM $REGISTRY/$repo:$live_tag"
    while read -r f; do echo "COPY $f /app/${f#backend/}"; done <<< "$files"
  } > "$ctx/Dockerfile.overlay"

  echo "== $deploy: $live_tag -> $tag ($(wc -l <<< "$files" | tr -d ' ') files)"
  sed 's/^/     /' <<< "$files"
  if [ -n "${DRY_RUN:-}" ]; then continue; fi

  docker buildx build -q --platform linux/amd64 --load \
    -f "$ctx/Dockerfile.overlay" -t "$REGISTRY/$repo:$tag" "$ctx" >/dev/null
  py=$(grep '\.py$' <<< "$files" | sed 's#^backend/#/app/#' | tr '\n' ' ' || true)
  if [ -n "$py" ]; then
    docker run --rm --platform linux/amd64 --entrypoint python "$REGISTRY/$repo:$tag" -m py_compile $py
  fi
  docker push -q "$REGISTRY/$repo:$tag"
  kubectl -n "$NAMESPACE" set image "deploy/$deploy" "$container=$REGISTRY/$repo:$tag"
  kubectl -n "$NAMESPACE" rollout status "deploy/$deploy" --timeout=10m
  ROLLBACK+=("kubectl -n $NAMESPACE set image deploy/$deploy $container=$live")
done <<< "$TARGETS"

if [ ${#ROLLBACK[@]} -gt 0 ]; then
  echo
  echo "Rollback:"
  printf '  %s\n' "${ROLLBACK[@]}"
fi
