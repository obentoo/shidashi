#!/usr/bin/env bash
# smoke-iso.sh — boot smoke test of the bentoo live ISO (OVERVIEW §7, Phase 1).
#
# Two modes:
#
#   (default) MINIMAL SELF-CONTAINED — does not depend on the binhost/Factory. Builds a
#   test live ISO exercising the REAL functions under test (shidashi.image:
#   make_squashfs + build_iso) over a tiny rootfs whose /sbin/init is a
#   static binary that, after the dracut dmsquash-live pivot, prints the
#   SHIDASHI_SMOKE_OK sentinel on the serial port and powers off. Boots the ISO in QEMU (KVM if
#   available, otherwise TCG — validates arrowlake without AVX-512, §9.4) and asserts the
#   sentinel. Proves the image path end to end (squashfs → grub-mkrescue
#   → boot → dmsquash-live mounts the squashfs as root) without compiling a world.
#
#   --iso PATH — PILOT: boots a real ISO already produced by `shidashi assemble`.
#   Extracts kernel+initramfs from the ISO (xorriso) and boots directly with console=ttyS0
#   to capture the boot log on the serial port; asserts the --expect string (default
#   "Reached target", the real image's systemd). Serves as the runbook for the
#   host-gated Phase 1 pilot (depends on the Phase 2 binhost).
#
# Host-gated: requires root (dracut + device mounting) + grub-mkrescue +
# mksquashfs + dracut + qemu-system-x86_64 + cc + an installed kernel.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SENTINEL="SHIDASHI_SMOKE_OK"

# --- options ------------------------------------------------------------------
ISO=""              # --iso PATH: pilot mode (boots an existing ISO)
EXPECT=""           # --expect STR: string to look for on the serial port (--iso mode)
WORK=""             # --work DIR: work root (default: mktemp)
TIMEOUT=300         # --timeout SECS: wall-clock budget of the QEMU boot
MEM=2048            # --mem MB: VM RAM (headroom for the large --no-hostonly initramfs)
KEEP=0              # --keep: keeps the work artifacts
DEBUG=0             # --debug: direct boot (-kernel/-initrd) with console=ttyS0
KERNEL_OVERRIDE=""  # --kernel PATH: raw bzImage (skips automatic discovery)
INITRAMFS_OVERRIDE="" # --initramfs PATH: ready-made initramfs (skips dracut)
LOCATE=0            # --locate: only resolves+prints the discovered kernel and exits

