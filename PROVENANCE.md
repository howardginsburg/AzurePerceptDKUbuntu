# Provenance: where every piece came from

This document accounts for every file percept-kit puts on the device or into an image:
- where it came from;
- how it was derived;
- why it's needed;
- the obstacles hit on the way, and how each was solved.

[README.md](README.md) explains how the pieces work together. Each piece falls into one of
three kinds:

| Kind | Meaning |
|---|---|
| **Device** | Copied unchanged from the Percept DK's own Mariner OS (Microsoft binaries, signed or not). It never leaves the device except inside *your* backups |
| **Downloaded** | Fetched from a public upstream at a pinned version. Verified by SHA-256 or by the upstream's signed checksum list |
| **Authored** | Written for this project. Each section says what upstream facts it is based on |

## Contents

1. [Inventory](#1-inventory)
2. [Boot: grub.cfg template and one-shot boots](#2-boot-grubcfg-template-and-one-shot-boots)
3. [The HDMI device tree, pe100-hdmi.dtb](#3-the-hdmi-device-tree-pe100-hdmidtb)
4. [Rescue Mariner layout](#4-rescue-mariner-layout)
5. [Ubuntu root filesystem and the kernel gaps](#5-ubuntu-root-filesystem-and-the-kernel-gaps)
6. [Ear audio binding fix](#6-ear-audio-binding-fix)
7. [Eye app source, modules/eye/app](#7-eye-app-source-moduleseyeapp)
8. [Eye image: Dockerfile and collect-rootfs.sh](#8-eye-image-dockerfile-and-collect-rootfssh)
9. [Eye inference outputs, app/output](#9-eye-inference-outputs-appoutput)
10. [Ear module](#10-ear-module)
11. [PC kit and operational obstacles](#11-pc-kit-and-operational-obstacles)
12. [Licences and what must not be shared](#12-licences-and-what-must-not-be-shared)
13. [Verify it yourself](#13-verify-it-yourself)
14. [Mouth module, Bluetooth and the voice sample](#14-mouth-module-bluetooth-and-the-voice-sample)
15. [Dashboard and Myriad X telemetry](#15-dashboard-and-myriad-x-telemetry)
16. [The device stack: compose.yaml and deploy](#16-the-device-stack-composeyaml-and-deploy)

---

## 1. Inventory

### Taken from the device (unchanged)

| Item | Where it lives on Mariner | Where percept-kit puts it | Why |
|---|---|---|---|
| shim `bootaa64.efi`, GRUB `grubaa64.efi` | ESP (p1) | Left in place; never written | Only binaries Secure Boot accepts |
| Kernel `vmlinuz` 5.4.3-26.cm1 | p3 `/boot` | Left in place; booted by both OSes. Copy plus `config`/`System.map` in Ubuntu's `/boot` | Only kernel shim accepts (section 2) |
| `/lib/modules/5.4.3-26.cm1`, `/lib/firmware` | p3 | Ubuntu root | Drivers and firmware matching that kernel (Wi-Fi, BT, GPU, VPU) |
| `pe100.dtb` | ESP | Left in place. Input to `pe100-hdmi.dtb`. Copy in `dtb/` | Microsoft's board device tree |
| `80-azurepercept-accessories.rules` | `/etc/udev/rules.d` | Ubuntu, same path | Gives group `apdk_accessories` (gid 5000) access to the Eye/Ear SoMs |
| `modprobe.d/wifi.conf`, `modules-load.d/wifi.conf` | `/etc` | Ubuntu, renamed `88x2ce.conf` | Load options for the out-of-tree Realtek `88x2ce` Wi-Fi driver |
| `wpa_supplicant-wlan0.conf` | `/var/custom-configs/etc/wpa_supplicant` | Ubuntu `/etc/wpa_supplicant` (mode 600) | Your Wi-Fi network from the Percept setup wizard |
| SSH host keys | `/etc/ssh/ssh_host_*` | Ubuntu `/etc/ssh` | The device keeps its SSH identity |
| Your password hash, `authorized_keys` | `/etc/shadow`, `~/.ssh` | Ubuntu user (uid 10000) | Same login on both OSes |
| `/etc/sudoers.d/90-percept-kit` | Written by `connect` | Ubuntu, same path | The kit keeps passwordless sudo |

### Downloaded (pinned)

| Item | Source | Pin / verification | Licence | Used for |
|---|---|---|---|---|
| Ubuntu base | `cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-arm64.tar.gz` | Ubuntu's `SHA256SUMS` | Ubuntu (various OSS) | Ubuntu root (section 5) |
| Ubuntu packages | Ubuntu `jammy` apt archive | apt's signed `Release` files | Various OSS | Ubuntu root |
| NXP device-tree sources | `github.com/nxp-imx/linux-imx`, tag `imx_5.4.3_2.0.0`, `arch/arm64/boot/dts/freescale/imx8mq.dtsi`, `imx8mq-evk.dts`, `include/dt-bindings/clock/imx8mq-clock.h` | Tag matches the kernel (5.4.3) | `(GPL-2.0+ OR MIT)`, `(GPL-2.0 OR MIT)`, `GPL-2.0` | Reference values for the HDMI nodes (section 3). Saved in `dtb/` |
| Eye app source | `github.com/microsoft/azure-percept-advanced-development`, `azureeyemodule/app`, commit `b6782ded19c342e53a02b958fe38053d43e74507` | Git commit hash | MIT (`modules/eye/LICENSE.txt`, `NOTICE.txt`) | `modules/eye/app` (section 7) |
| Eye build base | `mcr.microsoft.com/azureedgedevices/azureeyemodule:2301-1-noauth` | Tag; pulled on the device, or loaded from the device's own backup (section 8) | Microsoft image (see section 12) | Build stage only (section 8) |
| Eclipse Paho MQTT C | `github.com/eclipse-paho/paho.mqtt.c` tag `v1.3.16` tarball | SHA-256 `8b960f51…fd14c5` in the Dockerfile | EPL-2.0 / EDL-1.0 | Eye MQTT output (section 9) |
| cpp-httplib | `github.com/yhirose/cpp-httplib` tag `v0.65.0`, `httplib.h` + `LICENSE` | SHA-256 `17d85dcf…10eeaa` / `4b45cbe1…364d8b` | MIT | Eye HTTP output (section 9) |
| Alpine | `alpine:3.24` + `apk` `python3`, `alsa-utils` | Tag | Various OSS | Ear image (section 10) |
| Ubuntu image + packages | `ubuntu:22.04` + apt `python3`, `alsa-utils`, `espeak-ng`, `libasound2-plugin-bluez`, `bluez-alsa-utils`, `bluez` | Tag; apt's signed `Release` files | Various OSS (espeak-ng GPL-3.0, BlueALSA MIT, BlueZ GPL-2.0) | Mouth image (section 14) |
| piper-tts | PyPI `piper-tts==1.8.0` (manylinux aarch64 wheel; [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)) | SHA-256 `3f60c191…a38484` in `modules/mouth/requirements.txt` | GPL-3.0-or-later (bundles espeak-ng, GPL-3.0) | Mouth neural text-to-speech (section 14) |
| onnxruntime | PyPI `onnxruntime==1.23.2` (cp310 aarch64 wheel) | SHA-256 `8f7d1fe0…e8e8d2` | MIT | Runs the Piper voice |
| numpy | PyPI `numpy==2.2.6` (cp310 aarch64 wheel) | SHA-256 `efd28d4e…a38e83` | BSD-3-Clause | Needed by piper-tts and onnxruntime |
| Piper voice | `huggingface.co/rhasspy/piper-voices`, revision `e7229f7d33a10e145e109b886d661984201ac3c4`, `en/en_US/kristin/medium/` (`.onnx`, `.onnx.json`, `MODEL_CARD`) | SHA-256 `5849957f…6a18d4` / `5681426d…8b651c` in `modules/mouth/Dockerfile` | Repository MIT; training data public domain (per `MODEL_CARD`) | Mouth's default voice |
| BlueALSA on the host | Ubuntu `jammy` universe `bluez-alsa-utils` 3.0 | apt's signed `Release` files | MIT | Bluetooth A2DP output (section 14). Installed by `bt.sh setup`, which `deploy` runs |
| vosk | PyPI `vosk==0.3.45` (manylinux aarch64 wheel) | SHA-256 `54efb47d…262704` in `samples/voice/requirements.txt` | Apache-2.0 | Speech recognition in the voice sample |
| srt | PyPI `srt==3.5.3` | SHA-256 `48843150…ed0cc0` | MIT | Imported by vosk at load time |
| Vosk speech model | `alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip` | SHA-256 `30f26242…942498` in the sample's `Dockerfile` | Apache-2.0 | Voice sample |
| Ubuntu packages (voice image) | `ubuntu:22.04` + apt `python3`, `python3-paho-mqtt`, `python3-requests`, `python3-tqdm`, `python3-cffi-backend`, `libatomic1`, `ffmpeg` | apt's signed `Release` files | Various OSS (paho EPL-2.0/EDL-1.0, ffmpeg LGPL/GPL) | Voice sample |
| Mosquitto | Docker Hub official `eclipse-mosquitto` | Tag in `compose.yaml` | EPL-2.0 / EDL-1.0 | The device's MQTT broker (section 16) |
| Homepage | `ghcr.io/gethomepage/homepage` ([gethomepage/homepage](https://github.com/gethomepage/homepage)) | Tag in `compose.yaml` | GPL-3.0 | Dashboard home page (section 15) |
| Grafana | Docker Hub `grafana/grafana` ([grafana/grafana](https://github.com/grafana/grafana)) | Tag in `compose.yaml` | AGPL-3.0 | Dashboard charts |
| Prometheus | Docker Hub `prom/prometheus` | Tag in `compose.yaml` | Apache-2.0 | Metrics collection and storage |
| node_exporter | Docker Hub `prom/node-exporter` | Tag in `compose.yaml` | Apache-2.0 | Board metrics |
| cAdvisor | `ghcr.io/google/cadvisor` | Tag in `compose.yaml` | Apache-2.0 | Container metrics |
| go2rtc | Docker Hub `alexxit/go2rtc` ([AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc)) | Tag in `compose.yaml` | MIT | The Eye's video in a browser |

### Authored for this project

`percept-kit.sh`, everything in `scripts/` (including `build-hdmi-dtb.sh` and
`hdmi-dcss-nodes.dtsi`, section 3, and `stack.sh`, section 16), `compose.yaml`, `config.example/`,
`modules/eye/Dockerfile`, `modules/eye/collect-rootfs.sh`, `modules/eye/entrypoint.sh`,
`modules/eye/app/output/*` plus four small hooks (section 7), `modules/ear/*`, `modules/mouth/*`,
`samples/voice/*` (section 14) and `services/*` (sections 15 and 16).

---

## 2. Boot: grub.cfg template and one-shot boots

**Files**: `scripts/grub.cfg.template`, `scripts/install-grub-cfg.sh`. Authored.

### Obstacle: Secure Boot locked to Microsoft's Percept keys

The assessment ([ASSESSMENT.md](ASSESSMENT.md)) read the boot configuration from the running
Mariner:

| Finding | How it was read |
|---|---|
| i.MX HAB is closed: only signed SPL/ATF/UEFI can run | OCOTP fuse Bank 1 Word 3 bit 25 set; the firmware's `fw-version` reports `uboot-secure 1`, `uefi-secure 1` |
| UEFI Secure Boot on, not in setup mode | EFI variables `SecureBoot=1`, `SetupMode=0` |
| `db` trusts only *SantaCruz SecureBoot Production Signing*; PK/KEK are *Test Secure Boot Signing* | Certificates parsed out of the `db`/`PK`/`KEK` EFI variables |
| Boot chain: Project Mu UEFI (fw 2102009006) → shim → GRUB 2.06~rc1 (prefix `/boot/grub2` on the ESP) → `/boot/vmlinuz` on p3, no initrd | ESP contents, the GRUB binary's embedded prefix and module list, `/proc/cmdline` |
| `grub.cfg` is plain text and not signature-checked. GRUB has `load_env`, `save_env`, `devicetree`, `search`, `ext2`, `usb` built in | GRUB image module list; shim only verifies the kernel through `shim_lock` |

So Ubuntu's own shim, GRUB and kernel (signed by the Microsoft UEFI CA) can't boot, and
there's no screen or serial port to reach UEFI setup. The only thing that can change without a
signature is `grub.cfg`.

**Solution**: keep Microsoft's signed shim, GRUB and kernel, and change only `root=` (plus a
device tree) in `grub.cfg`.

### How the template was derived

The stock ESP `boot/grub2/grub.cfg` (194 bytes) was:

```
set timeout=0
set root=(hd0,gpt3)
set efiroot=(hd0,gpt1)
menuentry "Mariner" {
    linux /boot/vmlinuz root=/dev/mmcblk0p3 rw quiet loglevel=3 ima_policy=tcb
    devicetree $efiroot/pe100.dtb
}
```

- **Entry 0 "Mariner"**: the stock `linux` and `devicetree` lines, copied exactly, so Mariner
  boots precisely as before.
- **Entries 1 and 2 "Ubuntu"**: same kernel. The Mariner command line is changed as follows:

  | Parameter | Why |
  |---|---|
  | `root=PARTUUID=<p5>` | Stable across USB/eMMC enumeration order. Filled in by `install-grub-cfg.sh` from `blkid` |
  | `rootwait` | Wait for the root device instead of panicking. An early USB test used `rootdelay=10` |
  | `net.ifnames=0` | Keep the kernel names `wlan0`/`eth0`, which Mariner's `wpa_supplicant-wlan0.conf` and the networkd files use |
  | `systemd.unified_cgroup_hierarchy=0` | cgroup v1. See section 5 |
  | `panic=10` | Reboot after a panic, so a bad trial boot falls back to the default entry |
  | `console=ttymxc0,115200 console=tty0` | Kernel messages on the internal serial port and on HDMI |
  | `ima_policy=tcb` dropped | Mariner's IMA measurement policy has no use on Ubuntu |

  Entry 1 uses `pe100-hdmi.dtb` (section 3); entry 2 keeps Microsoft's `pe100.dtb` as a
  fallback.
- **One-shot boot**: GRUB's standard `next_entry` pattern, hardened after a design review
  pointed out a failure loop. If `save_env` can't persist the cleared `next_entry`, the trial
  entry would be retried on every boot. The template therefore boots `next_entry` **only if**
  clearing it was saved. The `load_env` call whitelists `next_entry`, so nothing else in
  `grubenv` can alter the menu.

### Obstacles

| Obstacle | Fix |
|---|---|
| Mariner has no `grub2-editenv` to write `grubenv` | `install-grub-cfg.sh` writes the file in bash. GRUB's format is exactly 1024 bytes: `# GRUB Environment Block`, `key=value` lines, padded with `#` |
| A broken `grub.cfg` would leave a device with no screen unbootable | The script renders to a temp file, runs `grub-script-check` (from Ubuntu's `grub-common`), then renames into place. The original is kept as `grub.cfg.orig`. Every new entry is first tried as a one-shot |
| `/tmp` is a tmpfs, so a one-shot boot armed by a script run from there doesn't survive the reboot | The kit uploads its scripts to `~/percept-kit/scripts` (persistent) and runs long steps as systemd units (section 11) |

---

## 3. The HDMI device tree, `pe100-hdmi.dtb`

**Files**:
- `scripts/hdmi-dcss-nodes.dtsi` (the exact addition; published);
- `scripts/build-hdmi-dtb.sh` (builds the binary on each device; published);
- `dtb/imx8mq.dtsi`, `dtb/imx8mq-evk.dts`, `dtb/imx8mq-clock.h` (NXP reference; published);
- `dtb/pe100.dtb` (Microsoft's original, copied from the ESP), `dtb/pe100.dts`,
  `dtb/pe100-hdmi.dts` (decompiled sources) and `dtb/pe100-hdmi.dtb`: kept locally, **not
  published** (section 12).

The kit doesn't ship the binary. `install-grub-cfg.sh` runs `build-hdmi-dtb.sh` on the device
against the ESP's own `pe100.dtb` and installs the result (see "Rebuild it yourself" below).

**In one sentence**: `pe100-hdmi.dtb` is Microsoft's `pe100.dtb` plus two nodes, the i.MX8MQ
DCSS display controller and the Cadence HDMI transmitter. Their values were taken from NXP's
reference device tree for the same SoC and the same kernel version. No other byte of the tree
changed.

### Why it was possible

The Percept DK board has an HDMI port, but stock Mariner shows nothing on it. Three findings
showed that only the device tree was missing:

1. **The kernel has the drivers built in.** `/boot/config-5.4.3-26.cm1` has `DRM_IMX_DCSS=y`,
   `DRM_IMX_CDNS_MHDP=y`, `DRM_CDNS_HDMI=y`, `FB=y`, `FRAMEBUFFER_CONSOLE=y`.
2. **The firmware loads the HDMI controller's firmware.** The i.MX8MQ HDMI block needs a
   signed firmware image loaded at boot. eMMC hardware boot partition `mmcblk0boot0` contains
   one (an IVT header at offset 0x8400).
3. **The display nodes were removed from the tree.** Decompiling `pe100.dtb` shows it is
   NXP's EVK tree (`model = "NXP i.MX8MQ EVK"`) without the `hdmi` and `dcss` nodes.
   - Its phandles run 0x01–0x5f with exactly two unused values: 0x43 and 0x45.
   - They sit either side of the irqsteer interrupt controller, phandle 0x44.
   - In NXP's `imx8mq.dtsi` the order is `hdmi` → `irqsteer` → `dcss`.
   - So the tree was compiled with both nodes present and they were deleted afterwards.

### How it was derived

All of this ran on Ubuntu on the device; `dtc` comes from Ubuntu's `device-tree-compiler`
package.

1. **Copy and decompile Microsoft's tree.** The ESP's `pe100.dtb` was copied to
   `dtb/pe100.dtb` (SHA-256 `629ff5e7…08a86e`, same as on the device today):

   ```bash
   dtc -I dtb -O dts -o pe100.dts pe100.dtb
   ```

2. **Fetch NXP's reference** for the kernel's exact BSP version (`5.4.3` → tag
   `imx_5.4.3_2.0.0`) from `github.com/nxp-imx/linux-imx`:
   - `imx8mq.dtsi`: SoC nodes; `hdmi` and `dcss` are present but `disabled`;
   - `imx8mq-evk.dts`: the EVK board enables them and links them with an OF graph;
   - `imx8mq-clock.h`: clock ID numbers.
3. **Merge the SoC and board parts into two plain nodes.** A decompiled tree has no labels or
   macros, so every symbolic value was resolved to a number.

   **`hdmi@32c00000`**:

   | Property | Value | Source |
   |---|---|---|
   | `reg` | `<0x32c00000 0x100000 0x32e40000 0x40000>` | `imx8mq.dtsi` |
   | `interrupts` | `<0 16 4 0 25 4>` | `imx8mq.dtsi` `<GIC_SPI 16 IRQ_TYPE_LEVEL_HIGH>, <GIC_SPI 25 …>` |
   | `interrupt-names` | `"plug_in", "plug_out"` | `imx8mq.dtsi` |
   | `compatible`, `lane-mapping = <0xe4>`, `port@1` | | `imx8mq-evk.dts` `&hdmi` |

   **`display-controller@32e00000`** (`compatible`, `reg` and `interrupts` from `imx8mq.dtsi`;
   `port@0` from the EVK's `&dcss`):

   | Property | Value | Source |
   |---|---|---|
   | `interrupt-parent` | `<0x44>` | `&irqsteer`: phandle 0x44 in `pe100.dts` |
   | `clocks` | `<0x02 248>`, `<0x02 247>`, `<0x02 249>`, `<0x02 254>`, `<0x02 122>`, `<0x02 266>`, `<0x02 267>` | `&clk` = clock-controller@30380000 = phandle 0x02 in `pe100.dts`. IDs from `imx8mq-clock.h`: `DISP_APB_ROOT`, `DISP_AXI_ROOT`, `DISP_RTRM_ROOT`, `VIDEO2_PLL_OUT`, `DISP_DTRC`, `VIDEO2_PLL1_REF_SEL`, `PHY_27MHZ` |
   | `assigned-clocks` | `107`, `109`, `266` | `DISP_AXI`, `DISP_RTRM`, `VIDEO2_PLL1_REF_SEL` |
   | `assigned-clock-parents` | `78`, `78`, `3` | `SYS1_PLL_800M`, `SYS1_PLL_800M`, `CLK_27M` |
   | `assigned-clock-rates` | `800000000`, `400000000` | `imx8mq.dtsi` |

   `status` was set to `"okay"`. The OF-graph endpoints `dcss_out` ↔ `hdmi_in` are written
   as labels, and `dtc` turns them into new phandles.
4. **Insert** the two nodes (saved verbatim as `scripts/hdmi-dcss-nodes.dtsi`) into `pe100.dts`
   just before `interrupt-controller@32e2d000`, which is where `hdmi` sits in NXP's order.
   The result is `dtb/pe100-hdmi.dts`.
5. **Compile**:

   ```bash
   dtc -I dts -O dtb -o pe100-hdmi.dtb pe100-hdmi.dts
   ```

   The output is SHA-256 `5d7b17b976e12b1b53ff9e1e22abff7c5e8b6a46df2bd046c0aaba4e6bf624db`.

### What was tested

- One-shot boots from USB, then eMMC.
- The first DCSS probe defers (`-517`, `EPROBE_DEFER`), then binds.
- DRM reports `HDMI-A-1` connected at 1920x1080. `fb0` carries the Linux console, and a USB
  keyboard logs in.
- Ubuntu runs on this tree day to day; Wi-Fi, the Eye/Ear SoMs and Docker all work with it.
- GRUB entry 2 boots Ubuntu with the untouched `pe100.dtb`, in case the HDMI tree ever
  misbehaves.

**Not added**: HDMI audio (NXP's `sound-hdmi` node, via SAI4) and the GPU/VPU nodes, which
Microsoft's tree already has.

### Rebuild it yourself

`scripts/build-hdmi-dtb.sh` repeats steps 1, 4 and 5. Before inserting the fragment it checks that:
- the anchor node exists exactly once;
- irqsteer is phandle 0x44 and the clock controller is phandle 0x02;
- the display nodes aren't already present.

After compiling, if the input is the tested `pe100.dtb` (`629ff5e7…`), the output must be exactly
`5d7b17b9…`, otherwise the script fails. Any other input only gets a warning that HDMI is
untested on that firmware.

`install-grub-cfg.sh` runs it on the device: from rescue Mariner during `install`, through a
chroot into the new Ubuntu root (which has `device-tree-compiler`); on Ubuntu, directly. On a
system without `dtc`, it keeps an existing `pe100-hdmi.dtb` on the ESP, or stops.

```bash
# on the device (Ubuntu):
findmnt -q /boot/efi || sudo mount /boot/efi
sudo sh ~/percept-kit/scripts/build-hdmi-dtb.sh /boot/efi/pe100.dtb /tmp/out.dtb
```

Results, all **byte-identical** (`5d7b17b9…`):
- Alpine `dtc` 1.7.2;
- Ubuntu 22.04 `dtc` 1.6.1 natively (`commit`);
- the same through the chroot path `install` uses;
- the ESP file, unchanged afterwards.

Feeding it `pe100-hdmi.dtb` instead fails with "input already has display nodes".

---

## 4. Rescue Mariner layout

**Files**: `scripts/rescue-prep.sh`, `scripts/restore-stock-p3.sh`. Authored.

### Obstacle: there is no free space for Ubuntu

| Partition | Size | Stock use |
|---|---|---|
| p1, p2 | 11 MB each | ESP and its A/B twin |
| p3, p4 | 2 GB each | Mariner root A and B |
| p5 | 10.5 GB | Mariner `/var` |

Only p5 is big enough for Ubuntu plus container images. Mariner can't simply be deleted:
- its p3 holds the only bootable kernel;
- it's the only OS left to repair Ubuntu or restore the eMMC.

### How the layout was derived

- **`/var` goes to p4.** p4 (the unused root B slot) has room for Mariner's `/var` once the
  container images are left out: about 60 MB, measured with `du` excluding `lib/docker`,
  `lib/containerd`, the journal and `tmp`.
- **Which services to stop.** Found by listing
  `/etc/systemd/system/multi-user.target.wants/` and reading each unit:

  | Unit | Why it's stopped |
  |---|---|
  | `iotedge*`, `docker-watcher*` | They (re)start IoT Edge and Docker |
  | `preloadedimgs` | Re-imports container images into Docker |
  | `do-client-lite`, `adu-agent` | Delivery Optimization and Device Update. ADU's A/B update writes the B root slot, which is now `/var` |
  | `containerd` | Not needed in rescue mode |
  | `resizefs` | Runs `resize2fs` against p5, which is Ubuntu's partition after the switch |

- **Why the links are "parked" rather than disabled.** Several of Mariner's wants links are
  links to unit files elsewhere. `systemctl disable` would delete those links outright, and
  `enable` can't recreate them identically. Moving the links into
  `/etc/percept-kit/wants-disabled/` and back is exactly reversible.
- **`/etc/fstab`** is saved to `/etc/percept-kit/fstab.orig`. Its p5 → `/var` line is then
  rewritten to p4, and a marker file records the rescue state.

On Mariner, `/home` and `/etc/sudoers.d` live under `/var/custom-configs`. So the copied
`/var` brings your user, SSH keys and Wi-Fi along. The copy is checked by looking for
`rootfsresized` and `custom-configs/home` on p4 before `fstab` is switched.

---

## 5. Ubuntu root filesystem and the kernel gaps

**File**: `scripts/build-ubuntu-rootfs.sh`. Authored. It runs on the device: on rescue Mariner
for eMMC, or on Mariner/Ubuntu for a USB disk.

### Derivation

- **Version**: Ubuntu 22.04. It's the newest LTS whose systemd, Docker and glibc are happy
  with a 5.4 kernel that will never be updated.
- **Base**: the official `ubuntu-base` tarball (it's what Ubuntu's own container images are
  built from), checked against Ubuntu's `SHA256SUMS`. It's completed in a chroot with
  `ubuntu-minimal` plus the packages listed in the script.
- **Copied from Mariner**: see the first table in [section 1](#1-inventory).
- **The user's uid is 10000**, the same uid Mariner's setup wizard gave the account
  (Mariner's `/etc/passwd` has `<user>:10000`, `tss:1000`, `apdk_app:5001`). Files owned by
  you therefore keep the same owner on both OSes. The accessory group `apdk_accessories` keeps
  Mariner's gid 5000, because the Eye image's user 5001:5000 depends on it.

### Obstacles: what the Mariner kernel doesn't have

Each problem was found by booting the USB build and reading `journalctl`, `dockerd` errors or
`/boot/config-5.4.3-26.cm1`.

| Symptom | Cause | Fix in the build script |
|---|---|---|
| `dockerd` fails to start: iptables errors | No `nf_tables` in the kernel; Ubuntu 22.04's `iptables` defaults to the nft backend | `update-alternatives --set iptables /usr/sbin/iptables-legacy` (and `ip6tables`) |
| Docker 28+ fails creating the `raw` table | No `CONFIG_IP_NF_RAW` | `docker.service.d/no-raw.conf`: `DOCKER_INSECURE_NO_IPTABLES_RAW=1` (Docker's documented switch) |
| Containers can't be given device access under cgroup v2 | No `CONFIG_BPF_SYSCALL`; cgroup v2 device control needs BPF | `systemd.unified_cgroup_hierarchy=0` on the kernel command line (cgroup v1) |
| Snaps can't run | No AppArmor in the kernel | `snapd` pinned to priority -1 so nothing pulls it in |
| Boot stalls about 2 minutes at `systemd-networkd-wait-online` | Unplugged Ethernet ports count as "required for online" | `RequiredForOnline=no` in `20-wired.network` |
| Docker images took about 3x their size on disk | The containerd image store keeps the compressed layer blobs as well as the unpacked snapshots | `daemon.json`: `"containerd-snapshotter": false` (classic overlay2) and `"log-driver": "local"` (size-capped logs) |
| `sfdisk` rejected the partition script | Mariner's `sfdisk` doesn't support `name=` | Partition spec reduced to `,,L` |
| Long builds died when SSH disconnected | `systemd-logind` kills processes of a closed SSH session | Long jobs run as `systemd-run --unit=pk-<name>` (section 11) |

The watchdog setting (`RuntimeWatchdogSec=30`) is a standard systemd option. It makes a hung
trial boot reboot onto the default entry.

---

## 6. Ear audio binding fix

**Files**: `scripts/host/81-percept-ear.rules`, `scripts/host/percept-ear-audio.service`.
Authored.

- **Symptom**: on Ubuntu the Ear SoM (USB `045e:0671`, *Azure Ear SoM*) is listed by `lsusb`,
  but often has no ALSA card after boot. `modprobe -r snd_usb_audio; modprobe snd_usb_audio`
  makes the card appear every time.
- **Cause**: the SoM's controller re-enumerates its audio function late in boot.
  `snd-usb-audio` has already probed and misses the second appearance.
- **Fix**:
  - A udev rule on the `usb_device` add event for `045e:0671` asks systemd to start a oneshot
    service. A udev `RUN+=` can't be used, because it can't run long or sleep.
  - The service waits 5 s and checks `/proc/asound/cards` for *Azure Ear SoM*. Only if it's
    missing does it reload the driver.
  - The card's capabilities (5 channels, S32_LE, 16 kHz; channel 5 silent) were read with
    `arecord --dump-hw-params`.

---

## 7. Eye app source, `modules/eye/app`

### Where the code came from

Almost all of it (about 10,000 lines of C++ in 74 files, plus 65 MB of model data) is
**Microsoft's open-source Azure Percept Eye module**. It's the same program that runs inside
Microsoft's `azureeyemodule` container.

| | |
|---|---|
| Repository | [microsoft/azure-percept-advanced-development](https://github.com/microsoft/azure-percept-advanced-development) (archived read-only by Microsoft) |
| Path | `azureeyemodule/app` |
| Commit | `b6782ded19c342e53a02b958fe38053d43e74507` (HEAD of `main`) |
| Last change to the app | "Add no-auth patch (#83)", 2023-01-20. This matches the `2301-…-noauth` module image the device shipped with |
| Licence | MIT. Copied unchanged to `modules/eye/LICENSE.txt`, with the third-party `NOTICE.txt` |

It was fetched with a sparse clone of just that folder:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/microsoft/azure-percept-advanced-development.git apad
git -C apad sparse-checkout set azureeyemodule/app
git -C apad rev-parse HEAD     # b6782ded19c342e53a02b958fe38053d43e74507
cp -r apad/azureeyemodule/app modules/eye/app
cp apad/LICENSE.txt apad/NOTICE.txt modules/eye/
```

**Why the GitHub source and not Microsoft's image?** The `/app` folder inside the MCR image
holds only part of the source: no `main.cpp`, no `CMakeLists.txt`. The archived repository is
the complete, licensed source of that binary.

### What's in it

| Folder | Contents (upstream) |
|---|---|
| `main.cpp` | Command-line options, camera/model start-up, the main loop |
| `device/` | Eye SoM camera/Myriad X device control |
| `model/` | One class per network type: SSD, YOLO, classification, OpenPose, OCR, U-Net, Faster R-CNN, ONNX SSD… parsing results into JSON and drawing overlays |
| `kernels/` | OpenCV G-API kernels that run on the Myriad X |
| `streaming/` | RTSP server (GStreamer) for `/raw`, `/result`, `/h264raw` |
| `iot/` | Azure IoT module client: twin settings and telemetry. Kept, but with no IoT Edge it only logs connection errors |
| `secure_ai/` | Azure Percept encrypted-model support (needs Azure; unused) |
| `ocr/`, `openpose/`, `imgcapture/`, `util/` | Helpers for those models, snapshot capture, logging |
| `data/` | `ssd_mobilenet_v2_coco.blob` (default model, compiled for the Myriad X), `labels.txt`, `ssd_mobilenet_v1_10.onnx` |

### Our changes to it

5 files, +946 / −1 lines. The upstream files keep their CRLF line endings.

| File | Change |
|---|---|
| `output/output.hpp`, `output/output.cpp` | **New**: the inference outputs (section 9) and the Prometheus metrics with the Myriad X telemetry (section 15) |
| `main.cpp` | `#include "output/output.hpp"`; `output::start();` just before the IoT client starts (one comment line reworded) |
| `iot/iot_interface.cpp` | `#include "../output/output.hpp"`; in `send_message()`, `output::publish(iotmsg.msg_body);` so every message bound for IoT Edge also goes to the local outputs |
| `CMakeLists.txt` | `THIRD_PARTY_DIR`; `find_library` for static Paho; `output/` sources; third-party headers as `SYSTEM` (so upstream's `-Werror` doesn't apply to them); link `paho-mqtt3cs`, `dl`, `rt`, `anl` |

Everything else is byte-identical to upstream. Check with the commands in
[section 13](#13-verify-it-yourself).

---

## 8. Eye image: `Dockerfile` and `collect-rootfs.sh`

**Files**: `modules/eye/Dockerfile`, `modules/eye/collect-rootfs.sh`. Authored.

### Obstacle: the Myriad X software stack only exists inside Microsoft's image

The app needs several components that aren't available as packages for this platform, in the
versions the app was built against:
- Intel/Microsoft's OpenCV G-API build with the Myriad X backend (`/eyesom`);
- `mxIf` and the Myriad firmware `mx.mvcmd`;
- OpenVINO 2021 (`myriad_compile`);
- onnxruntime;
- a matching GStreamer.

Upstream's `Dockerfile.arm64v8` builds `FROM azureeyebase:latest-arm64v8`, the base image
Microsoft's own pipeline used. The **published** `mcr.microsoft.com/azureedgedevices/azureeyemodule:2301-1-noauth` image
contains that same toolchain and stack. It's what the device ran, and its manifest is still
served by MCR.

### How the Dockerfile was derived

- **Build stage** `FROM` the MCR image. The cmake flags are copied from upstream's
  `Dockerfile.arm64v8`:

  ```
  -DOpenCV_DIR=/eyesom/build/install/lib64/cmake/opencv4
  -DmxIf_DIR=/eyesom/mxif/install/share/mxIf
  -DCMAKE_PREFIX_PATH=/onnxruntime/install/lib64
  ```

  Added flags:
  - `-DAUTH_FIX=1`: upstream's no-auth patch, so the app never waits for Azure device
    authentication;
  - `-DCMAKE_BUILD_TYPE=Release`;
  - `make -j2`: the device has 2.7 GB of RAM.

  A preceding `RUN` builds the two output libraries (section 9).
- **Final stage** `FROM scratch`. `collect-rootfs.sh` copies only what the binary needs:
  - the binary itself and `app/data`;
  - every library in the `ldd` closure of every ELF file copied;
  - the Myriad firmware;
  - the OpenVINO libraries, `plugins.xml` and `myriad_compile` (for `.xml` models at run time);
  - the GStreamer plugins;
  - glibc's NSS modules (DNS for model URLs);
  - the CA bundle;
  - the shell tools the app calls through `system()`: `sh`, `rm`, `mkdir`, `unzip`, `wget`…
  - `bash` and `entrypoint.sh`, which turns the `EYE_MODEL`, `EYE_PARSER`, `EYE_LABELS`,
    `EYE_RESOLUTION`, `EYE_FPS` and `EYE_TIME_ALIGN` settings into the app's options;
  - minimal `passwd`/`group`/`ld.so.conf` files.

  Everything is stripped.
- **User 5001:5000** is the same `apdk_app` / `apdk_accessories` identity Microsoft's image
  used, matching the host udev rule's group.

| Image | Size on disk |
|---|---|
| Microsoft `azureeyemodule:2301-1-noauth` | 2.6 GB as shipped (Debug build, duplicated layers); 1.23 GB in Docker's overlay2 store |
| `percept/eye` | About 240 MB, with the output libraries and their licence texts |

### Obstacles

| Obstacle | Fix |
|---|---|
| GStreamer's plugin directory is compiled in as `/usr/lib64/gstreamer-1.0`, and in the base image `/usr/lib64` is a symlink: without it, `no element "appsrc"` and no video | The collector recreates the base's `/bin`, `/sbin`, `/lib`, `/lib64` and `/usr/lib64` symlinks first |
| Some GStreamer plugins pull in libraries absent from the base | Plugins whose `ldd` shows `not found` are skipped (they never loaded in Microsoft's image either) |
| The Myriad X changes USB device number when its firmware loads, so passing one `/dev/bus/usb/…` device breaks | Bind-mount `/dev/bus/usb` and allow USB character devices with the cgroup rule `c 189:* rmw` |
| Licences | The base image's binaries are Microsoft's and aren't covered by the MIT source licence, so they're never extracted into the repository. They're pulled from MCR, or loaded from the device's own backup, at build time on your device, and the resulting image is never pushed. See section 12 |
| MCR could stop serving the base image (Percept was retired in March 2023) | Every stock DK has the exact image in its IoT Edge cache (the stock deployment runs `azureeyemodule:2301-1-noauth`). `backup` (and `eye-base save`) saves the device's own copy as `eye-base.tar.zst`, hashed in `SHA256SUMS`. Before the Eye is built, the kit uses the image if Docker has it, else verifies and `docker load`s it from this device's newest backup, else pulls it. With `mcr.microsoft.com` blocked in `/etc/hosts` and the image removed, the backup copy loaded with an identical image ID (`sha256:9b954af2…`). The image stays in Docker after the build, so later builds start at once; `eye-base remove` frees its 1.2 GB |

---

## 9. Eye inference outputs, `app/output`

**Files**: `modules/eye/app/output/output.hpp`, `output.cpp`; the Dockerfile's third-party `RUN`;
the Eye's MQTT settings in `compose.yaml`. Authored, using two libraries.

### Why

Upstream sends every inference result to IoT Edge (`iot::msgs::send_message`). Without IoT Edge
those results were lost. Hooking the same function means:
- every message Microsoft's app produces is published locally, in exactly the format IoT Hub
  would have received: `{"NEURAL_NETWORK": [...]}`;
- no model parser had to change.

The HTTP output's `GET /metrics` is described in section 15.

### How the libraries were chosen

| Considered | Outcome |
|---|---|
| Our own MQTT/HTTP code | Rejected in favour of maintained libraries |
| `azure_umqtt_c` (already linked from the base image) | Rejected: its OpenSSL TLS layer sends no SNI and does no hostname check, so `mqtts://` to cloud brokers would be unsafe or fail |
| Install Paho from Mariner packages | Impossible: the CBL-Mariner 1.0 repositories now return HTTP 403 |
| **Eclipse Paho MQTT C v1.3.16**, built from source in the build stage, static, with the base image's OpenSSL 1.1.1k | Chosen. Mature, EPL-2.0/EDL-1.0. Supports `mqtt://`, `mqtts://`, `ws://`, `wss://`, SNI and hostname verification. Pinned by tag and SHA-256 |
| **cpp-httplib v0.65.0** (single header, C++11) | Chosen for HTTP. MIT. Used without TLS: its TLS mode needs OpenSSL 3, which the base doesn't have. Pinned by SHA-256 |

The libraries' licence texts are copied into the image under `/usr/share/licenses/`.

### Obstacles

| Obstacle | Fix |
|---|---|
| Upstream compiles with `-Wall -Wextra -Werror` | Third-party headers included as `SYSTEM` |
| Static Paho needs extra system libraries | Link `dl`, `rt`, `anl` (plus the `ssl`/`crypto` upstream already links) |
| A slow HTTP or MQTT consumer must never stall inference | `publish()` only copies into memory. MQTT has its own thread and a 256-message queue that drops the oldest. HTTP streams read from a 64-message ring and skip ahead if they fall behind. At most 16 concurrent streams (503 after that) |
| Broker outages | Reconnect with backoff 1 → 60 s; inference and the other outputs keep running |
| Passing secrets without baking them into the image | `config/files/` on the device is readable only by the device user and the modules' group (5000), and is mounted read-only at `/config/files`. `MQTT_PASSWORD_FILE` and the certificate and key settings point there |

### What was tested (on the device)

| Test | Result |
|---|---|
| Build | Clean with `-Werror`; image 238 MB |
| HTTP `/`, `/latest` | Help text; 200 with the newest `{"NEURAL_NETWORK": …}` message (person detections) |
| HTTP `/inferences` (`curl -N`) | One JSON message per line, about 12 per second |
| HTTP `/events` | `data: {…}` Server-Sent Events |
| MQTT `mqtt://`, username + `EYE_MQTT_PASSWORD_FILE`, `EYE_MQTT_RETAIN=1`, `EYE_STDOUT=1` | Connected as `eye-<container id>`; `mosquitto_sub` received a message about once per second (`EYE_OUTPUT_INTERVAL_MS=1000`). The same JSON appeared on stdout. After `eye` stopped, the retained message was still delivered |
| MQTT `mqtts://` + `EYE_MQTT_CA_FILE`, topic `lab/eye` | TLS 1.3 (`TLS_AES_256_GCM_SHA384`); the server certificate's name (`host.docker.internal`) was checked; messages arrived on `lab/eye` |
| MQTT `mqtts://` with the wrong CA | Refused: `certificate verify failed`. No messages were published; retried with backoff |
| MQTT with the wrong password | Refused by the broker: `not authorized`. No messages were published; retried with backoff |
| MQTT broker unreachable (port 1999) + HTTP | `cannot connect … retrying in 1 s`, backing off; the app kept running and the HTTP output started normally |

The MQTT tests ran against the official `eclipse-mosquitto:2` container (Mosquitto 2.1.2) on the
device, with `allow_anonymous false`, a password file, and plain (1883) and TLS (8883)
listeners using a throw-away test CA. The broker container, its image and the test certificates
were deleted afterwards.

---

## 10. Ear module

**Files**: `modules/ear/ear.py`, `modules/ear/Dockerfile`. Authored; nothing from Microsoft.

- **Why not Microsoft's module.** Microsoft's `azureearspeechclientmodule` is a Speech SDK
  client. Its keyword model is downloaded from Azure, and that download fails on a standalone
  device.
- **What the hardware is.** Read with `lsusb -v` and `arecord --dump-hw-params`: the Ear SoM
  is a standard USB Audio Class device, a 4-mic linear array plus line-out. It needs no
  Microsoft software at all.
- **What the module is.** A Python standard-library HTTP service (`ear.py`) around ALSA's
  `arecord`, on `alpine:3.24` with `python3` and `alsa-utils`. It runs as uid 5002 with
  `--device /dev/snd` and the host's `audio` group. Image size is about 60 MB.
- **Input only.** The mouth module (section 14) owns the speaker, whether it's the line-out or
  Bluetooth.
- **Buttons and mix.** ear also reports the Ear's Mute and PTT/PTS buttons from its HID input
  device, and offers a 4-mic average (`ch=mix`). See section 14, Obstacles, and
  [ear.md](ear.md#whats-on-the-board-and-what-linux-can-use).

---

## 11. PC kit and operational obstacles

**Files**: `percept-kit.sh`, `scripts/kit/*.sh`. Authored. The obstacles below shaped how the
kit talks to the device.

| Obstacle | How it showed up | Diagnosis | What the kit does |
|---|---|---|---|
| **Mariner's SSH rate limit** | SSH timed out for minutes at a time; looked like Wi-Fi dropping | `/proc/net/xt_recent/DEFAULT` listed the PC's IP. `iptables-save` showed a `mangle` PREROUTING rule: 6 new connections in 60 s → `SSHBRUTEFORCE` → drop | `scripts/kit/ssh-exempt.sh` inserts a runtime-only `ACCEPT` for the PC's IP ahead of that rule each time the kit checks state. Polling after a reboot is spaced 15 s apart until the exemption is back |
| Windows OpenSSH can't reuse connections | Each kit step is a new connection, which counts toward the limit | No `ControlMaster` on Windows | Batching plus the exemption above |
| Jobs died when SSH dropped | Ubuntu build killed mid-way | `systemd-logind` kills a closed session's processes | Long jobs run as `systemd-run --unit=pk-<name>`, logging to `/var/tmp/pk-<name>.log` + `.rc`. The kit re-attaches and tails the log |
| `/tmp` emptied by reboots | A one-shot boot armed from `/tmp` is lost | `/tmp` is a tmpfs | Scripts live in `~/percept-kit/scripts` |
| Backup images unreadable | `dump.sh` output owned by root, mode 600 | Mariner's default `umask 077` | `umask 022` in the dump step, reads through `sudo` |
| No `zstd` on the PC | Couldn't compress backups locally | | Images are compressed on the device and streamed. SHA-256 is computed on both ends and compared |
| Windows line endings | `set: illegal option -` errors on the device | CRLF in shell scripts | `upload_kit` strips CR from every text file (not `.dtb`), and `deploy` from the text files it receives; `.gitattributes` keeps scripts LF |
| Passwords passed through GUI prompts arrive masked, and some shells give a key made with `-N ""` a literal `""` passphrase | Key login fails | | `connect` generates a passphrase-less ed25519 key itself and installs it with one password login |
| Mariner has no `grub2-editenv`, `xxd`, `strings` or `sgdisk` | | | `grubenv` written in bash; GPT saved with `dd` |
| Ubuntu may get a different DHCP address than Mariner | Device "disappears" after `install` | networkd sends a DUID-based client ID | Documented in [setup.md](setup.md#if-the-ip-address-changes) |
| Commands run for hours (`install`), long enough to update the repository meanwhile | `syntax error near unexpected token` once the command finished, after the script had changed | bash reads a script as it runs it, at a byte offset, so it read on into the changed text | The last line, `{ main "$@"; exit; }`, is parsed whole before it runs |

---

## 12. Licences and what must not be shared

| Component | Licence | OK to share |
|---|---|---|
| Kit scripts, docs, `compose.yaml`, `config.example/`, `output/`, Ear and mouth modules, `samples/`, `services/` | Written for this project | Yes |
| Mosquitto, Homepage, Grafana, Prometheus, node_exporter, cAdvisor, go2rtc (the pulled images) | EPL-2.0/EDL-1.0, GPL-3.0, AGPL-3.0, Apache-2.0, Apache-2.0, Apache-2.0, MIT | Not committed or rebuilt: the device pulls the projects' own images, unmodified. The kit only adds its configuration files, mounted read-only |
| vosk, srt, the Vosk model, ffmpeg, espeak-ng, piper-tts, onnxruntime, numpy, the Piper voice (in locally built images) | Apache-2.0, MIT, Apache-2.0, LGPL/GPL, GPL-3.0, GPL-3.0-or-later, MIT, BSD-3-Clause, MIT (public-domain data) | Not committed; downloaded at build time. The mouth and voice images contain no Microsoft binaries, but if you redistribute them, the GPL/LGPL source and notice obligations of espeak-ng, piper-tts and ffmpeg apply. mouth.py only calls piper-tts's public API at run time; it isn't distributed with it |
| `modules/eye/app` (upstream part) | MIT, © Microsoft. Keep `LICENSE.txt` and `NOTICE.txt` with it | Yes, with the licence files |
| Eclipse Paho MQTT C (in the image) | EPL-2.0 / EDL-1.0; texts in the image's `/usr/share/licenses/paho.mqtt.c` | Yes, with the licence texts |
| cpp-httplib (in the image) | MIT; text in `/usr/share/licenses/cpp-httplib` | Yes, with the licence text |
| NXP device-tree sources in `dtb/` | `(GPL-2.0+ OR MIT)`, `(GPL-2.0 OR MIT)`, `GPL-2.0` (headers in the files) | Yes, with their SPDX headers |
| `scripts/hdmi-dcss-nodes.dtsi` | NXP's node definitions, `(GPL-2.0+ OR MIT)` (SPDX header in the file); the phandle numbers are facts read from `pe100.dtb` | Yes, with its SPDX header |
| `dtb/pe100.dtb`, and `pe100-hdmi.dtb` / `pe100-hdmi.dts` derived from it | Microsoft's board file. Its source is an NXP dual-licensed tree, but Microsoft published no licence for the file itself | **Keep local** (gitignored). Each device builds its own from its ESP |
| The `percept/eye` image | Contains binaries from Microsoft's MCR image (Myriad stack, OpenVINO, `mx.mvcmd`, libraries) | **Never push or redistribute.** Build it on your own device |
| The MCR base image and `eye-base.tar.zst` | Microsoft's image; the backup file is your device's own copy | **Never push or redistribute.** Keep in your private backup |
| Mariner kernel, modules, firmware (copied into Ubuntu); `backups/` | Microsoft/vendor binaries; backups also hold your Wi-Fi password, SSH keys and password hash | **Keep local and private** |

---

## 13. Verify it yourself

```bash
# 1. The ESP's dtbs are the tested ones (on the device, Ubuntu):
findmnt -q /boot/efi || sudo mount /boot/efi
sudo sha256sum /boot/efi/pe100.dtb /boot/efi/pe100-hdmi.dtb
#    629ff5e7c149451aec0e007e0418152f24a872a95bc673a85a007fd40d08a86e  /boot/efi/pe100.dtb
#    5d7b17b976e12b1b53ff9e1e22abff7c5e8b6a46df2bd046c0aaba4e6bf624db  /boot/efi/pe100-hdmi.dtb

# 2. pe100-hdmi.dtb = pe100.dtb + hdmi-dcss-nodes.dtsi (section 3):
sudo sh ~/percept-kit/scripts/build-hdmi-dtb.sh /boot/efi/pe100.dtb /tmp/rebuilt.dtb && sudo cmp /tmp/rebuilt.dtb /boot/efi/pe100-hdmi.dtb && echo identical

# 3. The Eye app differs from upstream only in the 5 files of section 7:
git clone -q --depth 1 --filter=blob:none --sparse https://github.com/microsoft/azure-percept-advanced-development.git /tmp/apad
git -C /tmp/apad sparse-checkout set azureeyemodule/app
git diff --no-index --stat /tmp/apad/azureeyemodule/app modules/eye/app

# 4. Third-party downloads: the Dockerfile's `sha256sum -c` fails the build on any mismatch.
```

---

## 14. Mouth module, Bluetooth and the voice sample

**Files**: `modules/mouth/` (`mouth.py`, `bt.sh`, `Dockerfile`, `requirements.txt`), `samples/voice/` (`voice.py`,
`Dockerfile`, `requirements.txt`), and the `bt` command in `percept-kit.sh`. All authored; nothing
from Microsoft. Usage is in [mouth.md](mouth.md) and [samples/voice/README.md](samples/voice/README.md).

### Design choices

- **Separation of duties.** The Ear is input only, the mouth owns every sound the device makes,
  and the sample holds only logic. A sample never touches ALSA or Bluetooth itself. Because
  mouth's playback requests return only when the sound has finished, the sample can mute its
  recogniser while the device talks (the echo guard).
- **Bluetooth audio without a desktop sound server.** BlueALSA (`bluez-alsa-utils`, Ubuntu
  universe) is a small A2DP daemon that exposes speakers as ALSA devices. PulseAudio and
  PipeWire would also work, but they're per-user session daemons and much larger. The mouth
  image is Ubuntu 22.04 rather than Alpine so that its BlueALSA ALSA plugin speaks the same
  D-Bus API (3.0) as the host's daemon; 4.x changed it.
- **What the hardware offers.** `lsusb` shows the Realtek RTL8822CU Wi-Fi/Bluetooth combo
  (`13d3:3548`), and Mariner's firmware (`rtl8822cu_fw.bin`) is in the Ubuntu root. The kernel
  `config` has `CONFIG_BT=y`, `BT_BREDR`, `BT_RFCOMM`, and `BT_HCIBTUSB`/`BT_RTL` as modules, so
  Classic A2DP is possible. There's no `/dev/rfkill`, which is harmless.
- **Offline speech.**
  - Vosk (Apache-2.0) runs on the Cortex-A53 with a 40 MB English model.
  - Recognition uses a fixed grammar of the command phrases plus `[unk]`, which is fast,
    accurate and rejects conversation.
  - Rejected options:
    - openWakeWord's pre-trained models, CC BY-NC-SA (non-commercial);
    - Porcupine, which needs a proprietary access key;
    - Azure Speech, which needs the cloud.
  - Text-to-speech is **Piper** ("Neural voice" below), with espeak-ng from Ubuntu as an instant
    but mechanical-sounding fallback.
- **Eye input over MQTT.** The sample reuses the Eye's MQTT output (section 9) through the
  stack's own broker, the official `eclipse-mosquitto` image, instead of new broker code. The
  broker publishes no host port, so only containers on the Docker network `percept` reach it.
- **Supply chain.**
  - pip installs with `--require-hashes --no-deps`. Only vosk and srt come from PyPI; vosk's
    other dependencies are Ubuntu packages.
  - The model zip is checked with `sha256sum -c`.
  - The Dockerfile takes `ARG PIP_INDEX_URL`, so a mirror can be used, and the kit passes it
    from `config/percept.env`.
  - Nothing third-party is committed.

### Neural voice (Piper)

- **Choice.** Piper is the offline neural TTS that runs well on small ARM CPUs (VITS models via
  ONNX Runtime, no GPU). Cloud TTS was out (standalone), and larger models (Coqui XTTS, Bark)
  are far too slow for a Cortex-A53 with 2.7 GB of RAM.
- **Package.** `piper-tts` 1.8.0 from `OHF-Voice/piper1-gpl` is GPL-3.0-or-later, the same
  class as espeak-ng, which the image already had. Only three wheels are needed with
  `--no-deps`: piper-tts, onnxruntime and numpy. piper-tts also declares `pathvalidate`, which only
  its command-line tool and training code import (mouth uses the Python API), and onnxruntime's other dependencies (protobuf, sympy, …) aren't needed
  for CPU inference of a Piper model; both were verified by running it.
- **Voices, by licence.** The `rhasspy/piper-voices` repository is MIT, but each voice is trained
  on its own dataset, given in its `MODEL_CARD`. Usable: ljspeech, kristin, norman, john (public
  domain), joe, kathleen (CC0), libritts_r (CC BY 4.0). Excluded: lessac (Blizzard Challenge
  licence), amy (unclear), ryan and hfc_female/hfc_male (CC BY-NC-SA, non-commercial).
- **Selection.** The usable US voices were rendered on the device and compared by ear on the
  Bluetooth speaker, against espeak-ng; `en_US-kristin-medium` sounded the most natural.
- **Latency.** Piper's CLI loads the voice on every call (14–22 s). mouth loads it once, in a
  background thread at start-up (~13 s), then synthesises in-process at ~1.2–1.6× real time.
  Rendered phrases are cached (`mouth-cache` volume, keyed by SHA-256 of voice, speed and text,
  least-recently-used pruning), and the voice sample pre-renders its fixed replies.

### Obstacles

| Obstacle | How it showed up | Diagnosis | Fix |
|---|---|---|---|
| **Kernel crash on Bluetooth scan** | A `bt scan` froze the device, which then rebooted | Kernel oops in `__hci_update_background_scan` → `kfree`, in the Mariner 5.4.3 kernel's Bluetooth LE background-scan worker, when discovery stops. The kernel can't be patched (section 1) | `bt.sh setup` sets `ControllerMode = bredr` in `/etc/bluetooth/main.conf`, so the LE code never runs. `btmgmt info` shows `br/edr` only, and repeated 10 s scans cause no oops |
| **Container can't reach BlueALSA** | Bluetooth playback from the container failed with a D-Bus access error | `/etc/dbus-1/system.d/bluealsa.conf` only lets root and group `audio` call `org.bluealsa`. The system bus resolves a caller's groups from the **host's** user database by uid, so the container's `--group-add` doesn't count. (BlueZ's own policy allows any local user to call `org.bluez`.) | `bt.sh setup` creates host account `percept-mouth`, uid 5003, in group `audio`. The container runs as uid 5003 with the host's `/run/dbus/system_bus_socket` mounted |
| **The plugin already defines `pcm.bluealsa`** | A second definition fails: `default is not a string` | `libasound2-plugin-bluez` ships `/etc/alsa/conf.d/20-bluealsa.conf`, which defines `pcm.bluealsa` with `DEV`/`PROFILE`/`SRV` arguments | mouth defines nothing itself and plays to `bluealsa:DEV=<MAC>,PROFILE=a2dp` from the shipped file |
| **Pairing not remembered** | A pairing wasn't kept | With the adapter `Pairable: no`, BlueZ 5.64 pairs without bonding, so `/var/lib/bluetooth/<adapter>/<MAC>/info` has no `[LinkKey]` | `bt.sh pair` runs `bluetoothctl pairable on` first, so the link key is stored |
| **Second USB controller missing at boot** | 1 of 2 test reboots: no `hci0`, no Ear sound card | `dmesg`: `imx-pgc-domain.3: failed to command PGC`. The USB OTG2 power domain didn't switch on, so `38200000.usb` stayed in deferred probe (`/sys/kernel/debug/devices_deferred`). The Ear hub and the Bluetooth chip are behind it. Re-binding from sysfs doesn't help | Intermittent hardware/kernel issue, not fixable from user space. Documented: reboot ([setup.md](setup.md#the-ear-or-bluetooth-is-missing-after-a-reboot)) |
| **`column` not installed** | `column: command not found` | The minimal Ubuntu root has no `bsdextrautils` | `bt.sh --table` aligns columns with `awk` |
| **vosk's hidden dependency** | `import vosk` failed: no module `srt` | vosk imports `srt` at load time; with `--no-deps` it wasn't installed | Added `srt` 3.5.3, hash-pinned |
| **`libvosk.so` won't load** | `OSError: libatomic.so.1` | The aarch64 wheel links `libatomic`, which `ubuntu:22.04` doesn't include | `libatomic1` added to the apt list |
| **User name taken** | `useradd: group voice exists` | Ubuntu images have a `voice` group (gid 22) | The container user is `percept-voice` (uid 5004) |
| **Vocabulary check** | The small model has no `graph/words.txt` to check phrases against | It's a lookahead model (`Gr.fst`/`HCLr.fst`) | `voice.py` calls `model.vosk_model_find_word(word)`, which returns −1 for unknown words, and skips those phrases |
| **The Eye's RTSP is UDP-only** | ffmpeg with `-rtsp_transport tcp`: `461 Unsupported transport` | The Eye's RTSP server only offers UDP for `/result` | ffmpeg uses its default transport (UDP) |
| **No onnxruntime for the latest release** | pip found no matching wheel | Ubuntu 22.04's Python is 3.10; onnxruntime 1.23.2 is the last release with a cp310 aarch64 wheel | Pinned `onnxruntime==1.23.2` |
| **numpy too old** | Ubuntu's `python3-numpy` doesn't satisfy onnxruntime | onnxruntime 1.23.2 declares `numpy>=1.21.6`; Ubuntu 22.04 has 1.21.5 | numpy 2.2.6 (cp310 aarch64) from PyPI, hash-pinned, instead of the Ubuntu package |
| **Slow calls at start-up** | Requests right after a restart could get no connection | `bluealsa-aplay -L` is slow while Piper loads on every core | The server binds its port first and logs the sink from a thread; `bluealsa-aplay` gets 15 s, since synthesis also slows it |
| **Replies nearly silent after a reboot** | The sample heard and answered, but the speaker was barely audible | BlueALSA's PCM `Volume` property is 0x2020 (32/127 per channel, software volume) after a reboot | mouth sets the speaker's mixer control to `MOUTH_BT_VOLUME` (100%) whenever a speaker connects. Set to 25%, disconnected and reconnected, it's back to 100% |
| **Short sounds lost on Bluetooth** | A 0.15 s beep after a pause wasn't heard; the chime and speech were | The A2DP stream and the speaker's amplifier wake up on the first audio; 0.3 s of lead-in isn't enough after idle | 1 s of lead-in after 4 s without sound (`MOUTH_BT_IDLE_LEAD`); both test beeps are heard |
| **Mics silent, nothing recognised** | Every mic read −120 dBFS (pure zeros) while `arecord` ran normally | The Ear's Mute button: the SoM firmware zeroes the capture itself. Its LEDs don't show it under Ubuntu (they were driven by Microsoft's speech module) | ear reads the button state (below) and reports `mute`; the voice sample announces mute changes. Resetting the SoM's USB port also clears it |
| **How to read the Ear's buttons** | Mute and PTT/PTS are documented by Microsoft only for its retired speech module | `lsusb -t`: the Ear's interface 4 is HID, bound to `usbhid`, with one 8-bit input report (descriptor `05 01 09 00 a1 01 85 01 … 75 08 81 02`). Linux maps it to `ABS_MISC` on `/dev/input/…Azure_Ear_SoM_USB-event-if04`. Pressing each button while recording events showed bit 0 = Mute state, bit 1 = toggled per PTT press. No `hidraw` in the kernel | ear reads the input device (read-only mount of `/dev/input`, cgroup rule for major 13, host `input` group). The Ear's other device, "Azure Ear SoM Controller" (`045e:0673`, CDC-ACM `/dev/ttyACM1`), is how Microsoft drove the LEDs; its protocol isn't documented, so it isn't used |

### What was tested (on the device)

- **Mouth, line-out**: `/tone`, `/say`, `/play` and `/say?render=1` work, and the Ear kept
  streaming during playback.
- **Mouth, Bluetooth**: a Harman Kardon Invoke speaker paired (bonded), and A2DP connected
  with SBC at 48 kHz. Test phrases and voice replies were heard on the speaker.
- **Fallback**: with the speaker off, output fell back to the line-out (heard). With it on
  again, it reconnected by itself.
- **Reboot**: `percept-bt-connect.service` reconnected the speaker 2 s after start, and the Piper
  voice and the voice sample's pre-rendered replies were back. The whole stack's reboot test is in
  section 16.
- **Recognition, synthetic**: espeak-ng WAVs rendered by mouth were fed to `voice --test`.
  - Six commands were recognised with confidence 1.0.
  - The wake word alone armed the listener.
  - Commands without the wake word, and ordinary sentences, were ignored.
- **Recognition, live**: with the wake word `percept` (it's in the model's vocabulary), live
  commands and the "didn't understand" reply worked, and other words before a command ("computer,
  …") were ignored. Peaks were −35 to −6 dBFS with 12 dB gain.
- **Piper**: the kristin voice loads in ~13 s, in the background (`/status` answers at once). A
  new sentence takes ~2–5 s to synthesise; a cached one returns in ~0.01 s, also after the
  container is recreated. `?voice=espeak:en-us` and `?voice=en-us` still use espeak-ng, and an
  uninstalled Piper voice returns 400. Piper renderings of the commands are recognised by
  `voice --test`. The voice sample pre-renders its 16 fixed replies at start-up. On the device, the
  voice sounds human and replies come quickly enough for conversation.
- **MQTT**: eye → mosquitto → voice detections, and the `percept/voice/commands` and
  `percept/voice/events` topics, checked with `mosquitto_sub`. The Eye's Paho client reconnects by
  itself when mosquitto is recreated.
- **Snapshots**: annotated 816×616 JPEGs.
- **Presence**: synthetic inferences checked arrival after 3 frames, departure, the alert
  cooldown, and alert = tone + snapshot + speech.
- **Resources**: voice ~160 MB RAM and ~30% of one core; mosquitto ~3 MB; mouth ~290 MB with the
  Piper voice loaded. About 1.5 GB of RAM is still available with everything running. Images:
  voice 513 MB, mouth 366 MB.
- **Ear buttons and mic mix**: Mute → "Microphones off." / "Microphones on."; PTT while muted
  → "My microphones are off…"; PTT then "what do you see" (no wake word) → "I see one person."
  (confidence 1.0). With the 4-mic mix (`ch=mix`) as input, the phrase variants were recognised in
  a loopback test with confidence 1.0, and chatter was rejected.

---

## 15. Dashboard and Myriad X telemetry

**Files**: `services/dashboard/` (`prometheus/`, `grafana/`, `homepage/`, `go2rtc/`), the six
dashboard services in `compose.yaml`, `dashboard.md`, and the metrics in
`modules/eye/app/output/output.cpp` (class `Metrics`, `GET /metrics`). All authored. The six
container images are the upstream projects' own, pulled by `deploy`. Usage is in
[dashboard.md](dashboard.md) and [eye.md](eye.md#prometheus-metrics-and-myriad-x-telemetry).

### Design choices

- **Standard collectors, not a custom one.** node_exporter (the board) and cAdvisor (the containers)
  are the standard Prometheus exporters, and Prometheus pulls from them and stores the data. One
  custom collector container would have re-implemented both. Grafana Alloy, which embeds both
  exporters in a single container, would still need Prometheus to store the data, and its arm64 image
  is 197 MB compressed against 38 MB for the two exporters.
- **The Myriad X's metrics come from the Eye.** Only one process can own the Myriad X
  ([eye.md](eye.md)), so another container could see it on USB but not talk to it. The Eye's existing
  HTTP server got a `/metrics` route.
- **One definition per headline number.** The `device:` recording rules in
  `services/dashboard/prometheus/rules.yml` feed both Grafana's Overview row and the home page, so
  the two can't disagree.
- **Video without transcoding.** go2rtc relays the Eye's RTSP to browsers: MJPEG over a WebSocket
  for `result` and `raw`, WebRTC for `h264raw`. Transcoding was ruled out because the Cortex-A53
  cores can't decode an 8-megapixel H.264 stream in real time. MediaMTX was considered, but its
  browser outputs (WebRTC, HLS) don't carry MJPEG.
- **Links that work from the browser.** Homepage's links are followed by the browser, so they need
  an address the browser reaches: `deploy` passes the kit's `HOST` (`HOMEPAGE_VAR_HOST`) and lists
  the device's names and IPv4 addresses in `HOMEPAGE_ALLOWED_HOSTS`. go2rtc's WebRTC offers the same
  address.
- **Grafana without a password.** Every visitor is an anonymous organisation Admin
  (`GF_AUTH_ANONYMOUS_ENABLED`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`). Grafana's own login, its form,
  the sign-out item and basic auth are off (`GF_AUTH_DISABLE_LOGIN`, `GF_AUTH_DISABLE_LOGIN_FORM`,
  `GF_AUTH_DISABLE_SIGNOUT_MENU`, `GF_AUTH_BASIC_ENABLED=false`), so the default `admin`/`admin`
  account can't be used, and new installs don't create it
  (`GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION`). Grafana 13's top bar shows *Sign in* to every
  visitor who isn't signed in, anonymous ones included, and no setting hides it. Signing everyone
  in through Grafana's auth proxy would hide it, at the cost of a proxy container in front of
  Grafana; the link is left instead, and leads to an empty login page.
- **Configuration read-only, versions in one place.** Every configuration folder is mounted
  read-only, and the image tags are set only in `compose.yaml`.

### How the Myriad X telemetry was found

1. Upstream `main.cpp` includes `<VPUTempRead.hpp>` but never calls it.
2. In the base image, `/eyesom/mxif/install/include` has `VPUTempRead.hpp` and `VPUTelemetry.h`.
   `libmxIf.so` exports `vpual::tempread::stub::{Init,Get,Deinit}` and
   `vpual::telemetry::{Init,Deinit,Memory::Get}`. The firmware, `/eyesom/mx.mvcmd`, contains the
   service names `TempRead`, `VPUTempRead`, `Telemetry` and `VPUTelemetry`.
3. Disassembling `libmxIf.so` (`objdump -d -C`) showed that `mxIf::Boot()` calls
   `vpual::telemetry::Init()` and `vpual::tempread::stub::Init()`, that `mxIf::Reset()` calls
   `vpual::telemetry::Deinit()`, and that nothing in the library calls either `Get`. The Eye's log
   already said `[VPUTempRead] Init: Temperature reading initialized` at every start.
4. A probe program was compiled in the base image and run with the Eye stopped for 12 s. It called
   `mxIf::Boot()`, three rounds of both `Get` calls, then `mxIf::Reset()`. Temperatures came back
   `GET_SUCCESS`, 38–39 °C on all four sensors at idle. Memory came back 0 (success), with pools of
   1,572,864,000 (main), 121,470,824 (LeonOS heap) and 333,695,568 (LeonRT heap) bytes. Each call
   took 1.2–1.8 ms.

The sensor names are the indices in `VPUTempReadTypes.h` (`IDX_CSS`, `IDX_MSS`, `IDX_UPA`,
`IDX_DSS`).

### Obstacles

| Obstacle | How it showed up | Diagnosis | Fix |
|---|---|---|---|
| **Inference latency can't be measured** | Latency measured against the board's clock drifted from +0.78 s on average one minute to −0.54…+0.24 s the next, two-thirds of it negative | Frame timestamps come from the Myriad X's clock, which isn't synchronised with the board's | Not exported; documented as not measured |
| go2rtc can't read `/result` and `/raw` | | They offer only UDP (section 14), and go2rtc's RTSP client speaks only TCP | go2rtc uses `/resultTCP` and `/rawTCP`, the same streams over TCP (ffprobe: MJPEG 816×616, 10 fps) |
| **Full-resolution H.264 stalls in the browser** | MSE playback froze, with `RangeError: offset is out of bounds` in go2rtc's `video-rtc.js` | The player copies data into a fixed 2 MB buffer while the browser's SourceBuffer is busy; at 3264×2464 and about 25 Mbit/s it overflows | WebRTC for that stream, on port 8556 (the Eye has 8555) |
| **go2rtc 1.9.15 crash** | `panic: runtime error: invalid memory address or nil pointer dereference` in `net/http.(*response).write`, from go2rtc's MP4 consumer; Docker restarted it | The progressive `api/stream.mp4` handler kept writing after the browser aborted the request | Docker's restart policy; the kit's pages don't link `stream.mp4` |
| WebRTC from a container | | go2rtc would offer container addresses (172.18.x.x) and addresses found through public STUN servers | Port 8556 published for TCP and UDP; `candidates: [${PERCEPT_HOST}:8556]`, `ice_servers: []`, automatic candidates filtered out |
| Homepage on a read-only configuration | `Warning: Could not chown /app/config` | Homepage 2's entrypoint chowns the configuration to `PUID`, and logs into it by default | `PUID`/`PGID` 1000, which already own the app (so `/app/.next` isn't chowned); `LOG_TARGETS=stdout`; all nine configuration files provided, so none is copied in |
| Homepage refuses unknown hosts | `Host validation failed` unless the `Host` header is listed | `HOMEPAGE_ALLOWED_HOSTS` is required since Homepage 1.0 | `deploy` lists `HOST`, the device's IPv4 addresses (not Docker's bridges, sorted, so the list only changes when they do), the host name and `<host name>.local` |
| No `grafana/grafana-oss:13.3.0` | Docker Hub: 404 | Grafana publishes OSS images as `grafana/grafana` | `grafana/grafana`; 1.36 GB on disk, more than half of the dashboard's 2.2 GB |
| Grafana's memory grows | RSS from 317 to 356 MiB in 20 minutes | The Go heap (about 160 MiB in use) grows with Grafana's caches, and the board has no swap | `GOMEMLIMIT=256MiB`, a soft cap that makes the Go garbage collector work harder near it |
| node_exporter without udev data | `Failed to open directory, disabling udev device properties` | `--path.udev.data` isn't relative to `--path.rootfs` | `--path.udev.data=/host/run/udev/data` |
| **Stale values after a stall** | If inference stopped while the app kept running, the Myriad X gauges and `percept_eye_objects` would keep their last values | The inference thread updates them | `/metrics` leaves them out once they're more than 15 s (or three telemetry intervals) old, so a stall shows as missing data. The telemetry interval is set before the HTTP thread starts |
| "Since last inference" read up to 15 s | | `time() - percept_eye_last_inference_timestamp_seconds` includes the time since the last scrape | `timestamp(x) - x`: the result's age when it was scraped |

The constraints that shaped how configuration is updated under running containers, and the space
checks, are in section 16.

### What was tested (on the device)

- **Eye**: built with `-Werror`. `/metrics`: about 12 inferences a second; detections by label
  (laptop, person, chair…); four temperatures, 44–49 °C under load; the main pool at 205 MiB of
  1.5 GB; USB 5 Gbit/s. `Myriad X telemetry: temperatures ok, memory ok` was logged once at start.
  It kept running through deploys that changed only the dashboard, and the voice sample kept
  receiving detections over MQTT. Leaving out stale values was checked by reading the code only:
  stalling the pipeline on purpose would mean disconnecting the Myriad X.
- **Prometheus**: all four targets up; all nine `device:` rules return values; about 2,800 series
  and 185 samples a second, and about 4 MB per 2-hour block before compaction.
- **Grafana**: healthy; the data source's health check passes; the 32-panel dashboard is provisioned
  and opens as the home dashboard without signing in. In a browser it shows two lines of headline
  numbers, and only the network interfaces that are up.
- **Home page**: renders on port 80 with the widgets' values and green dots. `Host` headers of the
  device's address and host name got 200; `evil.example` got 400.
- **Video**: JPEG frames from `result` and `raw`; all three streams side by side in a browser
  (MJPEG, MJPEG, and WebRTC at 3264×2464, about 24 frames a second decoded).
- **Resource use**: see [dashboard.md](dashboard.md#resource-use).
- **Grafana without a password**: an anonymous visitor saved, read back and deleted a dashboard,
  and added and deleted a data source, through the API. `POST /login` with `admin`/`admin` was
  refused (`auth.client.notConfigured`). Requests with `admin:admin`, with a wrong password and with
  no credentials got identical answers, so the account gives nothing more than an anonymous visit;
  server-wide user administration (`/api/users`) is refused to all three.

---

## 16. The device stack: compose.yaml and deploy

**Files**: `compose.yaml`, `scripts/stack.sh`, `config.example/`, `services/mqtt/mosquitto.conf`,
`modules/eye/entrypoint.sh`, and the stack commands in `percept-kit.sh` (`deploy`, `status`,
`logs`, `restart`, `stop`, `start`, `rebuild`, `compose`, `eye-base`). All authored. How it works is
in [README.md](README.md#8-the-containers-one-compose-project); how to change it, in
[customize.md](customize.md).

### Design choices

- **One Compose project.** Docker Compose (Ubuntu's `docker-compose-v2`) describes every container
  once, in `compose.yaml`. The kit's commands are thin wrappers, and `docker compose` on the device
  does the same things. Container names equal service names, so `docker logs eye` works.
- **Settings in one place.** `config/percept.env` is the only settings file, and each setting is
  documented once, in `config.example/percept.env`. The device's `.env` and `.env.d/` are generated
  from it. Each module gets only its own prefix (`EYE_`, `EAR_`, `MOUTH_`, `VOICE_`), so a change
  recreates only the modules it concerns. Files that settings refer to go in `config/files/`, and
  any other change to the services in `config/compose.override.yaml`, which Compose merges.
- **Changes found by content.** Each image built on the device carries the hash of its source
  folder as a label (`org.percept-kit.source`), and `deploy` rebuilds only those whose hash differs.
  The upload works the same way: the PC sends its files' hashes, the device stages the files it
  already has as hard links in `.incoming/`, and only the others cross the network.
- **The broker is a core service.** The Eye always publishes to MQTT. The device's own Mosquitto
  runs while `MQTT_URL` is empty (Compose profile `local-mqtt`), and that one setting moves both the
  Eye and the voice sample to another broker.
- **Data outside the project.** The kit creates the network and the named volumes as external, so
  `docker compose down -v` can't delete them.

### Constraints

| Constraint | Effect | How the design handles it |
|---|---|---|
| Bind mounts follow a file's inode, not its path | Deleting and recreating a configuration folder leaves running containers on the deleted copy | `rsync --inplace` rewrites changed files where they are; services whose mounted files changed but kept their container are restarted |
| Compose checks a configuration only when it runs | A bad setting or override could stop a deploy half-way | The upload's configuration is loaded first, with its own `.env` (`stack.sh plan`). If Compose rejects it, `deploy` stops before anything on the device changes |
| A deploy can still fail after it has updated files, at a build or a pull | The next deploy would take the new files as its baseline and restart nothing | The baseline (hashes of mounted files, container IDs) stays in `.deploy-baseline` until a deploy finishes |
| The device names the files it needs | A tampered device could ask for files outside the stack | The PC sends only files it offered; any other name stops the deploy |
| Docker copies a build context's file modes into the image | Restrictive modes on all of `config/` would make your own containers' sources unreadable to them | Only `percept.env`, the override and `files/` are kept to the device user and group 5000 |
| A container is recreated whenever its environment changes, env files included | One shared env file would restart every module, the mouth's 13 s voice load included, on any change | Each module's own env file in `.env.d/` |
| `hostname -I` lists Docker's bridges too, in an order that can change | Homepage's allowed hosts would change, and Homepage be recreated, with nothing really changed | The device's own interfaces only, sorted |
| `docker compose up --remove-orphans` leaves services that a profile switched off running | | `deploy` removes them (`--profile '*' rm -sf`) |
| The eMMC is small: about 3.5 GB free with everything installed | Builds and image pulls can fill it | Checks before each build (1.5 GB for the Eye, 1 GB for the others), before pulls (3 GB when Grafana's image is missing, 1 GB otherwise), and before the Eye's build base is put back (1.3 GB from the backup, 3 GB from MCR, plus the build's 1.5 GB). The build cache is deleted after each build, and replaced image versions and anonymous volumes after each deploy. The journal is capped at 200 MB, and container logs at 15 MB each |
| The Realtek Wi-Fi driver logs every power-save change at its default level | About 10,700 journal lines an hour: 800 MB in three days | `rtw_drv_log_level=2` (errors only), at boot and straight away through sysfs |
| Files edited on Windows can have CRLF line ends | Scripts and env files break on the device | `deploy` removes the CRs from the text files it receives, before hashing |

### What was tested (on the device)

- **First deploy**: built the four images (the Eye in about 15 minutes) and started all eleven
  containers. Containers started by hand under the same names were replaced, and the existing
  Grafana, Prometheus and speech-cache volumes were kept.
- **Repeat deploys**: with nothing changed, `deploy` sent no file, built nothing, recreated or
  restarted no container, and took about a minute. After an edit to `ear.py` and `mouth.py`, only
  those two files were sent and only those two images rebuilt.
- **Reboot**: the device was back in under 2 minutes, and every component checked `ok` about 30 s
  after SSH answered, with the Eye SoM, the Ear and the Bluetooth adapter present.
- **The recipes in customize.md**, each then undone:
  - the bundled ONNX SSD (`EYE_MODEL`, `EYE_PARSER=onnxssd`) loaded with its 183 classes;
  - a password-protected TLS broker (`MQTT_URL=mqtts://…`, `MQTT_USERNAME`, `MQTT_PASSWORD_FILE`,
    `MQTT_CA_FILE`): the device's broker was removed, the Eye and the voice sample connected over
    TLS, and messages arrived; a wrong password was refused (`not authorised`). Removing
    `MQTT_URL` brought the device's broker back and recreated only the Eye and the voice sample;
  - the voice sample switched off with a profile in the override, and Node-RED added: the voice
    container was removed and Node-RED answered on port 1880; deleting the override reversed both;
  - `eye-base remove` freed 1,253 MB while the Eye kept running, and `eye-base restore` loaded the
    backup's copy with an identical image ID.