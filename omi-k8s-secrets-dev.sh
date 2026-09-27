#!/bin/bash

set -e

ENV_FILE="./.env.k8s"
SECRET_NAME="dev-omi-backend-secrets"
NAMESPACE="omi"
TEMP_ENV="/tmp/deduped_env.$$"

echo "🔐 Creating k8s Secret '$SECRET_NAME' in namespace '$NAMESPACE' from $ENV_FILE..."

# Deduplicate keys
awk -F= '!seen[$1]++ && $1 !~ /^#/ && NF > 1 { print $0 }' "$ENV_FILE" > "$TEMP_ENV"

# Delete existing secret if it exists
kubectl delete secret "$SECRET_NAME" -n "$NAMESPACE" --ignore-not-found

# Build --from-literal arguments
KUBECTL_ARGS=()
while IFS='=' read -r key value; do
  key=$(echo "$key" | xargs)
  # Don't use xargs on the value to preserve quotes
  value="${value#"${value%%[![:space:]]*}"}" # trim leading whitespace
  value="${value%"${value##*[![:space:]]}"}" # trim trailing whitespace
  echo "  - $key"
  KUBECTL_ARGS+=("--from-literal=$key=$value")
done < "$TEMP_ENV"

# Create the secret
kubectl create secret generic "$SECRET_NAME" -n "$NAMESPACE" "${KUBECTL_ARGS[@]}"

# Clean up
rm -f "$TEMP_ENV"

echo "✅ Secret '$SECRET_NAME' created successfully."