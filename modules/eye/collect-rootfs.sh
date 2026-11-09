#!/bin/bash
# Assemble a minimal root filesystem for the Eye module from the build stage.
# Copies only the inference app, its runtime closure (ldd), the Myriad X
# firmware, GStreamer plugins, OpenVINO's myriad_compile (for custom IR models)
# and the handful of shell tools the app shells out to.
set -euo pipefail
R=${1:-/rootfs}
APP_BIN=${APP_BIN:-/src/build/inference}
APP_SRC=${APP_SRC:-/src/app}
OV=/openvino/bin/aarch64/Release

rm -rf "$R"
mkdir -p "$R"/{usr/bin,usr/sbin,usr/lib,usr/libexec,etc,tmp,dev,proc,sys,run}
chmod 1777 "$R/tmp"
# Mirror the merged-/usr layout (bin -> usr/bin, lib -> usr/lib, usr/lib64 -> lib).
# GStreamer's compiled-in plugin dir is /usr/lib64/gstreamer-1.0.
for l in /bin /sbin /lib /lib64 /usr/lib64; do
    if [ -L "$l" ]; then ln -s "$(readlink "$l")" "$R$l"; fi
done

# copy <abs path>: copy a file into $R, recreating any symlink chain.
copy() {
    local p=$1 t
    if [ -e "$R$p" ] || [ -L "$R$p" ] || [ ! -e "$p" ]; then return 0; fi
    mkdir -p "$R$(dirname "$p")"
    if [ -L "$p" ]; then
        cp -P "$p" "$R$p"
        t=$(readlink "$p")
        case $t in /*) copy "$t" ;; *) copy "$(dirname "$p")/$t" ;; esac
    else
        cp -p "$p" "$R$p"
    fi
}

is_elf() { [ "$(head -c4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = 7f454c46 ]; }

# --- payload --------------------------------------------------------------
mkdir -p "$R/app/model" "$R/app/snapshot"
cp "$APP_BIN" "$R/app/inference"
cp -r "$APP_SRC/data" "$R/app/data"

export LD_LIBRARY_PATH=/eyesom/build/install/lib64:/eyesom/mxif/install/lib:/onnxruntime/install/lib64:$OV/lib

copy /eyesom/mx.mvcmd
for f in /eyesom/build/install/lib64/*.so* /eyesom/mxif/install/lib/*.so* \
         /onnxruntime/install/lib64/*.so* \
         "$OV"/lib/*.so "$OV"/lib/plugins.xml "$OV"/lib/*.mvcmd \
         "$OV"/myriad_compile "$OV"/custom_myriad_compile \
         /usr/libexec/gstreamer-1.0/gst-plugin-scanner; do
    copy "$f"
done
# GStreamer plugins, minus those whose dependencies are absent in the base image.
for f in /usr/lib/gstreamer-1.0/*.so; do
    deps=$(ldd "$f" 2>/dev/null || true)
    case $deps in *"not found"*) ;; *) copy "$f" ;; esac
done
for b in bash sh rm mkdir mv cp ls cat stat cut unzip wget; do
    copy "$(command -v "$b")"
done
# Name-service modules are dlopen()ed by glibc (DNS for wget / IoT SDK).
for f in /usr/lib/libnss_files* /usr/lib/libnss_dns* /usr/lib/libresolv*; do copy "$f"; done
# CA bundle.
for f in /etc/pki/tls/certs/* /etc/pki/tls/cert.pem; do copy "$f"; done
copy /etc/ssl/certs
# Licence texts of the libraries the Dockerfile adds (Paho MQTT C, cpp-httplib).
if [ -d /third_party/licenses ]; then
    mkdir -p "$R/usr/share/licenses"
    cp -r /third_party/licenses/. "$R/usr/share/licenses/"
fi

# --- shared-library closure -------------------------------------------------
find "$R" -type f | while read -r f; do
    is_elf "$f" || continue
    ldd "$f" 2>/dev/null | awk '/=> \//{print $3} /^[ \t]*\/[^ ]+ \(/{print $1}' || true
done | sort -u | while read -r lib; do copy "$lib"; done

# --- config -----------------------------------------------------------------
cat > "$R/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/bash
apdk_app:x:5001:5000::/app:/bin/bash
EOF
cat > "$R/etc/group" <<'EOF'
root:x:0:
apdk_accessories:x:5000:apdk_app
EOF
printf 'passwd: files\ngroup: files\nhosts: files dns\n' > "$R/etc/nsswitch.conf"
printf '%s\n' /usr/lib /usr/local/lib /eyesom/build/install/lib64 /eyesom/mxif/install/lib \
    /onnxruntime/install/lib64 "$OV/lib" > "$R/etc/ld.so.conf"
ldconfig -r "$R"

find "$R" -type f -name '*.so*' -exec strip --strip-unneeded {} + 2>/dev/null || true
strip "$R/app/inference"
chown -R 5001:5000 "$R/app"
du -sh "$R"
