set -e

# Configuration
IMAGE_NAME=backend-listen
IMAGE_TAG=local
REMOTE_REGISTRY=192.168.1.42:32000
FULL_IMAGE_NAME=$REMOTE_REGISTRY/$IMAGE_NAME:$IMAGE_TAG
DOCKERFILE_PATH=backend/Dockerfile
CONTEXT_PATH=.
# Use parameter if provided, otherwise default to local values
VALUES_FILE=${1:-backend/charts/backend-listen/local_omi_backend_listen_values.yaml}
RELEASE_NAME=omi-backend
NAMESPACE=omi
NODE_AFFINITY=anubis

echo "🔨 Building Docker image for linux/amd64..."
docker build --platform linux/amd64 -f "$DOCKERFILE_PATH" -t $IMAGE_NAME:$IMAGE_TAG "$CONTEXT_PATH"

echo "🏷️ Tagging image for remote registry..."
docker tag $IMAGE_NAME:$IMAGE_TAG $FULL_IMAGE_NAME

echo "📤 Pushing image to $REMOTE_REGISTRY..."
docker push $FULL_IMAGE_NAME

echo "🚀 Deploying to Kubernetes with Helm..."
echo "📍 Targeting node: $NODE_AFFINITY"
helm upgrade --install $RELEASE_NAME backend/charts/backend-listen \
  --namespace $NAMESPACE \
  --create-namespace \
  -f $VALUES_FILE \
  --set image.repository=$REMOTE_REGISTRY/$IMAGE_NAME \
  --set image.tag=$IMAGE_TAG \
  --set nodeSelector."kubernetes\.io/hostname"=$NODE_AFFINITY

echo "✅ Done: Image pushed and deployed as $FULL_IMAGE_NAME"