#!/usr/bin/env bash
# Start the phone watcher (Termux). Settings go in phone.env, e.g. NTFY_TOPIC=...
cd "$(dirname "$0")"
set -a
[ -f phone.env ] && . ./phone.env
set +a
command -v termux-wake-lock >/dev/null && termux-wake-lock
exec python phone_watcher.py "$@"
