#!/usr/bin/env bash
# Drains the queue and hands each item to your Claude agent. Run every minute
# (cron / systemd timer). Uses flock so it can't race the server's append, and
# never runs two copies at once.
#
# How "hand to the agent" works depends on your setup. With a `claude --channels`
# Telegram bot, the simplest robust pattern is: this script moves new lines into a
# spot the bot watches, then nudges the bot to process them inside its own session
# (so it keeps conversation context). Adapt the NUDGE step to your agent.
set -euo pipefail

QUEUE="${INBOX_QUEUE:-/var/lib/secretary/inbox.jsonl}"
DONE="${QUEUE%.jsonl}.done.jsonl"
LOCK="${QUEUE}.lock"
RUNLOCK="${QUEUE}.poll.lock"

exec 9>"$RUNLOCK"; flock -n 9 || exit 0   # one poller at a time

[ -s "$QUEUE" ] || exit 0

# atomically take the current queue contents
TMP="$(mktemp)"
{
  flock -x 200
  cat "$QUEUE" >> "$TMP"
  : > "$QUEUE"
} 200>"$LOCK"

while IFS= read -r line; do
  [ -n "$line" ] || continue
  # === hand off to your agent here ===
  # e.g. append to the bot's watched inbox and let the bot summarize+report:
  #   echo "$line" >> /path/the/bot/watches/inbox.jsonl
  # then NUDGE the bot to process (tmux send-keys, an API call, etc.)
  echo "$line" >> "$DONE"
done < "$TMP"

rm -f "$TMP"
