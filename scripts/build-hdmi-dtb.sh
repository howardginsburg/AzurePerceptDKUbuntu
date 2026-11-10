#!/bin/sh
# Build pe100-hdmi.dtb from Microsoft's pe100.dtb (read from the device's own ESP) by inserting
# the i.MX8MQ DCSS display controller + HDMI TX nodes from hdmi-dcss-nodes.dtsi.
# Needs dtc (Debian/Ubuntu: device-tree-compiler; Alpine: dtc). See PROVENANCE.md, "HDMI: the console and the camera".
#
# Usage: build-hdmi-dtb.sh <pe100.dtb> <out.dtb>
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
IN=${1:?usage: build-hdmi-dtb.sh <pe100.dtb> <out.dtb>}
OUT=${2:?usage: build-hdmi-dtb.sh <pe100.dtb> <out.dtb>}
FRAG=$HERE/hdmi-dcss-nodes.dtsi
ANCHOR='interrupt-controller@32e2d000 {'
# The pe100.dtb this was developed and tested with, and the result it must give (dtc 1.6/1.7).
KNOWN_IN=629ff5e7c149451aec0e007e0418152f24a872a95bc673a85a007fd40d08a86e
KNOWN_OUT=5d7b17b976e12b1b53ff9e1e22abff7c5e8b6a46df2bd046c0aaba4e6bf624db

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
dtc -q -I dtb -O dts -o "$tmp/pe100.dts" "$IN"

# Sanity: the anchor (irqsteer, phandle 0x44, which DCSS uses as interrupt parent) exists once,
# the clock controller is phandle 0x02, and the display nodes aren't already there.
[ "$(grep -c "$ANCHOR" "$tmp/pe100.dts")" = 1 ] || { echo "anchor '$ANCHOR' not found exactly once" >&2; exit 1; }
grep -A12 "$ANCHOR" "$tmp/pe100.dts" | grep -q 'phandle = <0x44>' || { echo "irqsteer is not phandle 0x44" >&2; exit 1; }
grep -A60 'clock-controller@30380000 {' "$tmp/pe100.dts" | grep -q 'phandle = <0x02>' || { echo "clock controller is not phandle 0x02" >&2; exit 1; }
if grep -q 'hdmi@32c00000\|display-controller@32e00000' "$tmp/pe100.dts"; then echo "input already has display nodes" >&2; exit 1; fi

awk -v frag="$FRAG" -v anchor="$ANCHOR" '
  index($0, anchor) { while ((getline line < frag) > 0) print line }
  { print }' "$tmp/pe100.dts" > "$tmp/pe100-hdmi.dts"
dtc -q -I dts -O dtb -o "$tmp/out.dtb" "$tmp/pe100-hdmi.dts"

in_sum=$(sha256sum "$IN" | cut -c1-64)
out_sum=$(sha256sum "$tmp/out.dtb" | cut -c1-64)
if [ "$in_sum" = "$KNOWN_IN" ]; then
  [ "$out_sum" = "$KNOWN_OUT" ] || { echo "result $out_sum differs from the tested $KNOWN_OUT" >&2; exit 1; }
else
  echo "WARNING: $IN ($in_sum) is not the tested pe100.dtb; HDMI on this firmware is untested" >&2
fi
cp "$tmp/out.dtb" "$OUT"
echo "$out_sum  $OUT"