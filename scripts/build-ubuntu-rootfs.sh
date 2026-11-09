#!/bin/bash
# Build an Ubuntu 22.04 arm64 rootfs from a running Azure Percept DK (CBL-Mariner).
# The Microsoft-signed Mariner kernel is reused because Secure Boot only trusts it,
# so its modules and firmware are copied into Ubuntu.
#
# Usage: sudo build-ubuntu-rootfs.sh /dev/sdX          (wipes the whole USB disk)
#        sudo build-ubuntu-rootfs.sh /dev/mmcblk0p5    (eMMC; rescue layout only, see rescue-prep.sh)
# Env:   PK_HOSTNAME (default percept-ubuntu)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DEV=${1:?target: USB disk (/dev/sdX) or /dev/mmcblk0p5}
REL=22.04.5
BASE_URL=https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release
TARBALL=ubuntu-base-${REL}-base-arm64.tar.gz
MNT=/mnt/ubuntu
KVER=$(uname -r)
NEWUSER=${SUDO_USER:?run with sudo (or set SUDO_USER) so the device user can be carried over}
NEWHOST=${PK_HOSTNAME:-percept-ubuntu}
WORK=/var/tmp/ubuntu-build

log() { echo "### $(date +%T) $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Safety: only a USB disk that isn't mounted, or p5 once Mariner's /var has moved off it.
if [ "$DEV" = /dev/mmcblk0p5 ]; then
  [ -e /etc/percept-kit/rescue ] || die "p5 is only a target in the rescue layout (run rescue-prep.sh)"
  ! findmnt -rno SOURCE | grep -qx "$DEV" || die "$DEV is mounted"
  PART=$DEV
  log "Formatting $PART"
  wipefs -a "$PART"
else
  [ "$(lsblk -dno TRAN "$DEV")" = "usb" ] || die "$DEV is not a USB disk"
  if grep -q "^${DEV}" /proc/mounts; then die "$DEV is mounted"; fi
  log "Partitioning $DEV"
  wipefs -a "$DEV"
  printf 'label: gpt\n,,L\n' | sfdisk -q "$DEV"
  partprobe "$DEV" 2>/dev/null || true; udevadm settle; sleep 2
  PART=${DEV}1
fi
mkfs.ext4 -q -F -L ubuntu-root "$PART"
mkdir -p "$MNT"; ! mountpoint -q "$MNT" || die "$MNT is already mounted"
mount "$PART" "$MNT"

log "Downloading $TARBALL"
mkdir -p "$WORK"; cd "$WORK"
[ -f "$TARBALL" ] || curl -fsSLO "$BASE_URL/$TARBALL"
curl -fsSL "$BASE_URL/SHA256SUMS" | grep " \*\?${TARBALL}\$" | sha256sum -c -
tar -xpzf "$TARBALL" -C "$MNT" --numeric-owner

log "Preparing chroot"
rm -f "$MNT/etc/resolv.conf"
grep -E '^nameserver' /run/systemd/resolve/resolv.conf > "$MNT/etc/resolv.conf" || echo 'nameserver 1.1.1.1' > "$MNT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"; chmod +x "$MNT/usr/sbin/policy-rc.d"
for d in dev dev/pts proc sys; do mount --bind /$d "$MNT/$d"; done
trap 'for d in sys proc dev/pts dev; do umount -l "$MNT/$d" 2>/dev/null || true; done' EXIT

# No snaps: the Mariner kernel has no AppArmor, so snap confinement can't work.
cat > "$MNT/etc/apt/preferences.d/no-snapd" <<'EOF'
Package: snapd
Pin: release *
Pin-Priority: -1
EOF

log "Installing packages"
chroot "$MNT" /bin/bash -e <<'EOF'
export DEBIAN_FRONTEND=noninteractive LC_ALL=C
apt-get update -q
apt-get install -y -q ubuntu-minimal systemd-sysv systemd-timesyncd udev kmod \
  openssh-server sudo wpasupplicant iw wireless-regdb rfkill bluez \
  ca-certificates curl nano less htop iputils-ping iproute2 bind9-dnsutils rsync \
  usbutils pciutils locales tzdata grub-common tpm2-tools zstd device-tree-compiler \
  docker.io docker-compose-v2 docker-buildx
apt-get -y -q dist-upgrade
apt-get clean
locale-gen en_US.UTF-8 >/dev/null
update-locale LANG=en_US.UTF-8
ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime
# The Mariner kernel has no nf_tables; Docker needs the legacy iptables backend.
update-alternatives --set iptables /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
EOF

log "Copying Mariner kernel, modules and firmware ($KVER)"
mkdir -p "$MNT/lib/modules" "$MNT/lib/firmware" "$MNT/boot"
cp -a "/lib/modules/$KVER" "$MNT/lib/modules/"
cp -a /lib/firmware/. "$MNT/lib/firmware/"
cp -a /boot/vmlinuz /boot/config-"$KVER" /boot/System.map-"$KVER" "$MNT/boot/"
chroot "$MNT" depmod -a "$KVER"
cp /etc/modprobe.d/wifi.conf "$MNT/etc/modprobe.d/88x2ce.conf"
cp /etc/modules-load.d/wifi.conf "$MNT/etc/modules-load.d/88x2ce.conf"
# The Realtek driver logs every Wi-Fi power-save change at its default level (4, info): about
# 10,000 journal lines an hour. Errors only.
echo 'options 88x2ce rtw_drv_log_level=2' > "$MNT/etc/modprobe.d/percept-kit-wifi.conf"

log "Percept device rules (Ear/Eye SoM, TPM)"
chroot "$MNT" groupadd -f -g 5000 apdk_accessories
chroot "$MNT" getent group tss >/dev/null || chroot "$MNT" groupadd -r tss
cp /etc/udev/rules.d/80-azurepercept-accessories.rules "$MNT/etc/udev/rules.d/"
cat > "$MNT/etc/udev/rules.d/60-tpm.rules" <<'EOF'
KERNEL=="tpm[0-9]*", MODE="0660", GROUP="tss"
KERNEL=="tpmrm[0-9]*", MODE="0660", GROUP="tss"
EOF

log "System configuration"
echo "$NEWHOST" > "$MNT/etc/hostname"
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s\n::1\tlocalhost ip6-localhost ip6-loopback\n' "$NEWHOST" > "$MNT/etc/hosts"
PARTUUID=$(blkid -s PARTUUID -o value "$PART")
cat > "$MNT/etc/fstab" <<EOF
PARTUUID=$PARTUUID  /          ext4  defaults,noatime,errors=remount-ro  0 1
/dev/mmcblk0p1      /boot/efi  vfat  noauto,umask=0077                   0 0
EOF
mkdir -p "$MNT/boot/efi"

# Network: same model as Mariner (systemd-networkd + wpa_supplicant on wlan0).
mkdir -p "$MNT/etc/systemd/network"
# Unplugged Ethernet must not hold up boot in systemd-networkd-wait-online.
printf '[Match]\nName=e*\n\n[Link]\nRequiredForOnline=no\n\n[Network]\nDHCP=yes\nIPv6AcceptRA=yes\n' > "$MNT/etc/systemd/network/20-wired.network"
printf '[Match]\nName=wlan0\n\n[Network]\nDHCP=yes\nIPv6AcceptRA=yes\n' > "$MNT/etc/systemd/network/25-wlan0.network"
install -m 600 /var/custom-configs/etc/wpa_supplicant/wpa_supplicant-wlan0.conf "$MNT/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"

# Reboot automatically if the system hangs.
mkdir -p "$MNT/etc/systemd/system.conf.d"
printf '[Manager]\nRuntimeWatchdogSec=30\nRebootWatchdogSec=2min\n' > "$MNT/etc/systemd/system.conf.d/watchdog.conf"
# Cap the journal: the eMMC is small.
mkdir -p "$MNT/etc/systemd/journald.conf.d"
printf '[Journal]\nSystemMaxUse=200M\n' > "$MNT/etc/systemd/journald.conf.d/percept-kit.conf"

# The Mariner kernel lacks the iptables raw table (CONFIG_IP_NF_RAW) that Docker 28+ uses.
mkdir -p "$MNT/etc/systemd/system/docker.service.d"
printf '[Service]\nEnvironment=DOCKER_INSECURE_NO_IPTABLES_RAW=1\n' > "$MNT/etc/systemd/system/docker.service.d/no-raw.conf"
# Classic overlay2 store (the containerd snapshotter roughly triples disk use) and
# size-capped container logs: the eMMC is small.
mkdir -p "$MNT/etc/docker"
printf '{\n  "features": { "containerd-snapshotter": false },\n  "log-driver": "local"\n}\n' > "$MNT/etc/docker/daemon.json"

# Ear SoM: it re-enumerates late in boot and snd-usb-audio can miss it.
install -m 644 "$HERE/host/81-percept-ear.rules" "$MNT/etc/udev/rules.d/"
install -m 644 "$HERE/host/percept-ear-audio.service" "$MNT/etc/systemd/system/"

# Carry over percept-kit's passwordless sudo rule so the kit keeps working.
if [ -f /etc/sudoers.d/90-percept-kit ]; then
  install -m 440 /etc/sudoers.d/90-percept-kit "$MNT/etc/sudoers.d/90-percept-kit"
fi

# Keep the device's SSH identity so existing known_hosts entries still match.
cp -a /etc/ssh/ssh_host_* "$MNT/etc/ssh/"

log "User $NEWUSER"
chroot "$MNT" useradd -m -u 10000 -s /bin/bash -G sudo,adm,docker,video,plugdev,apdk_accessories,tss "$NEWUSER"
HASH=$(awk -F: -v u="$NEWUSER" '$1==u{print $2}' /etc/shadow)
if [ -n "$HASH" ] && [ "${HASH:0:1}" != "!" ]; then
  chroot "$MNT" usermod -p "$HASH" "$NEWUSER"
else
  echo "WARNING: no password hash found for $NEWUSER; key-only login"
fi
install -d -m 700 -o 10000 -g 10000 "$MNT/home/$NEWUSER/.ssh"
install -m 600 -o 10000 -g 10000 "/home/$NEWUSER/.ssh/authorized_keys" "$MNT/home/$NEWUSER/.ssh/authorized_keys"

log "Enabling services"
chroot "$MNT" systemctl enable systemd-networkd systemd-resolved systemd-timesyncd \
  wpa_supplicant@wlan0 ssh serial-getty@ttymxc0
chroot "$MNT" systemctl disable wpa_supplicant 2>/dev/null || true
# grub-common is only installed for its tools; the ESP grub.cfg is managed by install-grub-cfg.sh.
chroot "$MNT" systemctl disable grub-common grub-initrd-fallback 2>/dev/null || true

rm -f "$MNT/usr/sbin/policy-rc.d"
ln -sf ../run/systemd/resolve/stub-resolv.conf "$MNT/etc/resolv.conf"

log "Done. PARTUUID=$PARTUUID"
du -sxh "$MNT"
