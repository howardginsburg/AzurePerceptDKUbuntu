#!/bin/bash
# Install percept-kit's multi-boot grub.cfg (+ grubenv and the HDMI device tree, built
# from the ESP's pe100.dtb by build-hdmi-dtb.sh) onto the Percept's ESP. The signed shim/GRUB binaries are never touched, and the
# original grub.cfg is kept as grub.cfg.orig.
#
# Usage: sudo install-grub-cfg.sh <ubuntu-root-partuuid> [--default N] [--timeout S] [--next N]
#   Entries: 0 = Mariner, 1 = Ubuntu (HDMI), 2 = Ubuntu (original device tree)
#   --next N   one-shot entry for the next boot only (falls back to --default after)
# Or:    sudo install-grub-cfg.sh --next-only N    (just set the one-shot entry)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ESP=/boot/efi
GRUBDIR=$ESP/boot/grub2
DEFAULT=0 TIMEOUT=0 NEXT= PARTUUID= NEXT_ONLY=

while [ $# -gt 0 ]; do
  case $1 in
    --default) DEFAULT=$2; shift 2 ;;
    --timeout) TIMEOUT=$2; shift 2 ;;
    --next) NEXT=$2; shift 2 ;;
    --next-only) NEXT=$2; NEXT_ONLY=1; shift 2 ;;
    *) PARTUUID=$1; shift ;;
  esac
done

# grubenv is a 1024-byte block: header, key=value lines, '#' padding.
write_grubenv() {
  local body="# GRUB Environment Block"$'\n'
  [ -n "$NEXT" ] && body+="next_entry=$NEXT"$'\n'
  { printf '%s' "$body"; head -c $((1024 - ${#body})) /dev/zero | tr '\0' '#'; } > "$1"
}

mkdir -p "$ESP"
mountpoint -q "$ESP" || mount /dev/mmcblk0p1 "$ESP"

if [ -n "$NEXT_ONLY" ]; then
  grep -q 'Managed by percept-kit' "$GRUBDIR/grub.cfg" || { echo "grub.cfg is not percept-kit's"; exit 1; }
  write_grubenv "$GRUBDIR/grubenv"; sync
  echo "Next boot: entry $NEXT"; exit 0
fi

[ -n "$PARTUUID" ] || { echo "usage: $0 <ubuntu-root-partuuid> [--default N] [--timeout S] [--next N]"; exit 2; }
[ -f "$GRUBDIR/grub.cfg.orig" ] || cp -p "$GRUBDIR/grub.cfg" "$GRUBDIR/grub.cfg.orig"

WORK=$(mktemp -d)
sed -e "s/@UBUNTU_PARTUUID@/$PARTUUID/" -e "s/@DEFAULT@/$DEFAULT/" -e "s/@TIMEOUT@/$TIMEOUT/" \
  "$HERE/grub.cfg.template" > "$WORK/grub.cfg"
# Syntax-check with GRUB's own tool when available (native on Ubuntu, or from the
# Ubuntu rootfs being built at /mnt/ubuntu when running on Mariner).
if command -v grub-script-check >/dev/null; then
  grub-script-check "$WORK/grub.cfg"
elif [ -x /mnt/ubuntu/usr/bin/grub-script-check ]; then
  cp "$WORK/grub.cfg" /mnt/ubuntu/tmp/pk-grub.cfg
  chroot /mnt/ubuntu grub-script-check /tmp/pk-grub.cfg; rm -f /mnt/ubuntu/tmp/pk-grub.cfg
fi
write_grubenv "$WORK/grubenv"

# pe100-hdmi.dtb is built here from this device's own pe100.dtb (it isn't shipped with the kit).
# dtc comes from the Ubuntu rootfs being built (on Mariner) or from Ubuntu itself.
if [ -x /mnt/ubuntu/usr/bin/dtc ] && mountpoint -q /mnt/ubuntu; then
  D=/mnt/ubuntu/tmp/pk-dtb; rm -rf "$D"; mkdir -p "$D"
  cp "$HERE/build-hdmi-dtb.sh" "$HERE/hdmi-dcss-nodes.dtsi" "$ESP/pe100.dtb" "$D/"
  chroot /mnt/ubuntu sh /tmp/pk-dtb/build-hdmi-dtb.sh /tmp/pk-dtb/pe100.dtb /tmp/pk-dtb/pe100-hdmi.dtb
  cp "$D/pe100-hdmi.dtb" "$WORK/pe100-hdmi.dtb"; rm -rf "$D"
elif command -v dtc >/dev/null; then
  sh "$HERE/build-hdmi-dtb.sh" "$ESP/pe100.dtb" "$WORK/pe100-hdmi.dtb"
elif [ -f "$ESP/pe100-hdmi.dtb" ]; then
  echo "dtc not found; keeping the existing $ESP/pe100-hdmi.dtb"
else
  echo "dtc not found (install device-tree-compiler); cannot build pe100-hdmi.dtb"; exit 1
fi
if [ -f "$WORK/pe100-hdmi.dtb" ]; then
  cmp -s "$WORK/pe100-hdmi.dtb" "$ESP/pe100-hdmi.dtb" || cp "$WORK/pe100-hdmi.dtb" "$ESP/pe100-hdmi.dtb"
fi
cp "$WORK/grub.cfg" "$GRUBDIR/grub.cfg.new" && mv -f "$GRUBDIR/grub.cfg.new" "$GRUBDIR/grub.cfg"
cp "$WORK/grubenv" "$GRUBDIR/grubenv"
sync
cmp "$WORK/grub.cfg" "$GRUBDIR/grub.cfg"
rm -rf "$WORK"
echo "Installed grub.cfg (default=$DEFAULT timeout=$TIMEOUT next=${NEXT:-none})"
