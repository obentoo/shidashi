#!/usr/bin/env bash
# smoke-iso.sh — smoke-test de boot da ISO live do bentoo (OVERVIEW §7, Fase 1).
#
# Dois modos:
#
#   (padrão) MÍNIMO SELF-CONTAINED — não depende do binhost/Factory. Monta uma
#   ISO live de teste exercitando as funções REAIS sob teste (shidashi.image:
#   make_squashfs + build_iso) sobre um rootfs minúsculo cujo /sbin/init é um
#   binário estático que, após o pivot do dracut dmsquash-live, imprime o
#   sentinela SHIDASHI_SMOKE_OK na serial e desliga. Boota a ISO em QEMU (KVM se
#   disponível, senão TCG — valida arrowlake sem AVX-512, §9.4) e assere o
#   sentinela. Prova o caminho de imagem ponta-a-ponta (squashfs → grub-mkrescue
#   → boot → dmsquash-live monta o squashfs como raiz) sem compilar um mundo.
#
#   --iso PATH — PILOT: boota uma ISO real já produzida por `shidashi assemble`.
#   Extrai kernel+initramfs da ISO (xorriso) e faz boot direto com console=ttyS0
#   para capturar o log de boot na serial; assere a string --expect (default
#   "Reached target", o systemd da imagem real). Serve de runbook do pilot
#   host-gated da Fase 1 (depende do binhost da Fase 2).
#
# Host-gated: exige root (dracut + montagem de dispositivos) + grub-mkrescue +
# mksquashfs + dracut + qemu-system-x86_64 + cc + um kernel instalado.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SENTINEL="SHIDASHI_SMOKE_OK"

# --- opções ------------------------------------------------------------------
ISO=""              # --iso PATH: modo pilot (boota uma ISO existente)
EXPECT=""           # --expect STR: string a procurar na serial (modo --iso)
WORK=""             # --work DIR: raiz de trabalho (default: mktemp)
TIMEOUT=300         # --timeout SECS: orçamento de wall-clock do boot QEMU
MEM=2048            # --mem MB: RAM da VM (folga p/ o initramfs --no-hostonly grande)
KEEP=0              # --keep: preserva os artefatos de trabalho
DEBUG=0             # --debug: boot direto (-kernel/-initrd) com console=ttyS0
KERNEL_OVERRIDE=""  # --kernel PATH: bzImage cru (pula a descoberta automática)
INITRAMFS_OVERRIDE="" # --initramfs PATH: initramfs pronto (pula o dracut)
LOCATE=0            # --locate: só resolve+imprime o kernel descoberto e sai

