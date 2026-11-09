#!/bin/bash
# Undo rescue-prep.sh on Mariner's root (p3): original fstab (/var back on p5) and
# service links. Run from rescue Mariner AFTER the stock p5 image has been written
# back, then reboot into stock Mariner.
#
# Usage: sudo restore-stock-p3.sh
set -euo pipefail

KIT=/etc/percept-kit
WANTS=/etc/systemd/system/multi-user.target.wants
die() { echo "ERROR: $*" >&2; exit 1; }

[ -e $KIT/rescue ] || die "not in rescue layout ($KIT/rescue missing)"
[ -f $KIT/fstab.orig ] || die "$KIT/fstab.orig missing"
[ "$(findmnt -no SOURCE /)" = /dev/mmcblk0p3 ] || die "root is not mmcblk0p3"
[ "$(findmnt -no SOURCE /var)" != /dev/mmcblk0p5 ] || die "/var is already on p5"
[ "$(blkid -s TYPE -o value /dev/mmcblk0p5)" = ext4 ] || die "p5 has no filesystem; write the p5 image first"
# The stock /var must be back on p5 before we point fstab at it.
mkdir -p /mnt/pk-p5; mount -o ro /dev/mmcblk0p5 /mnt/pk-p5
ok=0; [ -d /mnt/pk-p5/custom-configs ] && [ ! -e /mnt/pk-p5/etc/os-release ] && ok=1
umount /mnt/pk-p5
[ $ok = 1 ] || die "p5 does not hold Mariner's /var; write the p5 image first"

cp -p $KIT/fstab.orig /etc/fstab
for l in $KIT/wants-disabled/*; do if [ -L "$l" ]; then mv "$l" $WANTS/; fi; done
systemctl daemon-reload
rm -rf $KIT
sync
echo "p3 restored to stock. Reboot into stock Mariner."
