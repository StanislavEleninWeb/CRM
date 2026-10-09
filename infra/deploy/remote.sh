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

# The host keeps a checkout of infra/ at /opt/seweb-crm; only image references travel from here.
ssh -i ~/.ssh/deploy -o StrictHostKeyChecking=yes -o BatchMode=yes "$DEPLOY_USER@$DEPLOY_HOST" \
  "API_IMAGE='$API_IMAGE' FRONTEND_IMAGE='$FRONTEND_IMAGE' /opt/seweb-crm/infra/deploy/deploy.sh"
