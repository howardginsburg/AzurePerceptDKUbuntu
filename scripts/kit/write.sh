#!/bin/bash
# Write a zstd partition image from stdin to an eMMC partition, then verify it by
# reading the partition back. Only partitions that are safe in the current layout.
# Usage: sudo write.sh <device> <raw-sha256>   < image.zst
set -euo pipefail
DEV=$1 SHA=$2
die() { echo "ERROR: $*" >&2; exit 1; }
case $DEV in
  /dev/mmcblk0p1) if mountpoint -q /boot/efi; then umount /boot/efi; fi ;;
  /dev/mmcblk0p5) [ -e /etc/percept-kit/rescue ] || die "p5 can only be written from rescue Mariner" ;;
  /dev/mmcblk0p4) [ "$(findmnt -no SOURCE /var)" = /dev/mmcblk0p5 ] || die "p4 is only written from stock Mariner" ;;
  *) die "refusing $DEV" ;;
esac
if findmnt -rno SOURCE | grep -qx "$DEV"; then die "$DEV is mounted"; fi
zstd -dc | dd of="$DEV" bs=4M conv=fsync status=none
sync; echo 1 > /proc/sys/vm/drop_caches
GOT=$(dd if="$DEV" bs=4M status=none | sha256sum | cut -c1-64)
[ "$GOT" = "$SHA" ] || die "verify failed for $DEV: got $GOT want $SHA"
echo "verified $DEV"
