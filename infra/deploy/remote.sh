#!/bin/sh
# Run the deployment on the target host over SSH. Used by the release workflow.
# With no host configured it does nothing and says so: there is no hosting target yet.
set -eu

if [ -z "${DEPLOY_HOST:-}" ]; then
  echo "::notice::No DEPLOY_HOST is configured for this environment. Nothing was deployed."
  exit 0
fi
: "${DEPLOY_USER:?}" "${DEPLOY_HOST_KEY:?the public key line of the host, so the connection is verified}" "${DEPLOY_SSH_KEY:?}"
: "${API_IMAGE:?}" "${FRONTEND_IMAGE:?}"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$DEPLOY_SSH_KEY" > ~/.ssh/deploy && chmod 600 ~/.ssh/deploy
printf '%s %s\n' "$DEPLOY_HOST" "$DEPLOY_HOST_KEY" > ~/.ssh/known_hosts
trap 'rm -f ~/.ssh/deploy' EXIT

SSH="ssh -i $HOME/.ssh/deploy -o StrictHostKeyChecking=yes -o BatchMode=yes $DEPLOY_USER@$DEPLOY_HOST"
REMOTE_DIR="${REMOTE_DIR:-/opt/seweb-crm}"

# The deployment files travel with the release, so the host needs no checkout of the repository.
tar -C "$(dirname "$0")/../.." -czf - infra/production infra/deploy infra/postgres infra/backup infra/checks \
  | $SSH "mkdir -p '$REMOTE_DIR' && tar -C '$REMOTE_DIR' -xzf -"

# The host may pull these two images for the duration of this deployment only.
if [ -n "${REGISTRY_TOKEN:-}" ]; then
  printf '%s' "$REGISTRY_TOKEN" | $SSH "docker login ghcr.io --username '${REGISTRY_USER:-github}' --password-stdin >/dev/null"
fi
status=0
$SSH "API_IMAGE='$API_IMAGE' FRONTEND_IMAGE='$FRONTEND_IMAGE' '$REMOTE_DIR/infra/deploy/deploy.sh'" || status=$?
$SSH "docker logout ghcr.io >/dev/null 2>&1 || true"
exit "$status"