usage() {
    cat <<EOF
smoke-iso.sh — boot smoke test of the live ISO (OVERVIEW §7, Phase 1)

USAGE:
  scripts/smoke-iso.sh [options]                # MINIMAL self-contained mode
  scripts/smoke-iso.sh --iso bentoo-*.iso [...]  # PILOT mode (real ISO)

OPTIONS:
  --iso PATH        Boots a real ISO from 'shidashi assemble' (pilot mode).
  --expect STR      String expected on the serial port in --iso mode (default: "Reached target").
  --kernel PATH     The host's raw bzImage (skips automatic discovery).
  --initramfs PATH  Ready-made dmsquash-live initramfs (skips dracut).
  --locate          Only resolves+prints the discovered kernel and exits (diagnostics).
  --work DIR        Work root (default: disposable temporary directory).
  --timeout SECS    QEMU boot budget in seconds (default: ${TIMEOUT}).
  --mem MB          VM RAM in MB (default: ${MEM}).
  --debug           Direct -kernel/-initrd boot with console=ttyS0 rd.shell (diagnostics).
  --keep            Keeps the work artifacts for debugging.
  -h, --help        This help.

BOOT MODEL:
  Minimal mode builds the ISO with shidashi.image (squashfs + grub-mkrescue) and boots
  via grub; the static /sbin/init prints ${SENTINEL} on the serial port after the
  dracut dmsquash-live pivot. KVM is used when /dev/kvm is writable; otherwise it falls back to QEMU
  TCG (validates arrowlake — without AVX-512 the ISA fits within emulation, §9.4).

  The kernel is borrowed from the host. Discovery covers the classic dist-kernel
  (/boot/vmlinuz-KVER), kernel-install/BLS (\$machine-id/KVER/linux) and the systemd-boot
  UKI (extracts the .linux section from /boot/EFI/Linux/*.efi via objcopy);
  use --kernel to point at one manually.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --iso) ISO="${2:?--iso requires a path}"; shift 2 ;;
        --expect) EXPECT="${2:?--expect requires a string}"; shift 2 ;;
        --kernel) KERNEL_OVERRIDE="${2:?--kernel requires a path}"; shift 2 ;;
        --initramfs) INITRAMFS_OVERRIDE="${2:?--initramfs requires a path}"; shift 2 ;;
        --locate) LOCATE=1; shift ;;
        --work) WORK="${2:?--work requires a directory}"; shift 2 ;;
        --timeout) TIMEOUT="${2:?--timeout requires seconds}"; shift 2 ;;
        --mem) MEM="${2:?--mem requires MB}"; shift 2 ;;
        --debug) DEBUG=1; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "smoke-iso: error: $*" >&2; exit 1; }

require_tool() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' missing on the host; install it ($2)"
}

# Locates a raw bzImage of kernel $KVER, robust to bootloader layouts:
# classic dist-kernel, kernel-install/BLS and systemd-boot UKI. Prints the path;
# for a UKI it extracts the .linux section into the work dir via objcopy. Honors --kernel.
find_kernel() {
    if [ -n "$KERNEL_OVERRIDE" ]; then
        [ -r "$KERNEL_OVERRIDE" ] || die "--kernel unreadable: $KERNEL_OVERRIDE"
        printf '%s' "$KERNEL_OVERRIDE"
        return 0
    fi
    local mid="" cand uki
    [ -r /etc/machine-id ] && mid="$(cat /etc/machine-id)"
    # Raw bzImage in known layouts (classic, /lib/modules, BLS kernel-install).
    for cand in \
        "/boot/vmlinuz-$KVER" "/boot/vmlinuz" "/boot/kernel-$KVER" \
        "/lib/modules/$KVER/vmlinuz" \
        ${mid:+/boot/$mid/$KVER/linux /efi/$mid/$KVER/linux}; do
        [ -r "$cand" ] && { printf '%s' "$cand"; return 0; }
    done
    # systemd-boot UKI: the raw kernel is the .linux section of the PE/EFI.
    for uki in /boot/EFI/Linux/*.efi /efi/EFI/Linux/*.efi; do
        [ -r "$uki" ] || continue
        command -v objcopy >/dev/null 2>&1 \
            || die "UKI found ($uki) but 'objcopy' missing (sys-devel/binutils)"
        if objcopy -O binary --only-section=.linux "$uki" "$WORK/vmlinuz.uki" 2>/dev/null \
            && [ -s "$WORK/vmlinuz.uki" ]; then
            printf '%s' "$WORK/vmlinuz.uki"
            return 0
        fi
    done
    die "kernel $KVER not found (looked in /boot/vmlinuz-$KVER, /lib/modules/$KVER/vmlinuz,\
 BLS \$machine-id/$KVER/linux and UKI /boot/EFI/Linux/*.efi); pass --kernel PATH"
}

# --- preflight ---------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "requires root (dracut + device mounting + QEMU boot)"
require_tool qemu-system-x86_64 "app-emulation/qemu"
require_tool grub-mkrescue "sys-boot/grub + sys-fs/mtools"
require_tool xorriso "dev-libs/libisoburn"

# Work root: --work (the caller's, kept) or our own temp (disposable).
if [ -n "$WORK" ]; then
    mkdir -p "$WORK"
    MADE_TMP=0
else
    WORK="$(mktemp -d -t shidashi-smoke.XXXXXX)"
    MADE_TMP=1
fi
cleanup() { if [ "$KEEP" -eq 0 ] && [ "$MADE_TMP" -eq 1 ]; then rm -rf "$WORK"; fi; }
trap cleanup EXIT

SERIAL_LOG="$WORK/serial.log"
: >"$SERIAL_LOG"

# Reads the volume label straight from the code under test (single source — image.VOLUME_ID).
VOLID="$(cd "$REPO_ROOT" && python3 -c 'from shidashi.image import VOLUME_ID; print(VOLUME_ID)')"

# --- shared QEMU runner -----------------------------------------------------
# Builds the base QEMU argv; KVM when writable, otherwise TCG (-cpu max for arrowlake).
qemu_base() {
    local -n _args=$1
    _args=(-m "$MEM" -display none -no-reboot -serial "file:$SERIAL_LOG")
    if [ -w /dev/kvm ]; then
        _args+=(-enable-kvm -cpu host)
        echo "smoke-iso: KVM accelerator (/dev/kvm writable)" >&2
    else
        _args+=(-machine accel=tcg -cpu max)
        echo "smoke-iso: TCG accelerator (no KVM) — slower" >&2
    fi
}

boot_and_check() {
    local needle="$1"; shift
    local -a qargs
    qemu_base qargs
    echo "smoke-iso: booting (timeout ${TIMEOUT}s)…" >&2
    timeout "$TIMEOUT" qemu-system-x86_64 "${qargs[@]}" "$@" || true
    if grep -q "$needle" "$SERIAL_LOG"; then
        echo "smoke-iso: OK — sentinel '$needle' found on the serial port." >&2
        return 0
    fi
    echo "smoke-iso: FAILURE — '$needle' missing from the serial port. Log:" >&2
    sed 's/^/  | /' "$SERIAL_LOG" >&2 || true
    echo "smoke-iso: hint — run with --debug for a verbose boot (console=ttyS0 rd.shell)." >&2
    return 1
}

# =============================================================================
# PILOT mode: boots a real ISO from `shidashi assemble`.
# =============================================================================
if [ -n "$ISO" ]; then
    [ -r "$ISO" ] || die "ISO unreadable: $ISO"
    NEEDLE="${EXPECT:-Reached target}"
    # Extracts the boot artifacts from the ISO (shidashi.image layout: boot/vmlinuz,
    # boot/initramfs.img) and boots directly with console=ttyS0 — the ISO still provides
    # the squashfs via CDLABEL=$VOLID, exercising dmsquash-live for real.
    xorriso -osirrox on -indev "$ISO" \
        -extract /boot/vmlinuz "$WORK/vmlinuz" \
        -extract /boot/initramfs.img "$WORK/initramfs.img" >/dev/null 2>&1 \
        || die "failed to extract kernel/initramfs from the ISO (unexpected layout?)"
    boot_and_check "$NEEDLE" \
        -kernel "$WORK/vmlinuz" -initrd "$WORK/initramfs.img" \
        -append "root=live:CDLABEL=$VOLID rd.live.image console=ttyS0" \
        -cdrom "$ISO"
    exit $?
fi

# =============================================================================
# MINIMAL self-contained mode: builds a test ISO and boots it.
# =============================================================================
require_tool mksquashfs "sys-fs/squashfs-tools"
require_tool dracut "sys-kernel/dracut"
require_tool cc "sys-devel/gcc"

# Installed kernel: exactly one in /lib/modules (same as the Assembler's guard).
mapfile -t KVERS < <(find /lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)
[ "${#KVERS[@]}" -eq 1 ] || die "expected exactly one kernel in /lib/modules; found: ${KVERS[*]:-none}"
KVER="${KVERS[0]}"
KERNEL="$(find_kernel)"
echo "smoke-iso: kernel $KVER → $KERNEL" >&2
[ "$LOCATE" -eq 1 ] && exit 0  # --locate: only confirms the discovery and exits

# 1) minimal rootfs: a static /sbin/init that signals the sentinel and powers off, plus
#    the markers the boot requires of a "real root":
#      - /usr at the top: dmsquash-live only accepts a raw squashfs as root with
#        /usr (or /ostree); otherwise it requires the nested LiveOS/rootfs.img layout and
#        aborts ("Failed to find a root filesystem").
#      - /etc/os-release: systemd switch-root refuses a root without it ("does not
#        seem to be an OS tree").
#    Every real Assembler rootfs has both (usr-merged Gentoo + os-release) →
#    image.py is correct; the gap was only in the smoke test's synthetic rootfs.
ROOTFS="$WORK/rootfs"
mkdir -p "$ROOTFS"/{sbin,usr,etc,var,tmp,root}
printf 'NAME="bentoo-smoke"\nID=bentoo\nPRETTY_NAME="bentoo smoke-test"\nVERSION_ID="0"\n' \
    >"$ROOTFS/etc/os-release"
cat >"$WORK/init.c" <<'EOF'
/* Minimal /sbin/init: mounts devtmpfs (the live root is a writable overlay), emits the
   sentinel on the serial port/console and powers off — proves dmsquash-live pivoted. */
#include <fcntl.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <unistd.h>

static void emit(const char *path) {
    int fd = open(path, O_WRONLY | O_NOCTTY);
    if (fd >= 0) {
        static const char m[] = "\nSHIDASHI_SMOKE_OK\n";
        (void)!write(fd, m, sizeof(m) - 1);
        close(fd);
    }
}

int main(void) {
    mkdir("/dev", 0755);
    mount("dev", "/dev", "devtmpfs", 0, "");
    emit("/dev/console");
    emit("/dev/ttyS0");
    sync();
    reboot(RB_POWER_OFF);
    for (;;) pause();
    return 0;
}
EOF
cc -static -O2 -s -o "$ROOTFS/sbin/init" "$WORK/init.c" \
    || die "failed to compile the static /sbin/init (is cc -static available?)"

# 2) dracut dmsquash-live initramfs. Unlike the Assembler (which runs dracut
#    in a clean stage3 container), here it runs on the HOST — so it must be
#    isolated from the host's config: --conf /dev/null + an empty --confdir (ignores
#    /etc/dracut.conf{,.d}), --no-hostonly-cmdline (does not embed the host's rd.luks/rd.lvm)
#    and --omit of the storage discovery modules (crypt/lvm/mdraid/…). Without
#    that the initramfs inherits the host's crypttab/LUKS and the VM hangs in the initqueue
#    waiting for the host's encrypted disk (absent in QEMU). `dm` stays —
#    dmsquash-live uses device-mapper for the overlay. Or a ready-made initramfs via
#    --initramfs.
INITRAMFS="$WORK/initramfs.img"
if [ -n "$INITRAMFS_OVERRIDE" ]; then
    [ -r "$INITRAMFS_OVERRIDE" ] || die "--initramfs unreadable: $INITRAMFS_OVERRIDE"
    cp "$INITRAMFS_OVERRIDE" "$INITRAMFS"
else
    mkdir -p "$WORK/dracut.conf.d"  # empty confdir → ignores the host's config
    dracut --add dmsquash-live --omit "crypt systemd-cryptsetup dmraid mdraid lvm multipath" \
        --no-hostonly --no-hostonly-cmdline \
        --conf /dev/null --confdir "$WORK/dracut.conf.d" \
        --force "$INITRAMFS" "$KVER" \
        || die "dracut failed (dmsquash-live)"
fi

# 3) squashfs + hybrid ISO via the REAL functions under test (shidashi.image).
ISO_OUT="$WORK/bentoo-smoke.iso"
SQUASHFS="$WORK/rootfs.squashfs"
( cd "$REPO_ROOT" && python3 - "$ROOTFS" "$SQUASHFS" "$ISO_OUT" "$KERNEL" "$INITRAMFS" <<'PY'
import sys
from pathlib import Path

from shidashi import image
from shidashi.toolbox import HostTools

# the host's tools on purpose: this smoke test boots the host's kernel too
rootfs, squashfs, iso, kernel, initramfs = (Path(a) for a in sys.argv[1:6])
image.make_squashfs(rootfs, squashfs, tools=HostTools())
image.build_iso(squashfs, iso, tools=HostTools(), kernel=kernel, initramfs=initramfs)
print(f"smoke-iso: ISO built via shidashi.image → {iso}", file=sys.stderr)
PY
) || die "failed to build the ISO via shidashi.image"

# 4) boot + assert the sentinel. Default: boot via grub (tests the bootloader);
#    --debug: direct boot with console=ttyS0 rd.shell for verbose diagnostics.
if [ "$DEBUG" -eq 1 ]; then
    boot_and_check "$SENTINEL" \
        -kernel "$KERNEL" -initrd "$INITRAMFS" \
        -append "root=live:CDLABEL=$VOLID rd.live.image console=ttyS0 rd.shell" \
        -cdrom "$ISO_OUT"
else
    boot_and_check "$SENTINEL" -cdrom "$ISO_OUT" -boot d
fi
