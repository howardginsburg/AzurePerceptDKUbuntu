#!/bin/bash
# Read-only hardware/software assessment of an Azure Percept DK. Prints a report.
S() { sudo -n "$@" 2>/dev/null; }
section() { printf '\n== %s ==\n' "$1"; }
. /etc/os-release 2>/dev/null

section Identity
echo "Model:    $(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"
echo "eMMC CID: $(cat /sys/block/mmcblk0/device/cid 2>/dev/null)"
echo "Hostname: $(hostname)"
echo "OS:       $PRETTY_NAME"
echo "Kernel:   $(uname -r) ($(uname -m))"
echo "Uptime:   $(uptime -p 2>/dev/null || uptime)"

section Boot
sb=$(od -An -tu1 /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null | awk '{print $NF}')
if [ "$sb" = 1 ]; then echo "Secure Boot: enabled"; else echo "Secure Boot: disabled/unknown"; fi
echo "Root:        $(findmnt -no SOURCE /)   /var: $(findmnt -no SOURCE /var 2>/dev/null || echo '(on root)')"
echo "Cmdline:     $(cat /proc/cmdline)"
if [ -e /etc/percept-kit/rescue ]; then echo "Layout:      percept-kit rescue Mariner (since $(cat /etc/percept-kit/rescue))"; fi
cfg=/boot/efi/boot/grub2/grub.cfg
if S test -f $cfg; then
  if S grep -q 'Managed by percept-kit' $cfg; then
    echo "GRUB:        percept-kit multi-boot (default entry $(S sed -n 's/^set default=//p' $cfg))"
  else echo "GRUB:        stock"; fi
fi

section Storage
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT 2>/dev/null
echo; df -h -x tmpfs -x devtmpfs -x overlay 2>/dev/null

section "CPU / memory"
echo "CPUs: $(nproc)"
free -m
for z in /sys/class/thermal/thermal_zone*/temp; do
  if [ -f "$z" ]; then echo "Thermal $(basename "$(dirname "$z")"): $(( $(cat "$z") / 1000 )) C"; fi
done

section Accessories
for d in /sys/bus/usb/devices/*; do
  [ -f "$d/idVendor" ] || continue
  id=$(cat "$d/idVendor"):$(cat "$d/idProduct")
  case $id in
    045e:066f) note="<- Azure Eye SoM" ;; 03e7:*) note="<- Myriad X VPU (Eye)" ;;
    045e:0671) note="<- Azure Ear SoM audio" ;; 045e:0673) note="<- Azure Ear SoM controller" ;;
    *) note="" ;;
  esac
  printf '%s  %-28s %s\n' "$id" "$(cat "$d/product" 2>/dev/null)" "$note"
done
echo; echo "Sound cards:"; cat /proc/asound/cards 2>/dev/null
echo "TPM: $(ls /dev/tpm0 /dev/tpmrm0 2>/dev/null | tr '\n' ' ')"

section Network
ip -br addr 2>/dev/null | grep -v '^lo '
ip route show default 2>/dev/null
if command -v iw >/dev/null; then iw dev wlan0 link 2>/dev/null | grep -E 'SSID|signal|bitrate'; fi

section Services
for u in sshd ssh docker containerd iotedge nginx; do
  st=$(systemctl is-active "$u" 2>/dev/null)
  case $st in active|activating|failed) echo "$u: $st" ;; esac
done
echo "Failed units:"; systemctl --failed --no-legend 2>/dev/null | sed 's/^/  /'

section Containers
if S docker info >/dev/null; then
  S docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
  echo; S docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}'
else
  echo "Docker not running"
fi
