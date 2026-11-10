# Azure Percept DK – Device Assessment

The stock device as shipped, read over SSH: what's in it, and whether it can run Ubuntu. How
percept-kit builds on this is in [ARCHITECTURE.md](ARCHITECTURE.md) and
[PROVENANCE.md](PROVENANCE.md).

## Hardware

| Item | Finding |
|---|---|
| Board | Azure Percept DK (APDK-101), NXP i.MX8MQ (EVK-derived "pe100" device tree) |
| CPU | 4× Cortex-A53 @ 1.3 GHz (aarch64) |
| RAM | ~2.7 GB usable (320 MB CMA reserved) |
| Storage | 16 GB eMMC (`mmcblk0`) + 2× 4 MB boot partitions holding signed firmware |
| Wi-Fi | Realtek RTL8822CE (PCIe, out-of-tree `88x2ce` driver). `wlan0` = client, `wlan1` = OOBE hotspot (10.1.1.1) |
| Bluetooth | Realtek USB (`btusb`/`btrtl`) |
| Ethernet | `eth0` i.MX FEC + `eth1` Intel I211 (`igb`), both currently unplugged |
| GPU / VPU | Vivante GC7000L (`galcore`), Hantro VPU (`mxc_hantro`) |
| TPM | Present (`/dev/tpm0`) |
| Watchdog | `imx2_wdt` (`/dev/watchdog0`) |
| Display | None (no HDMI). Serial console exists (`ttymxc0`) but isn't externally exposed |
| Vision SoM | Intel Myriad X (`03e7:f63b`) + "Azure Eye SoM Controller". **Working offline**: `azureeyemodule:2301-1-noauth` is running inference |
| Audio SoM | "Azure Ear SoM" USB audio + controller. Hardware enumerates. Speech module fails (cloud KWS model download) |

## Software

| Item | Finding |
|---|---|
| OS | CBL-Mariner 1.0.20220608 ("Santa Cruz" drop 204670, `public_preview_1.0`). **End of life**; no updates since Percept retirement (2023-03-30) |
| Kernel | `5.4.3-26.cm1` (NXP vendor 5.4). No initrd. USB storage + ext4 built in. No AppArmor/SELinux/lockdown/IMA-appraise |
| Stack | IoT Edge 1.4, Docker/containerd, ADU agent, hostapd/dnsmasq (OOBE AP), nginx, telemetry daemons |
| Disk usage | `/` 872 MB / 2 GB, `/var` 6.2 GB / 10.5 GB (mostly container images) |

### eMMC layout

| Part | Size | Use |
|---|---|---|
| p1 | 11 MB | ESP A (shim `bootaa64.efi`, `grubaa64.efi`, `boot/grub2/grub.cfg`, `pe100.dtb`) – active |
| p2 | 11 MB | ESP B (A/B update slot) |
| p3 | 2 GB | Mariner rootfs A – active (`/boot/vmlinuz` lives here) |
| p4 | 2 GB | Mariner rootfs B – inactive |
| p5 | 10.5 GB | `/var` |

## Boot chain & Secure Boot (the key constraint)

```
ROM (HAB closed) → signed SPL/ATF → Microsoft Project Mu UEFI (fw 2102009006)
  → shim (bootaa64.efi, vendor cert "SantaCruz SecureBoot Production Signing")
  → GRUB 2.06 (grubaa64.efi) → shim_lock-verified kernel /boot/vmlinuz on p3
```

- **i.MX HAB is closed** (OCOTP Bank1 Word3 bit 25 set; `fw-version` reports `uboot-secure 1`, `uefi-secure 1`). Custom U-Boot/UEFI firmware **cannot** be flashed.
- **UEFI Secure Boot is enabled** (`SecureBoot=1`, `SetupMode=0`). PK/KEK = Microsoft *"Test Secure Boot Signing"*. `db` contains only *SantaCruz SecureBoot Production Signing*. **The Microsoft UEFI CA is not trusted**, so Ubuntu's shim, GRUB, and kernel will not boot.
- There's no video output or serial access, so the UEFI setup menu can't be reached to disable Secure Boot.
- **What *is* open:** `grub.cfg` on the ESP is plain text and not signature-checked. GRUB includes `load_env`/`save_env`/`search`/`usb`/`ext2`/`devicetree`. The **rootfs isn't verified**. Only the kernel image must carry the SantaCruz signature.

## Conclusion: can it run Ubuntu?

**Yes, with a caveat.** A stock Ubuntu installer/kernel can't boot. The workable approach is:

> **Ubuntu userland + the existing Microsoft-signed Mariner 5.4 kernel**, booted by the existing shim/GRUB, with only the `root=` in `grub.cfg` changed.

Trade-offs:

- Kernel stays at 5.4.3-26.cm1 forever (no kernel security updates). Userland gets normal Ubuntu `apt` updates.
- Kernel modules and firmware are copied from Mariner (Wi-Fi, BT, GPU, VPU drivers all work).
- No AppArmor in the kernel → avoid snaps. Ubuntu 22.04 LTS is the best match for kernel 5.4; 24.04 should work but is less tested on 5.4.
- Vision SoM can keep working under Ubuntu by running the `azureeyemodule:*-noauth` container in Docker.

## Risks

- A broken ESP `grub.cfg` = no boot with no screen to fix it. Microsoft's USB-C reflash package was retired.
  Mitigations: full eMMC backup first, a GRUB one-shot (`next_entry`) test boot that falls back to Mariner on power-cycle, and leaving Mariner on p3 as a rescue OS.
- Edits to the ESP must never touch `bootaa64.efi`/`grubaa64.efi` or the eMMC boot partitions.
