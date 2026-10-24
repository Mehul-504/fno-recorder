#!/usr/bin/env bash
# Decrypt enc/ back into data/ on your own machine. Key file: ../fno/recorder.key (made by setup_secrets.sh).
set -eu
key="${1:-../fno/recorder.key}"
export DATA_KEY="$(cat "$key")"
find enc -type f -name '*.enc' | while read -r f; do
  out="data/${f#enc/}"; out="${out%.enc}"
  if [ ! -f "$out" ] || [ "$f" -nt "$out" ]; then
    mkdir -p "$(dirname "$out")"
    openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 100000 -pass env:DATA_KEY -in "$f" -out "$out"
  fi
done
echo "decrypted into data/"
