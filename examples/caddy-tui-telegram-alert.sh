#!/bin/sh
# Runs caddy-tui --check and sends a Telegram message when something is wrong.
# Configure /etc/caddy-tui/alert.env (mode 600) with:
#   TELEGRAM_BOT_TOKEN=123456:ABC...
#   TELEGRAM_CHAT_ID=123456789
# Then run it from cron, e.g.:   0 * * * *  root  /usr/local/bin/caddy-tui-telegram-alert.sh
# (it needs root if the Caddyfile lives in a root-owned folder).
set -u
. /etc/caddy-tui/alert.env

out=$(caddy-tui --check --quiet --lang en 2>&1)
rc=$?
[ "$rc" -eq 0 ] && exit 0            # all good: stay silent

curl -fsS -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=$(hostname): Caddy check (exit $rc)
$out" >/dev/null
exit "$rc"
