#!/usr/bin/env bash
# Nightly Postgres dump. Photos are not backed up: they are deleted a week after ingestion.
# Usage: scripts/backup.sh            (KEEP=14 BACKUP_DIR=./backups AGE_RECIPIENT=age1... optional)
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP="${KEEP:-14}"
DEST="${BACKUP_DIR:-./backups}"
mkdir -p "$DEST"
chmod 700 "$DEST"

out="$DEST/grocery-$(date +%F-%H%M).sql.gz"
trap 'rm -f "$out.tmp"' EXIT

docker compose exec -T db pg_dump -U grocery -d grocery --no-owner | gzip > "$out.tmp"
mv "$out.tmp" "$out"

if [ -n "${AGE_RECIPIENT:-}" ]; then
  age -r "$AGE_RECIPIENT" -o "$out.age" "$out"
  rm "$out"
  out="$out.age"
fi

# keep the newest $KEEP dumps
ls -1t "$DEST"/grocery-* 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm --
echo "backup written: $out"
