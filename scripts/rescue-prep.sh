#!/bin/bash
# Turn stock Mariner (root p3, /var p5) into a "rescue" OS whose /var lives on p4,
# freeing p5 for Ubuntu. Fully reversible with restore-stock-p3.sh plus the p5/p4
# images taken by percept-kit's backup.
#
# Usage: sudo rescue-prep.sh      (on stock Mariner; reboot afterwards)
set -euo pipefail

KIT=/etc/percept-kit
P4=/dev/mmcblk0p4
MNT=/mnt/pk-var
WANTS=/etc/systemd/system/multi-user.target.wants
# Services that would start Docker/IoT Edge, pull/update images, run A/B OTA or
# resize p5. Their multi-user wants links are parked in $KIT/wants-disabled.
UNITS="iotedge.service iotedge-reconfig-watcher.path iotedge-reconfig-watcher.service
iotedge-reprovision-watcher.path docker-watcher.path docker-watcher.service
preloadedimgs.service do-client-lite.service adu-agent.service containerd.service
resizefs.service"

log() { echo "### $(date +%T) $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

. /etc/os-release
[ "$ID" = mariner ] || die "not Mariner"
[ "$(findmnt -no SOURCE /)" = /dev/mmcblk0p3 ] || die "root is not mmcblk0p3"
[ ! -e $KIT/rescue ] || die "already in rescue layout"
[ "$(findmnt -no SOURCE /var)" = /dev/mmcblk0p5 ] || die "/var is not on mmcblk0p5"
! findmnt -rno SOURCE | grep -qx $P4 || die "$P4 is mounted"
grep -Eq '^/dev/mmcblk0p5[[:space:]]+/var[[:space:]]' /etc/fstab || die "unexpected /etc/fstab"

log "Saving fstab and parking service links in $KIT"
mkdir -p $KIT/wants-disabled
[ -f $KIT/fstab.orig ] || cp -p /etc/fstab $KIT/fstab.orig
for u in $UNITS; do
  systemctl stop "$u" 2>/dev/null || true
  if [ -L "$WANTS/$u" ]; then mv "$WANTS/$u" $KIT/wants-disabled/; fi
done
systemctl stop docker.socket docker.service containerd.service 2>/dev/null || true
systemctl daemon-reload
ls $KIT/wants-disabled

log "Formatting $P4 for Mariner /var"
NEED=$(cd /var && du -sxm --exclude=./lib/docker --exclude=./lib/containerd --exclude=./log/journal --exclude=./tmp . | cut -f1)
HAVE=$(( $(blockdev --getsize64 $P4) / 1048576 ))
echo "need ${NEED} MiB of ${HAVE} MiB"
[ "$NEED" -lt $((HAVE * 3 / 4)) ] || die "/var too large for $P4"
mkfs.ext4 -q -F -L pk-mariner-var $P4
mkdir -p $MNT; mount $P4 $MNT
trap 'umount $MNT 2>/dev/null || true' EXIT

log "Copying /var (without Docker images, journal and tmp contents)"
TARF=(--numeric-owner --xattrs '--xattrs-include=*' --acls)
set +e
tar -C /var --one-file-system "${TARF[@]}" --exclude=./lost+found --exclude=./lib/docker \
  --exclude=./lib/containerd --exclude='./log/journal/*' --exclude='./tmp/*' -cpf - . 2>/tmp/pk-tar.err \
  | tar -C $MNT "${TARF[@]}" -xpf -
RC=("${PIPESTATUS[@]}"); set -e
# rc 1 from the reader = "file changed as we read it" (live logs), which is fine.
[ "${RC[0]}" -le 1 ] && [ "${RC[1]}" -eq 0 ] || { cat /tmp/pk-tar.err; die "copy failed (${RC[*]})"; }
install -d -m 711 $MNT/lib/docker $MNT/lib/containerd
[ -e $MNT/rootfsresized ] && [ -d $MNT/custom-configs/home ] || die "copy incomplete"
df -h $MNT | tail -1

log "Switching /var to $P4"
sed -i -E 's#^/dev/mmcblk0p5([[:space:]]+/var[[:space:]])#/dev/mmcblk0p4\1#' /etc/fstab
grep -Eq '^/dev/mmcblk0p4[[:space:]]+/var[[:space:]]' /etc/fstab || die "fstab edit failed"
date -u +%FT%TZ > $KIT/rescue
sync
log "Done. Reboot; Mariner will come up with /var on $P4 and p5 free."
