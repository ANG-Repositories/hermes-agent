#!/usr/bin/env bash
# Download a pinned CI asset. Never consume bytes before verifying the caller's hash.
set -euo pipefail

url=${1:?URL required}
api_url=${2:?API asset URL required}
sha=${3:?SHA-256 required}
output=${4:?Output path required}
attempts=6
for ((n = 1; ; n++)); do
  curl -fsSL --connect-timeout 20 --max-time 120 -o "$output" "$url" && break
  if ((n >= attempts)); then
    printf 'download failed after %s attempts: %s\n' "$attempts" "$url" >&2
    exit 1
  fi
  printf 'download attempt %s/%s failed; retrying in %ss\n' "$n" "$attempts" "$((n * 5))" >&2
  sleep "$((n * 5))"
done
printf '%s  %s\n' "$sha" "$output" | sha256sum -c -