usage() {
    cat <<EOF
smoke-iso.sh — smoke-test de boot da ISO live (OVERVIEW §7, Fase 1)

USO:
  scripts/smoke-iso.sh [opções]                 # modo MÍNIMO self-contained
  scripts/smoke-iso.sh --iso bentoo-*.iso [...]  # modo PILOT (ISO real)

OPÇÕES:
  --iso PATH        Boota uma ISO real do 'shidashi assemble' (modo pilot).
  --expect STR      String esperada na serial no modo --iso (default: "Reached target").
  --kernel PATH     bzImage cru do host (pula a descoberta automática).
  --initramfs PATH  initramfs dmsquash-live pronto (pula o dracut).
  --locate          Só resolve+imprime o kernel descoberto e sai (diagnóstico).
  --work DIR        Raiz de trabalho (default: diretório temporário descartável).
  --timeout SECS    Orçamento de boot do QEMU em segundos (default: ${TIMEOUT}).
  --mem MB          RAM da VM em MB (default: ${MEM}).
  --debug           Boot direto -kernel/-initrd com console=ttyS0 rd.shell (diagnóstico).
  --keep            Preserva os artefatos de trabalho para depuração.
  -h, --help        Esta ajuda.

MODELO DE BOOT:
  O modo mínimo monta a ISO com shidashi.image (squashfs + grub-mkrescue) e boota
  via grub; o /sbin/init estático imprime ${SENTINEL} na serial após o pivot do
  dracut dmsquash-live. KVM é usado quando /dev/kvm é gravável; senão cai em QEMU
  TCG (valida arrowlake — sem AVX-512 a ISA cabe na emulação, §9.4).

  O kernel é tomado emprestado do host. A descoberta cobre dist-kernel clássico
  (/boot/vmlinuz-KVER), kernel-install/BLS (\$machine-id/KVER/linux) e UKI
  systemd-boot (extrai a seção .linux de /boot/EFI/Linux/*.efi via objcopy);
  use --kernel para apontar manualmente.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --iso) ISO="${2:?--iso requer um caminho}"; shift 2 ;;
        --expect) EXPECT="${2:?--expect requer uma string}"; shift 2 ;;
        --kernel) KERNEL_OVERRIDE="${2:?--kernel requer um caminho}"; shift 2 ;;
        --initramfs) INITRAMFS_OVERRIDE="${2:?--initramfs requer um caminho}"; shift 2 ;;
        --locate) LOCATE=1; shift ;;
        --work) WORK="${2:?--work requer um diretório}"; shift 2 ;;
        --timeout) TIMEOUT="${2:?--timeout requer segundos}"; shift 2 ;;
        --mem) MEM="${2:?--mem requer MB}"; shift 2 ;;
        --debug) DEBUG=1; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "opção desconhecida: $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "smoke-iso: erro: $*" >&2; exit 1; }

require_tool() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' ausente no host; instale-o ($2)"
}

# Localiza um bzImage cru do kernel $KVER, robusto a layouts de bootloader:
# dist-kernel clássico, kernel-install/BLS e UKI systemd-boot. Imprime o caminho;
# numa UKI extrai a seção .linux para o work dir via objcopy. Honra --kernel.
find_kernel() {
    if [ -n "$KERNEL_OVERRIDE" ]; then
        [ -r "$KERNEL_OVERRIDE" ] || die "--kernel ilegível: $KERNEL_OVERRIDE"
        printf '%s' "$KERNEL_OVERRIDE"
        return 0
    fi
    local mid="" cand uki
    [ -r /etc/machine-id ] && mid="$(cat /etc/machine-id)"
    # bzImage cru em layouts conhecidos (clássico, /lib/modules, BLS kernel-install).
    for cand in \
        "/boot/vmlinuz-$KVER" "/boot/vmlinuz" "/boot/kernel-$KVER" \
        "/lib/modules/$KVER/vmlinuz" \
        ${mid:+/boot/$mid/$KVER/linux /efi/$mid/$KVER/linux}; do
        [ -r "$cand" ] && { printf '%s' "$cand"; return 0; }
    done
    # UKI systemd-boot: o kernel cru é a seção .linux do PE/EFI.
    for uki in /boot/EFI/Linux/*.efi /efi/EFI/Linux/*.efi; do
        [ -r "$uki" ] || continue
        command -v objcopy >/dev/null 2>&1 \
            || die "UKI achada ($uki) mas 'objcopy' ausente (sys-devel/binutils)"
        if objcopy -O binary --only-section=.linux "$uki" "$WORK/vmlinuz.uki" 2>/dev/null \
            && [ -s "$WORK/vmlinuz.uki" ]; then
            printf '%s' "$WORK/vmlinuz.uki"
            return 0
        fi
    done
    die "kernel $KVER não encontrado (procurei /boot/vmlinuz-$KVER, /lib/modules/$KVER/vmlinuz,\
 BLS \$machine-id/$KVER/linux e UKI /boot/EFI/Linux/*.efi); passe --kernel PATH"
}

# --- preflight ---------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "requer root (dracut + montagem de dispositivos + boot QEMU)"
require_tool qemu-system-x86_64 "app-emulation/qemu"
require_tool grub-mkrescue "sys-boot/grub + sys-fs/mtools"
require_tool xorriso "dev-libs/libisoburn"

# Raiz de trabalho: --work (do chamador, preservada) ou temp próprio (descartável).
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

# Lê o rótulo de volume direto do código sob teste (fonte única — image.VOLUME_ID).
VOLID="$(cd "$REPO_ROOT" && python3 -c 'from shidashi.image import VOLUME_ID; print(VOLUME_ID)')"

# --- runner QEMU compartilhado ----------------------------------------------
# Monta o argv base do QEMU; KVM quando gravável, senão TCG (-cpu max p/ arrowlake).
qemu_base() {
    local -n _args=$1
    _args=(-m "$MEM" -display none -no-reboot -serial "file:$SERIAL_LOG")
    if [ -w /dev/kvm ]; then
        _args+=(-enable-kvm -cpu host)
        echo "smoke-iso: acelerador KVM (/dev/kvm gravável)" >&2
    else
        _args+=(-machine accel=tcg -cpu max)
        echo "smoke-iso: acelerador TCG (sem KVM) — mais lento" >&2
    fi
}

boot_and_check() {
    local needle="$1"; shift
    local -a qargs
    qemu_base qargs
    echo "smoke-iso: bootando (timeout ${TIMEOUT}s)…" >&2
    timeout "$TIMEOUT" qemu-system-x86_64 "${qargs[@]}" "$@" || true
    if grep -q "$needle" "$SERIAL_LOG"; then
        echo "smoke-iso: OK — sentinela '$needle' encontrado na serial." >&2
        return 0
    fi
    echo "smoke-iso: FALHA — '$needle' ausente na serial. Log:" >&2
    sed 's/^/  | /' "$SERIAL_LOG" >&2 || true
    echo "smoke-iso: dica — rode com --debug para boot verboso (console=ttyS0 rd.shell)." >&2
    return 1
}

# =============================================================================
# Modo PILOT: boota uma ISO real do `shidashi assemble`.
# =============================================================================
if [ -n "$ISO" ]; then
    [ -r "$ISO" ] || die "ISO ilegível: $ISO"
    NEEDLE="${EXPECT:-Reached target}"
    # Extrai os artefatos de boot da ISO (layout de shidashi.image: boot/vmlinuz,
    # boot/initramfs.img) e faz boot direto com console=ttyS0 — a ISO ainda provê
    # o squashfs via CDLABEL=$VOLID, exercitando o dmsquash-live de verdade.
    xorriso -osirrox on -indev "$ISO" \
        -extract /boot/vmlinuz "$WORK/vmlinuz" \
        -extract /boot/initramfs.img "$WORK/initramfs.img" >/dev/null 2>&1 \
        || die "falha ao extrair kernel/initramfs da ISO (layout inesperado?)"
    boot_and_check "$NEEDLE" \
        -kernel "$WORK/vmlinuz" -initrd "$WORK/initramfs.img" \
        -append "root=live:CDLABEL=$VOLID rd.live.image console=ttyS0" \
        -cdrom "$ISO"
    exit $?
fi

# =============================================================================
# Modo MÍNIMO self-contained: monta uma ISO de teste e boota.
# =============================================================================
require_tool mksquashfs "sys-fs/squashfs-tools"
require_tool dracut "sys-kernel/dracut"
require_tool cc "sys-devel/gcc"

# Kernel instalado: exatamente um em /lib/modules (igual à guarda do Assembler).
mapfile -t KVERS < <(find /lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)
[ "${#KVERS[@]}" -eq 1 ] || die "esperava exatamente um kernel em /lib/modules; achei: ${KVERS[*]:-nenhum}"
KVER="${KVERS[0]}"
KERNEL="$(find_kernel)"
echo "smoke-iso: kernel $KVER → $KERNEL" >&2
[ "$LOCATE" -eq 1 ] && exit 0  # --locate: só confirma a descoberta e sai

# 1) rootfs mínimo: /sbin/init estático que sinaliza o sentinela e desliga, mais
#    os marcadores que o boot exige de um "root de verdade":
#      - /usr no topo: o dmsquash-live só aceita um squashfs cru como raiz com
#        /usr (ou /ostree); senão exige o layout aninhado LiveOS/rootfs.img e
#        aborta ("Failed to find a root filesystem").
#      - /etc/os-release: o systemd switch-root recusa um root sem ele ("does not
#        seem to be an OS tree").
#    Todo rootfs real do Assembler tem ambos (Gentoo usr-merged + os-release) →
#    image.py está correto; o furo era só do rootfs sintético do smoke-test.
ROOTFS="$WORK/rootfs"
mkdir -p "$ROOTFS"/{sbin,usr,etc,var,tmp,root}
printf 'NAME="bentoo-smoke"\nID=bentoo\nPRETTY_NAME="bentoo smoke-test"\nVERSION_ID="0"\n' \
    >"$ROOTFS/etc/os-release"
cat >"$WORK/init.c" <<'EOF'
/* /sbin/init mínimo: monta devtmpfs (a raiz live é overlay gravável), emite o
   sentinela na serial/console e desliga — prova que o dmsquash-live pivotou. */
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
    || die "falha ao compilar /sbin/init estático (cc -static disponível?)"

# 2) initramfs dracut dmsquash-live. Diferente do Assembler (que roda o dracut
#    num container stage3 limpo), aqui ele roda no HOST — então precisa ser
#    isolado da config do host: --conf /dev/null + --confdir vazio (ignora
#    /etc/dracut.conf{,.d}), --no-hostonly-cmdline (não embute rd.luks/rd.lvm do
#    host) e --omit dos módulos de descoberta de storage (crypt/lvm/mdraid/…). Sem
#    isso o initramfs herda o crypttab/LUKS do host e a VM trava no initqueue
#    esperando o disco cifrado do host (inexistente no QEMU). `dm` fica — o
#    dmsquash-live usa device-mapper no overlay. Ou um initramfs pronto via
#    --initramfs.
INITRAMFS="$WORK/initramfs.img"
if [ -n "$INITRAMFS_OVERRIDE" ]; then
    [ -r "$INITRAMFS_OVERRIDE" ] || die "--initramfs ilegível: $INITRAMFS_OVERRIDE"
    cp "$INITRAMFS_OVERRIDE" "$INITRAMFS"
else
    mkdir -p "$WORK/dracut.conf.d"  # confdir vazio → ignora a config do host
    dracut --add dmsquash-live --omit "crypt systemd-cryptsetup dmraid mdraid lvm multipath" \
        --no-hostonly --no-hostonly-cmdline \
        --conf /dev/null --confdir "$WORK/dracut.conf.d" \
        --force "$INITRAMFS" "$KVER" \
        || die "falha no dracut (dmsquash-live)"
fi

# 3) squashfs + ISO híbrida via as funções REAIS sob teste (shidashi.image).
ISO_OUT="$WORK/bentoo-smoke.iso"
SQUASHFS="$WORK/rootfs.squashfs"
( cd "$REPO_ROOT" && python3 - "$ROOTFS" "$SQUASHFS" "$ISO_OUT" "$KERNEL" "$INITRAMFS" <<'PY'
import sys
from pathlib import Path

from shidashi import image

rootfs, squashfs, iso, kernel, initramfs = (Path(a) for a in sys.argv[1:6])
image.make_squashfs(rootfs, squashfs)
image.build_iso(squashfs, iso, kernel=kernel, initramfs=initramfs)
print(f"smoke-iso: ISO montada via shidashi.image → {iso}", file=sys.stderr)
PY
) || die "falha ao montar a ISO via shidashi.image"

# 4) boot + assere o sentinela. Default: boot via grub (testa o bootloader);
#    --debug: boot direto com console=ttyS0 rd.shell para diagnóstico verboso.
if [ "$DEBUG" -eq 1 ]; then
    boot_and_check "$SENTINEL" \
        -kernel "$KERNEL" -initrd "$INITRAMFS" \
        -append "root=live:CDLABEL=$VOLID rd.live.image console=ttyS0 rd.shell" \
        -cdrom "$ISO_OUT"
else
    boot_and_check "$SENTINEL" -cdrom "$ISO_OUT" -boot d
fi
