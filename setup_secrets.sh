#!/usr/bin/env bash
# Run this yourself, once. It creates the data encryption key on your Mac and stores it,
# plus your Telegram bot token and chat id (read from ../fno/secrets.json), as encrypted
# GitHub Actions secrets of this repo. Nothing is printed.
set -eu
cd "$(dirname "$0")"
keyfile="../fno/recorder.key"
if [ ! -f "$keyfile" ]; then
  umask 077
  openssl rand -base64 48 > "$keyfile"
  echo "created $keyfile (keep it: without it the recordings cannot be read)"
fi
gh secret set DATA_KEY < "$keyfile"
python3 -c 'import json;print(json.load(open("../fno/secrets.json"))["TELEGRAM_BOT_TOKEN"],end="")' | gh secret set TELEGRAM_BOT_TOKEN
python3 -c 'import json;print(json.load(open("../fno/secrets.json"))["TELEGRAM_CHAT_ID"],end="")' | gh secret set TELEGRAM_CHAT_ID
echo "secrets set:"; gh secret list
