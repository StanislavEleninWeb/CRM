#!/bin/sh
# Encrypted database backup with a retention window.
#
#   BACKUP_PASSPHRASE_FILE=/etc/seweb-crm/backup.pass ./infra/backup/backup-encrypted.sh [output-directory]
#
# Produces crm-<UTC stamp>.dump.enc (AES-256, key derived with PBKDF2) and a .sha256 next to it,
# then deletes backups older than BACKUP_RETENTION_DAYS (default 30). The passphrase file must be
# kept somewhere other than the backups: a backup and its passphrase together are plaintext.
# The dump contains every tenant's data.
set -eu

OUT_DIR="${1:-backups}"
PASS_FILE="${BACKUP_PASSPHRASE_FILE:?BACKUP_PASSPHRASE_FILE is required}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
COMPOSE="${COMPOSE:-docker compose}"
[ -s "$PASS_FILE" ] || { echo "The passphrase file is missing or empty." >&2; exit 1; }

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PLAIN="$(mktemp "${TMPDIR:-/tmp}/crm-dump.XXXXXX")"
FILE="$OUT_DIR/crm-$STAMP.dump.enc"
trap 'rm -f "$PLAIN"' EXIT

$COMPOSE exec -T db sh -c 'pg_dump --format=custom --no-owner --dbname="$POSTGRES_DB" --username="$POSTGRES_USER"' > "$PLAIN"
test -s "$PLAIN" || { echo "Backup failed: empty dump" >&2; exit 1; }
# A truncated dump is found now, not during a restore.
$COMPOSE exec -T db pg_restore --list < "$PLAIN" > /dev/null

openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass "file:$PASS_FILE" -in "$PLAIN" -out "$FILE"
# Prove it can be read back with the same passphrase before the plaintext is removed.
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass "file:$PASS_FILE" -in "$FILE" | cmp -s - "$PLAIN" \
  || { echo "Backup failed: the encrypted file does not decrypt to the dump" >&2; rm -f "$FILE"; exit 1; }
( cd "$OUT_DIR" && { command -v sha256sum >/dev/null 2>&1 && sha256sum "crm-$STAMP.dump.enc" || shasum -a 256 "crm-$STAMP.dump.enc"; } > "crm-$STAMP.dump.enc.sha256" )

# Off-host copy. Without it a backup protects against mistakes, not against losing the server.
# The bucket's lifecycle rule removes old copies; credentials come from /etc/seweb-crm/backup-aws.env
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION) and may only write to that bucket.
if [ -n "${BACKUP_S3_URI:-}" ]; then
  docker run --rm --env-file "${BACKUP_AWS_ENV_FILE:-/etc/seweb-crm/backup-aws.env}" -v "$(cd "$OUT_DIR" && pwd):/backup:ro" \
    "${AWS_CLI_IMAGE:-amazon/aws-cli:2.31.10}" s3 cp --only-show-errors --sse AES256 --recursive --exclude '*' \
    --include "crm-$STAMP.dump.enc" --include "crm-$STAMP.dump.enc.sha256" /backup "$BACKUP_S3_URI/" \
    || { echo "Backup failed: the off-host copy could not be made" >&2; exit 1; }
fi

# Retention: this is the window during which deleted or erased records remain recoverable.
find "$OUT_DIR" -name 'crm-*.dump.enc*' -type f -mtime "+$RETENTION_DAYS" -exec rm -f {} +
echo "$FILE"
