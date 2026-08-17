#!/bin/bash
# Renders the dovecot config from the template and starts the server.
set -euo pipefail

VMAIL_UID="${VMAIL_UID:-1000}"
VMAIL_GID="${VMAIL_GID:-1000}"
VMAIL_USER="vmail"
MASTER="${DOVECOT_MASTER_PASSWORD:-}"

if [ -z "$MASTER" ]; then
  echo "FATAL: DOVECOT_MASTER_PASSWORD is not set." >&2
  exit 1
fi
if [ ${#MASTER} -lt 12 ]; then
  echo "FATAL: DOVECOT_MASTER_PASSWORD must be at least 12 characters." >&2
  exit 1
fi
if [[ ! "$MASTER" =~ ^[A-Za-z0-9._~=+/@:-]+$ ]]; then
  echo "FATAL: DOVECOT_MASTER_PASSWORD may only contain A-Z a-z 0-9 . _ ~ = + / @ : -" >&2
  echo "       (dovecot config values cannot hold spaces, quotes or '#')" >&2
  exit 1
fi

if ! getent group "$VMAIL_GID" >/dev/null; then
  groupadd -g "$VMAIL_GID" "$VMAIL_USER"
fi
if ! getent passwd "$VMAIL_UID" >/dev/null; then
  useradd -u "$VMAIL_UID" -g "$VMAIL_GID" -d /srv/mail -s /usr/sbin/nologin "$VMAIL_USER"
fi
VMAIL_USER="$(getent passwd "$VMAIL_UID" | cut -d: -f1)"

mkdir -p /srv/mail
chown "$VMAIL_UID:$VMAIL_GID" /srv/mail
chmod 0750 /srv/mail

config="$(cat /etc/dovecot/dovecot.conf.template)"
config="${config//@@MASTER_PASSWORD@@/$MASTER}"
config="${config//@@VMAIL_UID@@/$VMAIL_UID}"
config="${config//@@VMAIL_GID@@/$VMAIL_GID}"
config="${config//@@VMAIL_USER@@/$VMAIL_USER}"
printf '%s\n' "$config" > /etc/dovecot/dovecot.conf
chmod 0600 /etc/dovecot/dovecot.conf

echo "dovecot backup store ready (mail uid=$VMAIL_UID gid=$VMAIL_GID)"
exec "$@"
