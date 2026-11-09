#!/bin/bash
# Stream a block device to stdout as zstd, recording raw and compressed SHA-256s.
# Usage: sudo dump.sh <device> <tag>   ->  /run/pk-<tag>.raw /run/pk-<tag>.zst hold the hashes
set -euo pipefail
DEV=$1 TAG=$2
case $DEV in /dev/mmcblk0p[1-5]|/dev/mmcblk0boot[01]) ;; *) echo "refusing $DEV" >&2; exit 2 ;; esac
R=/run/pk-$TAG
rm -f "$R".raw "$R".zst "$R".raw.tmp "$R".zst.tmp
dd if="$DEV" bs=4M status=none \
  | tee >(sha256sum | cut -c1-64 > "$R".raw.tmp) \
  | zstd -3 -T0 -q -c \
  | tee >(sha256sum | cut -c1-64 > "$R".zst.tmp)
# The hashers finish shortly after the pipeline; wait for both.
for _ in $(seq 120); do
  if [ -s "$R".raw.tmp ] && [ -s "$R".zst.tmp ]; then break; fi
  sleep 0.5
done
mv "$R".raw.tmp "$R".raw
mv "$R".zst.tmp "$R".zst
