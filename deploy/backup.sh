#!/usr/bin/env bash
# Back up the bot's data: the SQLite database (copied safely while the bot
# runs) and the uploaded knowledge-base files, from the running backend.
#
#   deploy/backup.sh [folder]     # default: ../rocketchat_bot_backups
#
# Writes <folder>/bot-data-<time>.tar.gz holding bot.db and kb/. Vectors in
# Qdrant are not included: they are made again from the database.
set -euo pipefail

cd "$(dirname "$0")/.."
DEST=${1:-../rocketchat_bot_backups}
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
CONTAINER=$(docker compose ps -q backend)
[[ -n "$CONTAINER" ]] || { echo "the backend is not running" >&2; exit 1; }

# sqlite3's online backup: a consistent copy even while the bot writes.
docker exec "$CONTAINER" python -c "
import sqlite3
src = sqlite3.connect('/app/data/bot.db')
dst = sqlite3.connect('/app/data/backup-snapshot.db')
src.backup(dst)
dst.close()
print('database at', src.execute('select version_num from alembic_version').fetchone()[0])
"
docker exec "$CONTAINER" sh -c '
  cd /app/data && mkdir -p kb &&
  tar -czf - --transform "s|^backup-snapshot.db\$|bot.db|" backup-snapshot.db kb
' > "$DEST/bot-data-$STAMP.tar.gz"
docker exec "$CONTAINER" rm -f /app/data/backup-snapshot.db

echo "Saved $DEST/bot-data-$STAMP.tar.gz ($(du -h "$DEST/bot-data-$STAMP.tar.gz" | cut -f1))"
