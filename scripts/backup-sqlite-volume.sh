#!/usr/bin/env bash
set -euo pipefail
umask 077

if (( $# < 1 || $# > 2 )); then
  echo "Usage: sudo $0 /path/to/backup-directory [docker-volume]" >&2
  exit 2
fi

destination=$(realpath -e -- "$1")
volume=${2:-fluxo_sqlite-data}
source_dir=$(docker volume inspect --format '{{.Mountpoint}}' "$volume")
test -d "$destination"
test -f "$source_dir/flows.sqlite3"
if [[ "$(stat -c %d -- "$source_dir")" == "$(stat -c %d -- "$destination")" ]]; then
  echo "Choose a destination on a different filesystem from the SQLite volume." >&2
  exit 1
fi

require_stopped() {
  local running
  running=$(docker ps --filter "volume=$volume" --format '{{.Names}}')
  if [[ -n "$running" ]]; then
    printf 'Stop the containers using %s before backing it up:\n%s\n' "$volume" "$running" >&2
    exit 1
  fi
}

require_stopped
name="fluxo-sqlite-$(date -u +%Y%m%dT%H%M%SZ).tar.xz"
partial=$(mktemp "$destination/.fluxo-sqlite-XXXXXXXX.tar.xz")
cleanup() { if [[ -e "$partial" ]]; then rm -- "$partial"; fi; }
trap cleanup EXIT
nice -n 10 tar --format=posix --numeric-owner -C "$source_dir" -cf - . |
  nice -n 10 xz -3 -T1 -c > "$partial"

# Decompression verifies the XZ checksum. Tar compares the contents against the stopped volume.
require_stopped
nice -n 10 xz -dc -- "$partial" | nice -n 10 tar -C "$source_dir" -df -
require_stopped
sha=$(sha256sum "$partial")
ln -- "$partial" "$destination/$name"
( set -o noclobber; printf '%s  %s\n' "${sha%% *}" "$name" > "$destination/$name.sha256" )
sync -f "$destination"
printf 'Verified byte-for-byte copy: %s\n' "$destination/$name"
