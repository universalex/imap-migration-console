#!/usr/bin/env bash
# Spins up two throwaway IMAP servers next to the stack so the console can be
# tried out without touching real mail:
#
#   imap-test-source   pre-filled with a few messages   -> use as source host
#   imap-test-target   empty                            -> use as restore target
#
# Both accept any login with the DOVECOT_MASTER_PASSWORD from .env, no
# encryption, port 143. Run ./test/smoke-test.sh clean to remove them again.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env found - copy .env.example first" >&2; exit 1; }
# shellcheck disable=SC1091
set -a; . ./.env; set +a

NETWORK="$(docker compose ps --format '{{.Name}}' app >/dev/null 2>&1 && \
  docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' \
  "$(docker compose ps -q app)")"
IMAGE="imap-backup/dovecot:latest"
# address:inbox:sent:archive:archive-2024
USERS=(
  "anna@example.com:34:12:3:18"
  "ben@example.com:21:7:2:9"
  "info@example.com:57:23:4:31"
  "team@example.com:12:4:1:6"
)

if [ "${1:-}" = "clean" ]; then
  docker rm -f imap-test-source imap-test-target >/dev/null 2>&1 || true
  echo "removed test servers"
  exit 0
fi

for name in imap-test-source imap-test-target; do
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network "$NETWORK" \
    -e DOVECOT_MASTER_PASSWORD="$DOVECOT_MASTER_PASSWORD" \
    -e VMAIL_UID="${VMAIL_UID:-1000}" -e VMAIL_GID="${VMAIL_GID:-1000}" \
    "$IMAGE" >/dev/null
done

seed() {
  local user="$1" folder="$2" count="$3" subject="$4"
  docker exec imap-test-source bash -c '
    user="$1"; folder="$2"; count="$3"; subject="$4"
    base="/srv/mail/$user/Maildir"
    [ "$folder" = "INBOX" ] || base="$base/.$folder"
    mkdir -p "$base/cur" "$base/new" "$base/tmp"
    for i in $(seq 1 "$count"); do
      f="$base/new/$(date +%s).seed${folder}${i}.testsource"
      {
        printf "Return-Path: <sender@test.local>\n"
        printf "Message-ID: <%s-%s-%s@test.local>\n" "$folder" "$i" "$user"
        printf "Date: Mon, 6 Jan 2025 1%s:00:00 +0100\n" "$((i % 10))"
        printf "From: Test Sender <sender@test.local>\n"
        printf "To: %s\n" "$user"
        printf "Subject: %s %s\n" "$subject" "$i"
        printf "MIME-Version: 1.0\n"
        printf "Content-Type: text/plain; charset=utf-8\n\n"
        printf "Message %s in folder %s for %s.\n" "$i" "$folder" "$user"
        head -c 2000 /dev/urandom | base64
      } > "$f"
    done
    chown -R "${VMAIL_UID:-1000}:${VMAIL_GID:-1000}" "/srv/mail/$user"
  ' _ "$user" "$folder" "$count" "$subject"
}

credentials=""
for spec in "${USERS[@]}"; do
  IFS=: read -r user inbox sent archive archive24 <<<"$spec"
  seed "$user" INBOX "$inbox" "Inbox message"
  seed "$user" Sent "$sent" "Sent message"
  seed "$user" Archive "$archive" "Archived"
  seed "$user" Archive.2024 "$archive24" "Archived 2024"
  credentials+="    $user;$DOVECOT_MASTER_PASSWORD"$'\n'
done

cat <<EOF

Test servers are up.

  Source host : imap-test-source   (encryption: None, port 143)
  Target host : imap-test-target   (encryption: None, port 143)

  Paste into the "Accounts" box of the New backup tab:

$credentials
  Every account holds mail in INBOX, Sent, Archive and Archive/2024.
  Remove the servers again with: ./test/smoke-test.sh clean
EOF
