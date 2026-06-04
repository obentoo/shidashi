"""Image — rootfs squashfs e live medium da ISO (OVERVIEW §7).

Duas etapas do ISO Assembler, no mesmo idioma de :mod:`shidashi.container`:
construtores de linha de comando **puros e inspecionáveis** (testáveis sem root)
e execuções *thin* sobre eles (``mksquashfs``/``grub-mkrescue``), que exigem as
ferramentas no host e são exercidas pelos testes host-gated.

* :func:`make_squashfs` comprime o rootfs num squashfs read-only (zstd -19).
* :func:`build_iso` monta a árvore do live medium — squashfs em
  ``LiveOS/squashfs.img`` (convenção do dracut ``dmsquash-live``), kernel +
  initramfs em ``boot/`` e um ``grub.cfg`` com a cmdline ``rd.live.image`` — e
  gera a ISO híbrida BIOS+UEFI com ``grub-mkrescue`` (que delega ao ``xorriso``).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

__all__ = ["ImageError", "VOLUME_ID", "build_iso", "make_squashfs"]

# Rótulo de volume da ISO; a cmdline do live boot referencia-o em
# ``root=live:CDLABEL=<VOLUME_ID>`` para o dmsquash-live achar o squashfs.
VOLUME_ID = "BENTOO"

# Layout do live medium dentro da ISO (convenção do dracut dmsquash-live).
_LIVEOS_IMG = "LiveOS/squashfs.img"
_ISO_KERNEL = "boot/vmlinuz"
_ISO_INITRD = "boot/initramfs.img"


class ImageError(Exception):
    """Falha ao gerar o squashfs ou a ISO (OVERVIEW §7).

    Levantada por ferramenta ausente no host (``mksquashfs``/``grub-mkrescue``) e
    por saída não-zero desses comandos — sempre com uma mensagem acionável que
    nomeia o comando e anexa o ``stderr`` capturado, em vez de propagar um
    ``CalledProcessError`` cru.
    """


def _require_tool(tool: str) -> None:
    """Levanta :class:`ImageError` se ``tool`` não está no ``PATH`` do host."""
    if shutil.which(tool) is None:
        raise ImageError(
            f"{tool!r} ausente no host; instale-o "
            "(montagem de ISO exige squashfs-tools + grub + xorriso)"
        )


def _run(argv: list[str]) -> None:
    """Executa ``argv`` capturando saída; embrulha falha em :class:`ImageError`."""
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as err:
        raise ImageError(f"comando falhou ({argv[0]}): {' '.join(argv)}\n{err.stderr}") from err


# Diretórios excluídos do squashfs (a raiz live): voláteis (não pertencem a uma
# imagem; podem capturar nós de dispositivo/resíduos do nspawn), o ``boot`` (o
# kernel+initramfs vão no ``boot/`` da ISO via :func:`_stage_iso_tree`, não na
# raiz — evita duplicá-los e ~dobrar o tamanho) e o ``var/cache/binpkgs`` (alvo
# de bind do binhost, externo à imagem). ``-e`` consome o resto do argv, logo vem
# por último.
_SQUASHFS_EXCLUDES = ("boot", "proc", "sys", "dev", "run", "var/cache/binpkgs")


def _mksquashfs_argv(rootfs: Path, output: Path, *, compression: str, level: int) -> list[str]:
    """Monta o argv do ``mksquashfs`` (OVERVIEW §7). **Pura**, sem efeitos.

    Forma: ``["mksquashfs", <rootfs>, <output>, "-comp", <compression>,
    "-Xcompression-level", <level>, "-noappend", "-no-progress", "-e",
    *_SQUASHFS_EXCLUDES]``. ``-noappend`` garante saída determinística (nunca
    anexa a um .squashfs preexistente), o nível de compressão é explícito
    (default 19, §7) e ``-e`` exclui voláteis + ``boot`` + o bind do binhost
    (ver :data:`_SQUASHFS_EXCLUDES`) — deve vir por último (consome o resto).
    """
    return [
        "mksquashfs",
        str(rootfs),
        str(output),
        "-comp",
        compression,
        "-Xcompression-level",
        str(level),
        "-noappend",
        "-no-progress",
        "-e",
        *_SQUASHFS_EXCLUDES,
    ]


def make_squashfs(
    rootfs: Path, output: Path, *, compression: str = "zstd", level: int = 19
) -> Path:
    """Comprime ``rootfs`` num squashfs read-only e devolve ``output`` (OVERVIEW §7).

    Exige ``mksquashfs`` no host; cria o diretório-pai de ``output`` e roda o
    comando de :func:`_mksquashfs_argv`. Uma saída não-zero vira :class:`ImageError`.
    """
    _require_tool("mksquashfs")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(_mksquashfs_argv(rootfs, output, compression=compression, level=level))
    return output


def _grub_cfg(*, volume_id: str, timeout: int = 10) -> str:
    """Renderiza o ``grub.cfg`` do live medium (dracut ``dmsquash-live``). **Pura**.

    A cmdline ``root=live:CDLABEL=<volume_id> rd.live.image`` instrui o módulo
    ``dmsquash-live`` do initramfs a montar ``LiveOS/squashfs.img`` da mídia
    rotulada ``<volume_id>`` como raiz overlay em RAM (OVERVIEW §7).
    """
    return (
        f"set timeout={timeout}\n"
        f'menuentry "bentoo (live)" {{\n'
        f"    linux /{_ISO_KERNEL} root=live:CDLABEL={volume_id} rd.live.image quiet\n"
        f"    initrd /{_ISO_INITRD}\n"
        f"}}\n"
    )


def _grub_mkrescue_argv(iso_root: Path, output: Path, *, volume_id: str) -> list[str]:
    """Monta o argv do ``grub-mkrescue`` → ISO híbrida (OVERVIEW §7). **Pura**.

    Forma: ``["grub-mkrescue", "-o", <output>, <iso_root>, "--", "-volid",
    <volume_id>]``. Tudo após ``--`` é repassado ao ``xorriso`` (backend do
    ``grub-mkrescue``); ``-volid`` fixa o rótulo de volume que a cmdline do
    :func:`_grub_cfg` referencia.
    """
    return ["grub-mkrescue", "-o", str(output), str(iso_root), "--", "-volid", volume_id]


def _stage_iso_tree(squashfs: Path, kernel: Path, initramfs: Path, iso_root: Path) -> None:
    """Popula ``iso_root`` com o layout do live medium (OVERVIEW §7).

    Copia o squashfs para ``LiveOS/squashfs.img``, o kernel e o initramfs para
    ``boot/`` e grava o ``boot/grub/grub.cfg`` de :func:`_grub_cfg`. Não roda
    nenhuma ferramenta externa — só I/O de arquivos — logo é testável sem root.
    """
    (iso_root / "LiveOS").mkdir(parents=True, exist_ok=True)
    (iso_root / "boot" / "grub").mkdir(parents=True, exist_ok=True)
    shutil.copy2(squashfs, iso_root / _LIVEOS_IMG)
    shutil.copy2(kernel, iso_root / _ISO_KERNEL)
    shutil.copy2(initramfs, iso_root / _ISO_INITRD)
    (iso_root / "boot" / "grub" / "grub.cfg").write_text(_grub_cfg(volume_id=VOLUME_ID))


def build_iso(squashfs: Path, output: Path, *, kernel: Path, initramfs: Path) -> Path:
    """Monta a ISO live híbrida a partir do squashfs e devolve ``output`` (OVERVIEW §7).

    Exige ``grub-mkrescue`` no host. Monta a árvore do live medium
    (:func:`_stage_iso_tree`: squashfs + ``kernel`` + ``initramfs`` produzidos
    pelo dracut ``dmsquash-live``) num diretório temporário e roda o
    ``grub-mkrescue`` de :func:`_grub_mkrescue_argv` → ISO híbrida BIOS+UEFI.
    Uma saída não-zero vira :class:`ImageError`.
    """
    _require_tool("grub-mkrescue")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shidashi-iso-") as tmp:
        iso_root = Path(tmp)
        _stage_iso_tree(squashfs, kernel, initramfs, iso_root)
        _run(_grub_mkrescue_argv(iso_root, output, volume_id=VOLUME_ID))
    return output
