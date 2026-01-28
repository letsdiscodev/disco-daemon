#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.
set -o pipefail # The return value of a pipeline is the status of the last command to exit with a non-zero status.

# --- Parse Arguments ---
SSH_KEY=""
REMOTE_HOST=""

while [[ $# -gt 0 ]]; do
  case $1 in
    -i)
      SSH_KEY="$2"
      shift 2
      ;;
    *)
      if [ -z "$REMOTE_HOST" ]; then
        REMOTE_HOST="$1"
      fi
      shift
      ;;
  esac
done

if [ -z "$REMOTE_HOST" ]; then
  echo "Usage: $0 <remote-host> [-i <ssh-key>]"
  echo "Example: $0 10.123.123.123"
  echo "Example: $0 10.123.123.123 -i ~/.ssh/id_ed25519"
  exit 1
fi

# --- Configuration ---
REMOTE_USER="root"
LOCAL_PATH="." # Assumes you run this from the root of the disco repo
REMOTE_PATH="/root/disco-dev-src" # A dedicated directory on the server for our dev source
DEV_IMAGE_TAG="disco-daemon:dev-latest" # A unique tag for our development image

# Build SSH options
SSH_OPTS=""
if [ -n "$SSH_KEY" ]; then
  SSH_OPTS="-i $SSH_KEY"
fi

# --- Logging ---
echo "INFO: Starting Disco Daemon development deployment..."
echo "----------------------------------------------------"
echo "      Remote Host: $REMOTE_HOST"
echo "       Local Path: $LOCAL_PATH"
echo "      Remote Path: $REMOTE_PATH"
echo "    Dev Image Tag: $DEV_IMAGE_TAG"
if [ -n "$SSH_KEY" ]; then
  echo "          SSH Key: $SSH_KEY"
fi
echo "----------------------------------------------------"

# --- Phase 1: Sync Code ---
echo ""
echo "INFO: Phase 1: Synchronizing source code to remote server..."
rsync -avz --delete \
  --filter=':- .gitignore' \
  --filter=':- .dockerignore' \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  -e "ssh $SSH_OPTS" \
  "$LOCAL_PATH/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH}/"
echo "SUCCESS: Code synchronization complete."

# --- Phase 2: Remote Build ---
echo ""
echo "INFO: Phase 2: Building development image on remote server..."
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "DOCKER_BUILDKIT=1 docker build -t ${DEV_IMAGE_TAG} ${REMOTE_PATH}/"
echo "SUCCESS: Remote build complete. Image tagged as ${DEV_IMAGE_TAG}."

# --- Phase 3: Hot-Swap Service ---
echo ""
echo "INFO: Phase 3: Updating the 'disco' service to use the new image..."
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "docker service update --image ${DEV_IMAGE_TAG} --force disco"
echo "SUCCESS: 'disco' service update initiated."
echo ""
echo "INFO: Tailing logs to confirm restart..."
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "docker service logs --tail 20 disco"
