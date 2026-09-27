#!/bin/bash

set -e

ENV_FILE="./backend/.env.config"
CONFIG_NAME="omi-backend-config"
NAMESPACE="omi"
TEMP_ENV="/tmp/deduped_config.$$"

echo "📋 Creating k8s ConfigMap '$CONFIG_NAME' in namespace '$NAMESPACE' from $ENV_FILE..."

# Deduplicate keys, skip comments and blank lines
awk -F= '!seen[$1]++ && $1 !~ /^#/ && NF > 1 { print $0 }' "$ENV_FILE" > "$TEMP_ENV"

# Delete existing configmap if it exists
kubectl delete configmap "$CONFIG_NAME" -n "$NAMESPACE" --ignore-not-found

# Build --from-literal arguments
KUBECTL_ARGS=()
while IFS='=' read -r key value; do
  key=$(echo "$key" | xargs)
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  echo "  - $key"
  KUBECTL_ARGS+=("--from-literal=$key=$value")
done < "$TEMP_ENV"

# Create the configmap
kubectl create configmap "$CONFIG_NAME" -n "$NAMESPACE" "${KUBECTL_ARGS[@]}"

# Clean up
rm -f "$TEMP_ENV"

echo "✅ ConfigMap '$CONFIG_NAME' created successfully."
