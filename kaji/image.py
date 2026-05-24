"""Image — construção do rootfs squashfs e do live medium da ISO (OVERVIEW §7).

Esqueleto da Fase 0: apenas as assinaturas públicas tipadas. Comprime o rootfs
em squashfs (zstd) read-only, gera o live boot com dracut ``dmsquash-live`` e
produz a ISO híbrida BIOS+UEFI com ``grub-mkrescue``/``xorriso`` (OVERVIEW §7).
Nada aqui executa ainda: cada corpo levanta ``NotImplementedError``.
"""

from pathlib import Path


def make_squashfs(rootfs: Path, output: Path, *, compression: str = "zstd") -> Path:
    """Comprime ``rootfs`` num squashfs read-only e devolve o caminho (OVERVIEW §7)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §7")


def build_iso(squashfs: Path, output: Path) -> Path:
    """Monta a ISO híbrida live a partir do squashfs e devolve o caminho (OVERVIEW §7)."""
    raise NotImplementedError("Fase 0 — ver OVERVIEW §7")
