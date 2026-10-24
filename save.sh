#!/usr/bin/env bash
# Encrypt everything under data/ into enc/ (AES-256, key in $DATA_KEY), commit enc/ and push.
# Plain data/ is git-ignored and never leaves the runner.
set -u
msg="${1:-data}"
if [ -z "${DATA_KEY:-}" ]; then
  echo "DATA_KEY not set; refusing to commit unencrypted data" >&2
  exit 0
fi
find data -type f | while read -r f; do
  out="enc/${f#data/}.enc"
  if [ ! -f "$out" ] || [ "$f" -nt "$out" ]; then
    mkdir -p "$(dirname "$out")"
    openssl enc -aes-256-cbc -md sha256 -pbkdf2 -iter 100000 -salt -pass env:DATA_KEY -in "$f" -out "$out"
  fi
done
git add enc
if git diff --cached --quiet; then
  exit 0
fi
git commit -q -m "$msg"
for i in 1 2 3 4 5; do
  if git push -q; then exit 0; fi
  git pull -q --rebase || true
  sleep $((i * 5))
done
echo "push failed" >&2
