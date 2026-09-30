"""Assembler — ISO Assembler: monta a ISO live a partir do binhost (OVERVIEW §7).

Metade leve do pipeline (OVERVIEW §5.3): seleciona e empacota, não compila. Para
uma :class:`~shidashi.recipe.ResolvedRecipe` e um binhost por arch, semeia um
stage3, sobrepõe os MESMOS layers de portage da Factory (de modo que a USE final
resolvida case com a gravada nos binpkgs — OVERVIEW §18.6), puxa a fatia do
flavor do binhost com ``emerge --usepkgonly`` (binário pronto, sem ordem de build
→ imune a ciclo, §18.6), gera o initramfs ``dmsquash-live`` com dracut, comprime
o rootfs em squashfs e produz a ISO híbrida (:mod:`shidashi.image`).

Imune a ciclo: ``--usepkgonly`` instala binário pronto e o match multi-instance
pega a instância certa por flavor pela USE final; toda a complexidade de ciclo
fica na Factory (OVERVIEW §7, §18.6). Os símbolos privilegiados de execução
(``fetch_stage3``/``extract_stage3``/``apply_portage``/``bind_repos`` e os de
:mod:`shidashi.image`) são globais do módulo, monkeypatcháveis nos testes; a
execução real (nspawn + emerge + dracut + mksquashfs + grub-mkrescue) exige root
e é exercida pelos testes host-gated.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from shidashi import config, image
from shidashi.container import Container
from shidashi.phases import (
    ISO_EMERGE_OPTIONS,
    ISO_SETTLE_OPTIONS,
    clear_use_break,
    image_cuts,
    image_targets,
    is_installed,
    write_cuts,
)
from shidashi.recipe import ResolvedRecipe
from shidashi.resolve import apply_portage, apply_rootfs, bind_repos, install_sets
from shidashi.seed import extract_stage3, fetch_stage3, load_pointer
from shidashi.tree import pinned_repos

__all__ = ["Assembler", "AssemblerError"]

# Alvo fixo do binhost dentro do container: o ``make.conf`` base aponta o PKGDIR
# para cá, então o binhost host-side é bind-montado sobre este caminho (igual à
# Factory, que monta o PKGDIR de saída no mesmo destino — OVERVIEW §6.3).
_BINHOST_DST = Path("/var/cache/binpkgs")


class AssemblerError(Exception):
    """Falha ao montar a ISO (OVERVIEW §7).

    Levantada pela guarda de root, por kernel/initramfs ausentes no rootfs após o
    emerge/dracut e por versão de kernel ambígua. Falhas de ``emerge``/``dracut``
    sobem como ``CalledProcessError`` do :class:`~shidashi.container.Container`; as
    de squashfs/ISO como :class:`shidashi.image.ImageError`.
    """


def _require_root() -> None:
    """Guarda de privilégio: levanta :class:`AssemblerError` se não-root.

    Primeira coisa que :meth:`Assembler.assemble` faz — antes de qualquer
    fetch/extração — espelhando :func:`shidashi.factory._require_root` (R8.1):
    nspawn + extração de stage3 + dracut exigem root e o Shidashi nunca escala
    privilégios sozinho.
    """
    if os.geteuid() != 0:
        raise AssemblerError(
            "shidashi assemble requer root (systemd-nspawn + extração de stage3 + dracut); "
            "rode como root — o Shidashi não escala privilégios sozinho"
        )


#: The stage3's leftovers out of the image. ``--with-bdeps=n``: an ISO carries
#: binaries only, like the ``--usepkgonly`` install that ignores build deps.
#: With the default (y), depclean keeps the stage3's build-only packages (perl
#: modules, autotools, docbook) as "required", finds their perl-5.42 gone
#: (the image has 5.44) and refuses to remove anything -- the third kde ISO,
#: 2026-09-30. Measured on that rootfs: required 1458 (= the image), 82 removed,
#: gcc-15.3.0 and binutils-2.46.1 among them; gcc-16.2.0 and binutils-2.47 kept.
ISO_DEPCLEAN_ARGV = ["emerge", "--depclean", "--with-bdeps=n"]


def _jobs_args(jobs: int | None) -> list[str]:
    """``--jobs N`` for emerge, or nothing. Pure.

    Nothing compiles here, so MAKEOPTS does not matter: what a job buys is
    unpacking and merging several binpkgs at once (the base turns on
    ``parallel-install``). Without it emerge merges one package at a time.
    """
    return ["--jobs", str(jobs)] if jobs is not None else []


def iso_settle_argv(atoms: tuple[str, ...], *, jobs: int | None = None) -> list[str]:
    """The settle of the cut packages: their final binpkgs. Pure."""
    return ["emerge", *ISO_SETTLE_OPTIONS, *_jobs_args(jobs), *atoms]


def iso_emerge_argv(recipe: ResolvedRecipe, *, jobs: int | None = None) -> list[str]:
    """Monta o argv do ``emerge --usepkgonly`` da ISO (OVERVIEW §7/§18.6/§9.3). **Pura**.

    Forma: ``["emerge", "--usepkgonly", "--emptytree", "--verbose", *alvos]``.
    ``--usepkgonly`` instala SÓ binpkgs do binhost (nunca compila → imune a ciclo,
    §18.6). ``--emptytree`` reinstala TODO o fecho de dependências dos alvos a
    partir do binhost — inclusive o ``@system`` — para que a base **não** fique
    com os binários genéricos/baseline do stage3 semente: numa ISO ``znver5`` o
    ``@system`` também vem arch-native, honrando o §7 ("puxa **tudo** do binhost")
    e o §9.3 (sem v3 vazando). É simétrico à fase ``rebuild`` da Factory
    (``--emptytree @world``), que garante o binhost completo que isto exige.

    Alvos: ``@system`` + os sets da receita (``@base``, os ``@extra-*`` que o
    flavor declara e ``@<flavor>``) — a base mais a fatia consumível; quando a
    receita não declara sets recai-se em ``@world`` (= ``@system`` + o que a base
    seedou).
    """
    return [
        "emerge", *ISO_EMERGE_OPTIONS, "--verbose", *_jobs_args(jobs),
        *image_targets(recipe.sets),
    ]


def _dracut_argv(kver: str, initramfs: Path) -> list[str]:
    """Monta o argv do ``dracut`` do live medium (OVERVIEW §7). **Pura**.

    Forma: ``["dracut", "--add", "dmsquash-live", "--no-hostonly", "--force",
    <initramfs>, <kver>]``. ``--add dmsquash-live`` embute o módulo que monta o
    squashfs como raiz overlay em RAM; ``--no-hostonly`` torna o initramfs
    genérico (a ISO precisa bootar em qualquer máquina, não só na de build).
    """
    return ["dracut", "--add", "dmsquash-live", "--no-hostonly", "--force", str(initramfs), kver]


def _kernel_version(rootfs: Path) -> str:
    """Descobre a versão do kernel instalada via ``${rootfs}/lib/modules/`` (OVERVIEW §7).

    Espera exatamente um diretório sob ``lib/modules`` (o kernel puxado do binhost
    pelo set ``boot``, universal via ``@base``); levanta :class:`AssemblerError` se
    houver zero (nenhum kernel) ou mais de um (ambíguo — qual bootar?).
    """
    modules = rootfs / "lib" / "modules"
    versions = sorted(p.name for p in modules.iterdir() if p.is_dir()) if modules.is_dir() else []
    if len(versions) != 1:
        raise AssemblerError(
            f"esperava exatamente um kernel em {modules}; encontrei {versions or 'nenhum'} "
            "(garanta que os sets puxem um único gentoo-kernel/dist-kernel do binhost)"
        )
    return versions[0]


def _locate_kernel(rootfs: Path, kver: str) -> Path:
    """Localiza o ``vmlinuz`` do kernel ``kver`` no rootfs (OVERVIEW §7).

    Tenta, em ordem: ``boot/vmlinuz-<kver>`` (convenção dist-kernel);
    ``usr/lib/modules/<kver>/vmlinuz``, where kernel-install keeps the image --
    the only place it is when installkernel[uki] (the base's SYSTEMD="boot uki
    ukify") writes a UKI to ``boot/EFI/Linux`` instead of ``boot/vmlinuz``; and
    qualquer ``boot/vmlinuz*``. Levanta :class:`AssemblerError` se não achar.
    The modules entry is a relative symlink into ``usr/src``; it must resolve
    inside the rootfs, never to the host.
    """
    boot = rootfs / "boot"
    candidate = boot / f"vmlinuz-{kver}"
    if candidate.is_file():
        return candidate
    modules = rootfs / "usr" / "lib" / "modules" / kver / "vmlinuz"
    if modules.is_file() and modules.resolve().is_relative_to(rootfs.resolve()):
        return modules
    globbed = sorted(boot.glob("vmlinuz*")) if boot.is_dir() else []
    if not globbed:
        raise AssemblerError(f"nenhum vmlinuz encontrado em {boot} (kernel não instalado?)")
    return globbed[0]


def _build_binds(
    binhost_dir: Path, repos_conf_dir: Path, *, repos: Mapping[str, Path] | None = None
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Monta os binds RO (repos + binhost) e RW (vazio) do container. **Pura**.

    O Assembler só LÊ — repos sincronizados do host (:func:`shidashi.resolve.bind_repos`)
    e o binhost por arch (montado sobre :data:`_BINHOST_DST`) entram **read-only**
    (``--usepkgonly`` não escreve no PKGDIR). Não há binds RW: o rootfs é mutado
    in-place pelo emerge/dracut, não via bind. ``bind_repos`` é global do módulo
    (monkeypatchável nos testes). ``repos`` are the pinned repositories the
    binpkgs were built from (D26): assembling against other trees would ask
    the binhost for versions it does not have.
    """
    binds_ro = bind_repos(repos_conf_dir, overrides=repos)
    binds_ro.append((binhost_dir, _BINHOST_DST))
    return binds_ro, []


def _install_sets(rootfs: Path, recipe: ResolvedRecipe) -> None:
    """Instala os sets da receita (OVERVIEW §13). Delega a :func:`resolve.install_sets`.

    Mantido como nome local porque os testes e o Assembler o importam daqui; a
    lógica vive num lugar só, compartilhada com a Factory (fonte única de USE,
    OVERVIEW §4.2).
    """
    install_sets(rootfs, recipe)


class Assembler:
    """Monta a ISO de uma receita resolvida a partir do binhost (OVERVIEW §7).

    Recebe a receita já resolvida e o ``binhost_dir`` (publish-pool host-side da
    arch) de onde puxar os binpkgs finais via ``--usepkgonly``.
    """

    def __init__(
        self, recipe: ResolvedRecipe, binhost_dir: Path, *, jobs: int | None = None
    ) -> None:
        self.recipe = recipe
        self.binhost_dir = binhost_dir
        #: emerge --jobs and mksquashfs -processors; None = emerge serial,
        #: mksquashfs on every CPU.
        self.jobs = jobs

    def assemble(self, output: Path, *, download: bool = True, keep: bool = False) -> Path:
        """Produz a ISO live em ``output`` e devolve o caminho gerado (OVERVIEW §7).

        Ordem:

        1. **Guarda de root** (:func:`_require_root`) — antes de qualquer trabalho.
        2. Resolve o pointer do stage3 (``load_pointer``), faz seed fresco da base
           genérica (``fetch_stage3`` + ``extract_stage3``) num rootfs de scratch.
           A microarquitetura entra pelos binpkgs do binhost, não pela base.
        3. ``apply_portage`` (os MESMOS layers da Factory → USE final idêntica,
           §18.6) + :func:`_install_sets`.
        4. Abre um :class:`Container` não-efêmero (repos + binhost RO) e roda o
           ``emerge --usepkgonly --emptytree`` de :func:`iso_emerge_argv` (puxa
           TUDO do binhost arch-native, incl. ``@system`` — §7/§9.3) seguido do
           ``dracut`` de :func:`_dracut_argv`.
        5. Localiza kernel + initramfs (:func:`_kernel_version`/:func:`_locate_kernel`),
           comprime o rootfs (:func:`shidashi.image.make_squashfs`) e monta a ISO
           (:func:`shidashi.image.build_iso`).
        6. Em sucesso e sem ``keep``, remove o rootfs de scratch e o squashfs
           intermediário (já copiado para a ISO); em falha ou ``keep``, preserva-os
           para depuração.
        """
        _require_root()

        recipe = self.recipe
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
        )
        key = f"{recipe.arch}-{recipe.flavor}-{recipe.init}"
        rootfs = config.scratch_dir() / "assemble" / key

        pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
        tarball = fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
        # a fresh stage3 into a fresh directory: a failed --keep run leaves its
        # rootfs, and extracting over it would inherit what that run left
        shutil.rmtree(rootfs, ignore_errors=True)
        extract_stage3(tarball, rootfs)

        apply_rootfs(rootfs, recipe, variants_dir=config.variants_dir())
        apply_portage(rootfs, recipe, variants_dir=config.variants_dir())
        _install_sets(rootfs, recipe)
        # the chain's cycle cuts, as the factory built under them: a fresh stage3
        # meets every cycle again, and the cut binpkgs are in the PKGDIR
        cuts = image_cuts(recipe)
        write_cuts(rootfs, cuts)

        binds_ro, binds_rw = _build_binds(
            self.binhost_dir, rootfs / "etc" / "portage" / "repos.conf", repos=repos
        )

        keep_rootfs = keep
        try:
            with Container(
                rootfs,
                ephemeral=False,
                binds=binds_ro,
                binds_rw=binds_rw,
                # installing ~1800 binpkgs takes a while: stream it, like the factory
                log=config.scratch_dir() / "logs" / f"assemble-{key}.log",
            ) as container:
                container.run(iso_emerge_argv(recipe, jobs=self.jobs))
                # the settle: the cut packages again, from their final binpkgs
                clear_use_break(rootfs)
                settle = tuple(sorted({c.atom for c in cuts if is_installed(rootfs, c.atom)}))
                if settle:
                    container.run(iso_settle_argv(settle, jobs=self.jobs))
                # The stage3 under the ISO keeps what the closure does not reach:
                # its own gcc and binutils slots, bootstrap leftovers. Measured on
                # the pipeline's minimal (2026-09-27): gcc-15.3.0, binutils-2.46.1,
                # autoconf-2.72-r7, rust-bin -- none needed by the image. The sets
                # are in world_sets, so depclean keeps everything the recipe asks.
                container.run(ISO_DEPCLEAN_ARGV)
                container.run(
                    ["emerge", *ISO_SETTLE_OPTIONS, *_jobs_args(self.jobs),
                     "@preserved-rebuild"]
                )
                kver = _kernel_version(rootfs)
                initramfs = rootfs / "boot" / f"initramfs-{kver}.img"
                container.run(_dracut_argv(kver, Path("/boot") / initramfs.name))

            kernel = _locate_kernel(rootfs, kver)
            squashfs = rootfs.parent / f"{key}.squashfs"
            image.make_squashfs(rootfs, squashfs, processors=self.jobs)
            iso = image.build_iso(squashfs, output, kernel=kernel, initramfs=initramfs)
        except BaseException:
            keep_rootfs = True  # preserva o rootfs para depuração em falha
            raise

        if not keep_rootfs:
            shutil.rmtree(rootfs, ignore_errors=True)
            squashfs.unlink(missing_ok=True)  # intermediário já copiado para a ISO
        return iso
