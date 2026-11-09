#!/bin/bash
# Prints key=value facts that percept-kit uses to decide what to do. Read-only.
. /etc/os-release 2>/dev/null
root=$(findmnt -no SOURCE /)
var=$(findmnt -no SOURCE /var 2>/dev/null || true); [ -n "$var" ] || var=$root
state=unknown
if [ "$ID" = mariner ] && [ "$root" = /dev/mmcblk0p3 ]; then
  if [ -e /etc/percept-kit/rescue ]; then state=rescue
  elif [ "$var" = /dev/mmcblk0p5 ]; then state=stock; fi
elif [ "$ID" = ubuntu ]; then
  case $root in /dev/mmcblk0p5) state=ubuntu ;; /dev/sd*) state=usb-ubuntu ;; esac
fi
S() { sudo -n "$@" 2>/dev/null; }
echo "state=$state"
echo "os=$PRETTY_NAME"
echo "root=$root"
echo "var=$var"
echo "home=$HOME"
echo "hostname=$(hostname)"
echo "cid=$(cat /sys/block/mmcblk0/device/cid 2>/dev/null)"
echo "model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
echo "bootid=$(cat /proc/sys/kernel/random/boot_id)"
echo "sudo=$(S true && echo yes || echo no)"
echo "kitsudo=$(S test -e /etc/sudoers.d/90-percept-kit && echo yes || echo no)"
echo "p4label=$(S blkid -s LABEL -o value /dev/mmcblk0p4)"
echo "p5type=$(S blkid -s TYPE -o value /dev/mmcblk0p5)"
echo "p5label=$(S blkid -s LABEL -o value /dev/mmcblk0p5)"
echo "p5partuuid=$(S blkid -s PARTUUID -o value /dev/mmcblk0p5)"
cfg=/boot/efi/boot/grub2/grub.cfg
if S test -f $cfg; then
  if S grep -q 'Managed by percept-kit' $cfg; then echo grub=kit; else echo grub=stock; fi
  echo "grubdefault=$(S sed -n 's/^set default=//p' $cfg)"
else
  echo grub=unmounted
fi
