"""Assembler — ISO Assembler: builds the live ISO from the binhost (OVERVIEW §7).

The light half of the pipeline (OVERVIEW §5.3): it selects and packages, it does not compile.
For a :class:`~shidashi.recipe.ResolvedRecipe` and a per-arch binhost, it seeds a
stage3, overlays the SAME portage layers as the Factory (so that the final
resolved USE matches the one recorded in the binpkgs — OVERVIEW §18.6), pulls the
flavor's slice from the binhost with ``emerge --usepkgonly`` (prebuilt binaries, no build order
→ immune to cycles, §18.6), generates the ``dmsquash-live`` initramfs with dracut, compresses
the rootfs into a squashfs and produces the hybrid ISO (:mod:`shidashi.image`). The squashfs
and ISO tools run in the toolbox stage's rootfs (:mod:`shidashi.toolbox`), not on the host.

Immune to cycles: ``--usepkgonly`` installs prebuilt binaries and the multi-instance match
picks the right instance per flavor by the final USE; all the cycle complexity
stays in the Factory (OVERVIEW §7, §18.6). The privileged execution symbols
(``fetch_stage3``/``extract_stage3``/``apply_portage``/``bind_repos`` and those of
:mod:`shidashi.image`) are module globals, monkeypatchable in the tests; the
real execution (nspawn + emerge + dracut + mksquashfs + grub-mkrescue) requires root
and is exercised by the host-gated tests.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shidashi import audit, binpkgs, checkpoint, config, image, publish, toolbox, world
from shidashi.container import Container
from shidashi.phases import (
    ISO_EMERGE_OPTIONS,
    ISO_SETTLE_OPTIONS,
    clear_use_break,
    image_cuts,
    image_targets,
    is_installed,
    judge_plan,
    parse_reused_atoms,
    write_cuts,
)
from shidashi.recipe import TOOLBOX_STAGE, ResolvedRecipe, UseBreak
from shidashi.resolve import apply_portage, apply_rootfs, bind_repos, install_sets, world_atoms
from shidashi.seed import Stage3Pointer, extract_stage3, fetch_stage3, load_pointer
from shidashi.system import (
    apply_live,
    apply_system,
    build_time_secrets,
    finalize,
    load_livecd,
    load_system_config,
    remove_generated_secrets,
    verify,
)
from shidashi.tree import load_pin_id, pinned_repos

__all__ = ["Assembler", "AssemblerError"]

# Fixed binhost target inside the container: the base ``make.conf`` points PKGDIR
# here, so the host-side binhost is bind-mounted over this path (same as the
# Factory, which mounts the output PKGDIR at the same destination — OVERVIEW §6.3).
_BINHOST_DST = Path("/var/cache/binpkgs")


@dataclass
class _Trunk:
    """The trunk an image branches from (:func:`trunk_stage`), as this assemble found it.

    ``mark`` is its checkpoint once restored or built; None while it still has
    to be installed, in this run, before the image's own packages.
    """

    assembler: Assembler
    key: str
    store: checkpoint.Store
    fps: checkpoint.Fingerprints
    cuts: tuple[UseBreak, ...]
    atoms: tuple[str, ...]
    mark: checkpoint.Mark | None


class AssemblerError(Exception):
    """Failure to build the ISO (OVERVIEW §7).

    Raised by the root guard, by a kernel/initramfs missing from the rootfs after
    emerge/dracut and by an ambiguous kernel version. ``emerge``/``dracut`` failures
    propagate as ``CalledProcessError`` from the :class:`~shidashi.container.Container`;
    squashfs/ISO ones as :class:`shidashi.image.ImageError`.
    """


def _require_root() -> None:
    """Privilege guard: raises :class:`AssemblerError` if not root.

    The first thing :meth:`Assembler.assemble` does — before any
    fetch/extraction — mirroring :func:`shidashi.factory._require_root` (R8.1):
    nspawn + stage3 extraction + dracut require root and Shidashi never escalates
    privileges on its own.
    """
    if os.geteuid() != 0:
        raise AssemblerError(
            "shidashi assemble requires root (systemd-nspawn + stage3 extraction + dracut); "
            "run as root — Shidashi does not escalate privileges on its own"
        )


#: The stage3's leftovers out of the image. ``--with-bdeps=n``: an ISO carries
#: binaries only, like the ``--usepkgonly`` install that ignores build deps.
#: With the default (y), depclean keeps the stage3's build-only packages (perl
#: modules, autotools, docbook) as "required", finds their perl-5.42 gone
#: (the image has 5.44) and refuses to remove anything -- the third kde ISO,
#: 2026-09-30. Measured on that rootfs: required 1458 (= the image), 82 removed,
#: gcc-15.3.0 and binutils-2.46.1 among them; gcc-16.2.0 and binutils-2.47 kept.
ISO_DEPCLEAN_ARGV = ["emerge", "--depclean", "--with-bdeps=n"]


def _depclean_count(output: str) -> int | None:
    """How many packages depclean removed ("Number removed: N"), if it says. Pure."""
    match = re.search(r"Number removed:\s+(\d+)", output)
    return int(match.group(1)) if match else None


def _emerge_log_merges(rootfs: Path, *, since: int) -> dict[str, dict[str, int]]:
    """This run's merges in the image's own emerge.log (empty if there is none)."""
    log = rootfs / "var" / "log" / "emerge.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return audit.parse_emerge_log(text, since=since)


def _tree_bytes(path: Path) -> int:
    """Apparent size of a tree (``du -sb``), for the compression ratio; 0 if unknown."""
    try:
        done = subprocess.run(["du", "-sxb", str(path)], capture_output=True, text=True, check=True)
    except OSError, subprocess.CalledProcessError:
        return 0
    return int(done.stdout.split()[0])


def _jobs_args(jobs: int | None) -> list[str]:
    """``--jobs N`` for emerge, or nothing. Pure.

    Nothing compiles here, so MAKEOPTS does not matter: what a job buys is
    unpacking and merging several binpkgs at once (the base turns on
    ``parallel-install``). Without it emerge merges one package at a time.
    """
    return ["--jobs", str(jobs)] if jobs is not None else []


#: The layers whose ``rootfs/`` the install needs: the base's ``locale.gen``,
#: which ``locale-gen`` reads while glibc merges. Every other layer's ``rootfs/``
#: lands after preserved-rebuild, so the checkpoints carry none of it and are
#: shared between images (``worker`` resumes from ``minimal``'s); it also wins
#: over a package's own file, with no ``._cfg`` left behind.
INSTALL_ROOTFS_LAYERS = ("base",)

#: The resume of an interrupted install: Portage replays its own saved command.
ISO_RESUME_ARGV = ["emerge", "--resume"]

#: ``>>> Completed (3 of 120) cat/pkg-1.0::gentoo``: a merge that went through.
_COMPLETED = re.compile(r"^>>> Completed \(\d+ of \d+\) ([^\s:]+)")


def merged_reused_atoms(emerge_output: str) -> tuple[str, ...]:
    """The binpkgs a stopped ``emerge`` merged before it stopped, as
    :func:`~shidashi.phases.parse_reused_atoms` names them. Pure.

    Its ``[binary ...]`` lines are the whole plan, printed before the first merge;
    only the plan entries with a ``Completed`` line were merged.
    """
    done = {
        m.group(1)
        for m in (_COMPLETED.match(line.strip()) for line in emerge_output.splitlines())
        if m is not None
    }
    return tuple(a for a in parse_reused_atoms(emerge_output) if checkpoint.token_cpv(a) in done)


#: Portage moves a binpkg that fails its size or digest check aside as
#: ``<file>._checksum_failure_.<random>``; on the read-only binhost the move itself
#: fails, in a traceback, so the name is the only trace either way.
_CHECKSUM_FAILURE = re.compile(re.escape(str(_BINHOST_DST)) + r"/(\S+?)\._checksum_failure_")


#: ``pkg-1.0``, ``pkg-1.0-r1``: a PF split into PN and PV (a version starts with a
#: digit and holds no hyphen but its ``-rN`` revision).
_PF = re.compile(r"^(?P<pn>.+?)-(?P<pv>\d[^-]*(?:-r\d+)?)$")


def binpkgs_outside_tree(plan: Sequence[str], repos: Mapping[str, Path]) -> tuple[str, ...]:
    """The plan's binpkgs whose ebuild the pinned trees lack, as ``cat/pkg-1.0::repo``. I/O.

    ``plan`` holds :func:`~shidashi.checkpoint.plan_tokens` (``cat/pkg-1.0-1:slot::repo``),
    ``repos`` the pinned trees by name. A binpkg from a repository that is not
    pinned is outside too. Behind ``--use-ebuild-visibility``, which already keeps
    emerge to the tree: this names what slipped through, if anything ever does.
    """
    outside: list[str] = []
    for token in plan:
        repo = token.rsplit("::", 1)[1] if "::" in token else ""
        tree = repos.get(repo)
        # with its build id (``-1``) or, from a binhost without one, as it is
        cpvs = dict.fromkeys((checkpoint.token_cpv(token), token.split(":", 1)[0]))
        if tree is None or not any(_has_ebuild(tree, cpv) for cpv in cpvs):
            outside.append(f"{checkpoint.token_cpv(token)}::{repo}")
    return tuple(outside)


def _has_ebuild(tree: Path, cpv: str) -> bool:
    """Whether ``tree`` holds the ebuild of ``cat/pkg-1.0``. I/O."""
    category, _, pf = cpv.partition("/")
    match = _PF.match(pf)
    return match is not None and (tree / category / match["pn"] / f"{pf}.ebuild").is_file()


def corrupt_binpkgs(emerge_output: str) -> tuple[str, ...]:
    """The binpkgs (paths in the binhost) Portage found corrupt or truncated. Pure."""
    return tuple(dict.fromkeys(_CHECKSUM_FAILURE.findall(emerge_output)))


def iso_settle_argv(atoms: tuple[str, ...], *, jobs: int | None = None) -> list[str]:
    """The settle of the cut packages: their final binpkgs. Pure."""
    return ["emerge", *ISO_SETTLE_OPTIONS, *_jobs_args(jobs), *atoms]


#: The branch of an image off its trunk: the image's sets on top of the trunk's
#: installed state. ``--newuse`` swaps the trunk's packages whose USE the flavor
#: changes (``kde qt6``, ``gtk gnome``) for their binpkgs of that flavor.
ISO_BRANCH_OPTIONS = (
    "--usepkgonly",
    "--binpkg-respect-use=y",
    "--use-ebuild-visibility=y",
    "--update",
    "--deep",
    "--newuse",
)


def trunk_stage(recipe: ResolvedRecipe) -> str | None:
    """The stage ``recipe``'s image branches from, or None. Pure.

    The deepest intermediate stage that ships no image of its own: ``desktop``
    for kde, gnome and wm -- identical for the three, so it is installed once
    and each flavor grows from it. The base never is one (nothing ships
    without minimal), and neither is a shipping stage: minimal and worker
    install their whole configuration from the stage3 (and share it already).
    """
    shipping = {phase.stage for phase in recipe.phases if phase.ships}
    for stage in reversed(recipe.stages[1:-1]):
        if stage not in shipping:
            return stage
    return None


def iso_branch_argv(recipe: ResolvedRecipe, *, jobs: int | None = None) -> list[str]:
    """The image's install on top of its trunk (:data:`ISO_BRANCH_OPTIONS`). Pure."""
    return [
        "emerge",
        *ISO_BRANCH_OPTIONS,
        "--verbose",
        *_jobs_args(jobs),
        *image_targets(recipe.sets),
    ]


def iso_emerge_argv(recipe: ResolvedRecipe, *, jobs: int | None = None) -> list[str]:
    """Build the argv of the ISO's ``emerge --usepkgonly`` (OVERVIEW §7/§18.6/§9.3). **Pure**.

    Shape: ``["emerge", "--usepkgonly", "--emptytree", "--verbose", *targets]``.
    ``--usepkgonly`` installs ONLY binpkgs from the binhost (never compiles → immune to cycles,
    §18.6). ``--emptytree`` reinstalls the WHOLE dependency closure of the targets
    from the binhost — including ``@system`` — so that the base does **not** keep
    the generic/baseline binaries of the seed stage3: in a ``znver5`` ISO,
    ``@system`` also comes arch-native, honoring §7 ("pulls **everything** from the binhost")
    and §9.3 (no v3 leaking). It is symmetric to the Factory's ``rebuild`` phase
    (``--emptytree @world``), which guarantees the complete binhost this requires.

    Targets: ``@system`` + the recipe's sets (``@base``, the ``@extra-*`` that the
    flavor declares and ``@<flavor>``) — the base plus the consumable slice; when the
    recipe declares no sets it falls back to ``@world`` (= ``@system`` + what the base
    seeded).
    """
    return [
        "emerge",
        *ISO_EMERGE_OPTIONS,
        "--verbose",
        *_jobs_args(jobs),
        *image_targets(recipe.sets),
    ]


def _dracut_argv(kver: str, initramfs: Path) -> list[str]:
    """Build the ``dracut`` argv for the live medium (OVERVIEW §7). **Pure**.

    Shape: ``["dracut", "--add", "dmsquash-live", "--no-hostonly", "--force",
    <initramfs>, <kver>]``. ``--add dmsquash-live`` embeds the module that mounts the
    squashfs as an overlay root in RAM; ``--no-hostonly`` makes the initramfs
    generic (the ISO has to boot on any machine, not only the build one).

    ``--omit systemd-modules-load``: that dracut module copies the image's
    modules-load.d into the initramfs but not the out-of-tree modules they name,
    so every boot logged "Failed to find module 'vboxdrv'" (x3) from the initrd
    while the real root loaded them fine (seen by the boot test, 2026-09-30).
    The live boot's own modules (squashfs, overlay, isofs, loop) are loaded by
    dmsquash-live, not through modules-load.d.
    """
    return [
        "dracut",
        "--add",
        "dmsquash-live",
        "--omit",
        "systemd-modules-load",
        "--no-hostonly",
        "--force",
        str(initramfs),
        kver,
    ]


def ships_nvidia_driver(rootfs: Path) -> bool:
    """Whether the image installed x11-drivers/nvidia-drivers (its vdb entry). I/O.

    Decides the "open NVIDIA driver" boot entry: only that package blacklists
    nouveau, so only its images need a way back to it.
    """
    vdb = rootfs / "var" / "db" / "pkg" / "x11-drivers"
    return vdb.is_dir() and any(vdb.glob("nvidia-drivers-[0-9]*"))


def _kernel_version(rootfs: Path) -> str:
    """Find the installed kernel version via ``${rootfs}/lib/modules/`` (OVERVIEW §7).

    Expects exactly one directory under ``lib/modules`` (the kernel pulled from the binhost
    by the ``boot`` set, universal via ``@base``); raises :class:`AssemblerError` if
    there are zero (no kernel) or more than one (ambiguous — which one to boot?).
    """
    modules = rootfs / "lib" / "modules"
    versions = sorted(p.name for p in modules.iterdir() if p.is_dir()) if modules.is_dir() else []
    if len(versions) != 1:
        raise AssemblerError(
            f"expected exactly one kernel in {modules}; found {versions or 'none'} "
            "(make sure the sets pull a single gentoo-kernel/dist-kernel from the binhost)"
        )
    return versions[0]


def _locate_kernel(rootfs: Path, kver: str) -> Path:
    """Locate the ``vmlinuz`` of kernel ``kver`` in the rootfs (OVERVIEW §7).

    Tries, in order: ``boot/vmlinuz-<kver>`` (dist-kernel convention);
    ``usr/lib/modules/<kver>/vmlinuz``, where kernel-install keeps the image --
    the only place it is when installkernel[uki] (the base's SYSTEMD="boot uki
    ukify") writes a UKI to ``boot/EFI/Linux`` instead of ``boot/vmlinuz``; and
    any ``boot/vmlinuz*``. Raises :class:`AssemblerError` if none is found.
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
        raise AssemblerError(f"no vmlinuz found in {boot} (kernel not installed?)")
    return globbed[0]


def _build_binds(
    binhost_dir: Path, repos_conf_dir: Path, *, repos: Mapping[str, Path]
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Build the container's RO (repos + binhost) and RW (empty) binds. **Pure**.

    The Assembler only READS — the pinned repos (:func:`shidashi.resolve.bind_repos`)
    and the per-arch binhost (mounted over :data:`_BINHOST_DST`) go in **read-only**
    (``--usepkgonly`` does not write to the PKGDIR). There are no RW binds: the rootfs is mutated
    in place by emerge/dracut, not through a bind. ``bind_repos`` is a module global
    (monkeypatchable in the tests). ``repos`` are the pinned repositories the
    binpkgs were built from (D26): assembling against other trees would ask
    the binhost for versions it does not have.
    """
    binds_ro = bind_repos(repos_conf_dir, pinned=repos)
    binds_ro.append((binhost_dir, _BINHOST_DST))
    return binds_ro, []


def _install_sets(rootfs: Path, recipe: ResolvedRecipe) -> None:
    """Install the recipe's sets (OVERVIEW §13). Delegates to :func:`resolve.install_sets`.

    Kept as a local name because the tests and the Assembler import it from here; the
    logic lives in a single place, shared with the Factory (single source of USE,
    OVERVIEW §4.2).
    """
    install_sets(rootfs, recipe)


class AssembleResult(BaseModel):
    """What an assemble produced: the ISOs and every artifact beside them."""

    model_config = ConfigDict(frozen=True)
    name: str
    isos: tuple[Path, ...]
    artifacts: tuple[Path, ...]


def write_squashfs_exclude(patterns: tuple[str, ...], dest: Path) -> Path:
    """``livecd.yaml``'s ``squashfs_exclude``, one pattern per line, for
    ``mksquashfs -ef``. I/O."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(f"{p}\n" for p in patterns), encoding="utf-8")
    return dest


def build_info(
    recipe: ResolvedRecipe,
    *,
    name: str,
    build_id: str,
    kernel: str,
    compression: str,
    volume: str,
    packages: int,
    world_atoms: int,
    stage3: Mapping[str, object],
    tools: Mapping[str, str],
) -> dict[str, object]:
    """``bentoo/build.json`` on the medium: what this image is and what made it. Pure."""
    return {
        "name": name,
        "build_id": build_id,
        "flavor": recipe.flavor,
        "init": recipe.init,
        "arch": recipe.arch,
        "profile": recipe.profile,
        "kernel": kernel,
        "compression": compression,
        "volume": volume,
        "packages": packages,
        "world": world_atoms,
        "stage3": dict(stage3),
        # the versions of what made the squashfs and the ISO (the toolbox's)
        "toolbox": dict(tools),
        "repository": audit.repo_state(),
    }


class Assembler:
    """Assembles the ISO of a resolved recipe from the generation's binpkgs (OVERVIEW §7)."""

    def __init__(
        self, recipe: ResolvedRecipe, binhost_dir: Path, *, jobs: int | None = None
    ) -> None:
        self.recipe = recipe
        self.binhost_dir = binhost_dir
        #: emerge --jobs, mksquashfs/unsquashfs -processors and the stage4's
        #: xz -T; None = emerge serial, the others on every CPU.
        self.jobs = jobs

    def _fingerprints(
        self,
        *,
        pointer: Stage3Pointer,
        repos: Mapping[str, Path],
        cuts: Sequence[UseBreak],
        atoms: Sequence[str],
    ) -> checkpoint.Fingerprints:
        """The checkpoints' fingerprints: everything the installed state depends on. I/O.

        The configuration is RENDERED -- the same ``configure`` the image gets,
        into a temporary directory -- and its result hashed: a layer that
        configures nothing (``worker``) leaves it unchanged, whatever its name.
        Then the stage3, the profile and the pins (seeds/, the pinned
        repositories' paths, which carry their snapshot or commit) and the
        install's command line without ``--jobs`` (it changes the speed, not the
        result). The binhost is left out: :meth:`_still_valid` checks a checkpoint
        against it by what the resolver would choose today.
        """
        recipe = self.recipe
        with tempfile.TemporaryDirectory(prefix="shidashi-config-") as tmp:
            rendered = Path(tmp)
            self._configure(rendered, cuts=cuts, atoms=atoms)
            config_digest = checkpoint.tree_digest([rendered])
        inputs = (
            checkpoint.FORMAT,
            pointer.model_dump(mode="json"),
            recipe.profile,
            {name: str(path) for name, path in sorted(repos.items())},
            checkpoint.tree_digest([config.seeds_dir()]),
            config_digest,
            iso_emerge_argv(recipe),
        )
        # the binhost is not here: a checkpoint is validated against it by what the
        # resolver chooses (:meth:`_still_valid`), not by the whole index
        install = checkpoint.fingerprint(checkpoint.INSTALL, *inputs)
        return checkpoint.Fingerprints(
            install=install,
            packages=checkpoint.fingerprint(
                checkpoint.PACKAGES, install, ISO_DEPCLEAN_ARGV, list(ISO_SETTLE_OPTIONS)
            ),
            partial=checkpoint.fingerprint(checkpoint.PARTIAL, *inputs),
        )

    def _plan_data(self, plan: Sequence[str]) -> dict[str, object]:
        """What a checkpoint records of the binhost: the plan and its index slice. I/O."""
        cpvs = {checkpoint.token_cpv(t) for t in plan}
        return {
            "plan": list(plan),
            "binhost": checkpoint.binhost_slice(self.binhost_dir / "Packages", cpvs),
        }

    def _still_valid(
        self, mark: checkpoint.Mark, rootfs: Path, *, repos: Mapping[str, Path]
    ) -> str | None:
        """Why the restored checkpoint ``mark`` no longer matches the binhost, or None. I/O.

        Valid while the resolver would choose today exactly the binpkgs it
        installed: first the index entries of those packages (a rebuilt or new
        instance changes them), then ``emerge --pretend`` with the install's own
        command line, run in the restored rootfs (``--emptytree`` resolves as if
        nothing were installed). Every other entry of the index is ignored, so a
        binpkg rebuilt for kde leaves minimal's checkpoint alone.
        """
        plan = tuple(mark.data.get("plan") or ())
        if not plan:
            return "no plan recorded"
        slice_now = checkpoint.binhost_slice(
            self.binhost_dir / "Packages", {checkpoint.token_cpv(t) for t in plan}
        )
        if slice_now != mark.data.get("binhost"):
            return "the binhost entries of its packages changed"
        binds_ro, binds_rw = _build_binds(
            self.binhost_dir, rootfs / "etc" / "portage" / "repos.conf", repos=repos
        )
        with Container(rootfs, ephemeral=False, binds=binds_ro, binds_rw=binds_rw) as container:
            pretended = container.run([*iso_emerge_argv(self.recipe), "--pretend"])
        chosen = checkpoint.plan_tokens(pretended.stdout + pretended.stderr)
        # the same binpkgs, counted; the order is the display's, not the result's
        # (the install ran with --jobs, the pretend without: measured 2026-10-05)
        if sorted(chosen) != sorted(plan):
            changed = sorted(set(chosen) ^ set(plan)) or ["(the same binpkgs, other counts)"]
            return f"the resolver chooses other binpkgs: {', '.join(changed[:5])}"
        return None

    def _find_trunk(
        self,
        *,
        pointer: Stage3Pointer,
        repos: Mapping[str, Path],
        store: checkpoint.Store,
        rootfs: Path,
        stale: list[str],
    ) -> _Trunk | None:
        """The image's trunk, restored into ``rootfs`` when its checkpoint holds. I/O.

        None when the image has no trunk. A trunk checkpoint is validated like
        any other, against the trunk's own command line; a stale one is dropped
        and the trunk is installed again by this run.
        """
        name = trunk_stage(self.recipe)
        if name is None:
            return None
        recipe = self.recipe
        trunk_recipe = config.load_recipe(recipe.arch, name, recipe.init, any_stage=True)
        assembler = Assembler(trunk_recipe, self.binhost_dir, jobs=self.jobs)
        # a stage, not an image: its world comes from the kits, no committed file
        cuts = image_cuts(trunk_recipe)
        atoms = world_atoms(trunk_recipe)
        fps = assembler._fingerprints(pointer=pointer, repos=repos, cuts=cuts, atoms=atoms)
        key = f"{recipe.arch}-{name}-{recipe.init}"
        mark = store.find(checkpoint.INSTALL, fps.install)
        if mark is not None:
            store.restore(mark, rootfs)
            reason = assembler._still_valid(mark, rootfs, repos=repos)
            if reason is None:
                mark = store.claim(mark, key)
            else:
                stale.append(f"trunk {name}: {reason}")
                store.drop(mark)
                mark = None
        return _Trunk(assembler, key, store, fps, cuts, atoms, mark)

    def _configure(self, rootfs: Path, *, cuts: Sequence[UseBreak], atoms: Sequence[str]) -> None:
        """Write the image's install-time configuration into ``rootfs``. I/O.

        The base's ``rootfs/`` (:data:`INSTALL_ROOTFS_LAYERS`), the composed
        ``/etc/portage``, the sets, the cuts and the world: what the install reads.
        """
        recipe = self.recipe
        apply_rootfs(
            rootfs, recipe, variants_dir=config.variants_dir(), layers=INSTALL_ROOTFS_LAYERS
        )
        # no host_jobs: this rootfs is the image, its make.conf the user's;
        # the assemble's own --jobs goes on the emerge command lines
        apply_portage(rootfs, recipe, variants_dir=config.variants_dir(), host_jobs=False)
        _install_sets(rootfs, recipe)
        write_cuts(rootfs, tuple(cuts))
        world.write_to_image(rootfs, tuple(atoms))

    def _refuse_stale_plan(
        self,
        container: Container,
        recipe: ResolvedRecipe,
        *,
        image: ResolvedRecipe | None = None,
    ) -> tuple[str, ...] | None:
        """Stop before the install when the image's plan holds a stale binpkg (R2.1, R2.2).

        One ``--pretend`` of ``recipe``'s install, judged by the subslot and the
        soname rules (:func:`shidashi.phases.judge_plan`, the factory's
        ``check-binpkgs`` judge). Returns the plan's tokens, so a branched install
        does not resolve it a second time; ``None`` without an index -- nothing to
        judge, and the install runs and fails as today on a missing binhost.

        With ``image``, ``recipe`` is that image's trunk, judged before the trunk is
        installed: the refusal and its rebuild hint name the image being assembled.
        """
        shown = image or recipe
        index = self.binhost_dir / "Packages"
        try:
            text = index.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except OSError as err:
            raise AssemblerError(f"cannot read the binhost index {index}: {err}") from err
        pretended = container.run([*iso_emerge_argv(recipe), "--pretend"])
        plan_text = pretended.stdout + pretended.stderr
        try:
            by_subslot, by_soname = judge_plan(container, binpkgs.parse_index(text), plan_text)
        except binpkgs.BinpkgError as err:
            raise AssemblerError(f"{err}:\n{err.output.strip()}") from err
        if by_subslot or by_soname:
            detail = binpkgs.describe(
                by_subslot, by_soname, arch=shown.arch, image=shown.flavor, init=shown.init
            )
            what = (
                f"the {shown.flavor} image"
                if image is None
                else f"the {recipe.flavor} trunk of the {image.flavor} image"
            )
            raise AssemblerError(
                f"{what} would install binpkgs built against a library "
                f"its tree no longer ships; nothing was installed.\n{detail}"
            )
        return checkpoint.plan_tokens(plan_text)

    def assemble(
        self,
        output_dir: Path,
        *,
        download: bool = True,
        keep: bool = False,
        compressions: Sequence[str] = ("zstd",),
        stage4: bool = False,
        sbom: bool = True,
        fresh: bool = False,
        trunk: bool = True,
        now: datetime.datetime | None = None,
    ) -> AssembleResult:
        """Build the live ISO(s) of the recipe into ``output_dir``. PRIVILEGED.

        In order, each an audited step: seed a fresh stage3; configure it (layers,
        sets, cuts, world, system.yaml validated); install everything from binpkgs
        under the cuts, then settle them; depclean; preserved-rebuild; configure
        the system (Handbook), make the optional stage4, add the live layer and
        verify both; the initramfs; the SBOM; then per compression profile the
        squashfs, the ISO and its published artifacts (DIGESTS, SHA256SUMS,
        package list, contents). On success without ``keep`` the scratch rootfs
        and squashfs go; on failure or ``keep`` they stay for debugging.

        On a btrfs scratch the rootfs is a subvolume, frozen after the install
        and after preserved-rebuild (:mod:`shidashi.checkpoint`); a failed install
        is frozen too, with Portage's resume list. The next run of the same image
        resumes from the deepest checkpoint whose inputs are unchanged, so a
        failure after the install never pays it again. ``fresh`` drops the image's
        own, restores none (not those another image still holds, not the trunk)
        and installs the image whole from the stage3.

        With ``trunk`` (the default) a flavor without a checkpoint of its own
        grows from its trunk's (:func:`trunk_stage`, ``desktop``): the trunk is
        installed from the stage3 once, frozen, and every flavor adds its packages
        on top of it (``trunk`` step, :data:`ISO_BRANCH_OPTIONS`).
        """
        _require_root()
        run = audit.current()
        recipe = self.recipe
        if recipe.flavor == TOOLBOX_STAGE:
            raise AssemblerError("the toolbox is a build stage, not an image: nothing to assemble")
        key = f"{recipe.arch}-{recipe.flavor}-{recipe.init}"
        rootfs = config.scratch_dir() / "assemble" / key
        when = now or datetime.datetime.now(datetime.UTC)
        name = publish.release_name(recipe, when)
        # the squashfs and the stage4 leave out livecd.yaml's squashfs_exclude
        exclude_file = write_squashfs_exclude(
            load_livecd(config.variants_dir()).squashfs_exclude,
            rootfs.parent / f"{key}.squashfs-exclude",
        )
        for profile in compressions:
            if profile not in image.COMPRESSION:
                raise AssemblerError(f"unknown compression {profile!r}")

        with run.step("seed") as step:
            repos = pinned_repos(
                seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=download
            )
            pointer = load_pointer(recipe.init, seeds_dir=config.seeds_dir())
            # the toolbox built from this tree: one of an older pin is not it (D7)
            pins = load_pin_id(config.seeds_dir())
            # checked now, not after the half-hour install that needs it
            toolbox_tar = toolbox.tarball_path(
                recipe,
                snapshot=pointer.snapshot,
                pins=pins,
                fork_points_dir=config.fork_points_dir(),
            )
            if not toolbox_tar.is_file():
                raise AssemblerError(
                    f"no toolbox for {recipe.arch}/{recipe.init} at {toolbox_tar}; build it "
                    f"first: shidashi factory {recipe.arch} {TOOLBOX_STAGE} {recipe.init}"
                )
            # the chain's cycle cuts, as the factory built under them: a fresh stage3
            # meets every cycle again, and the cut binpkgs are in the PKGDIR
            cuts = image_cuts(recipe)
            # the flat package list, as committed in variants/<stage>/world.<init>
            atoms = world.current_atoms(recipe, config.variants_dir())
            # one store per arch and init: every image of it shares the checkpoints
            store = checkpoint.open_store(
                config.scratch_dir() / "assemble" / "checkpoints" / f"{recipe.arch}-{recipe.init}"
            )
            backend = store.backend if store is not None else None
            # computed only with a store: rendering the configuration has a cost
            fps = (
                self._fingerprints(pointer=pointer, repos=repos, cuts=cuts, atoms=atoms)
                if store is not None
                else None
            )
            if store is not None and fps is not None:
                if fresh:
                    store.release(key)
                else:
                    store.retain(key, (fps.install, fps.packages, fps.partial))
            resumed: checkpoint.Mark | None = None
            stale: list[str] = []
            # --fresh restores nothing: neither a checkpoint another image still
            # holds nor the trunk; the image installs whole from the stage3
            if store is not None and fps is not None and not fresh:
                step.add(pruned=store.prune())
                # deepest first; a stale one goes (it is stale for every image
                # sharing it: the same inputs, the same binhost) and the next is tried
                while (resumed := checkpoint.resume_point(store, fps)) is not None:
                    store.restore(resumed, rootfs)
                    reason = (
                        None
                        if resumed.step == checkpoint.PARTIAL
                        else self._still_valid(resumed, rootfs, repos=repos)
                    )
                    if reason is None:
                        break
                    stale.append(f"{resumed.step}: {reason}")
                    store.drop(resumed)
            branch: _Trunk | None = None
            if resumed is None and trunk and not fresh and store is not None:
                branch = self._find_trunk(
                    pointer=pointer, repos=repos, store=store, rootfs=rootfs, stale=stale
                )
            if stale:
                step.add(stale=stale)
            if store is not None and resumed is not None:
                resumed = store.claim(resumed, key)
                step.add(
                    restored=resumed.step,
                    restored_from=resumed.run_id,
                    shared_with=[i for i in resumed.images if i != key],
                )
            elif branch is not None and branch.mark is not None:
                step.add(trunk=branch.key, trunk_restored_from=branch.mark.run_id)
            else:
                tarball = fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
                # a fresh stage3 into a fresh directory: a failed --keep run leaves its
                # rootfs, and extracting over it would inherit what that run left
                checkpoint.remove_tree(rootfs, backend)
                if backend is not None:
                    backend.create(rootfs)
                extract_stage3(tarball, rootfs)
            step.add(
                stage3=pointer.filename,
                stage3_sha512=pointer.sha512,
                rootfs=str(rootfs),
                checkpoints="on" if store is not None else "off (scratch is not btrfs)",
            )

        with run.step("configure") as step:
            if resumed is None and branch is None:
                self._configure(rootfs, cuts=cuts, atoms=atoms)
            elif resumed is None and branch is not None and branch.mark is None:
                # the trunk's own configuration first: the image's comes after it
                branch.assembler._configure(rootfs, cuts=branch.cuts, atoms=branch.atoms)
            # loaded (and validated) now: a broken system.yaml fails before the
            # half-hour install, not after it
            system_cfg = load_system_config(recipe, variants_dir=config.variants_dir())
            run.attach("world", list(atoms))
            step.add(
                world=len(atoms),
                system=system_cfg.model_dump(exclude={"live": {"password"}}),
                layers=list(recipe.portage_layers),
                sets=list(recipe.sets),
                cuts=[f"{c.atom} {'' if c.enable else '-'}{c.flag}" for c in cuts],
                # a restored checkpoint already holds this configuration
                applied=resumed is None,
            )

        binds_ro, binds_rw = _build_binds(
            self.binhost_dir, rootfs / "etc" / "portage" / "repos.conf", repos=repos
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[Path] = []
        isos: list[Path] = []
        squashfs_files: list[Path] = []

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
                # what a restored checkpoint spares: the install (and its settle),
                # or everything up to preserved-rebuild
                installed_state = resumed is not None and resumed.step in (
                    checkpoint.INSTALL,
                    checkpoint.PACKAGES,
                )
                final_packages = resumed is not None and resumed.step == checkpoint.PACKAGES
                # the cuts the image's own install and settle use: off a trunk,
                # only those the trunk did not already install and settle
                branch_cuts = cuts
                trunk_since, trunk_reused = 0, ()
                if branch is not None:
                    with run.step("trunk") as step:
                        if branch.mark is None:
                            # judged before the trunk installs: a stale binpkg of the
                            # trunk would otherwise be merged, the whole trunk with it,
                            # before the image's judgement below could refuse (R2.2)
                            if resumed is None:
                                trunk_judged = self._refuse_stale_plan(
                                    container, branch.assembler.recipe, image=recipe
                                )
                                step.add(
                                    stale_check="judged" if trunk_judged is not None else "no index"
                                )
                            trunk_since = int(time.time())
                            built = container.run(
                                iso_emerge_argv(branch.assembler.recipe, jobs=self.jobs)
                            )
                            text = built.stdout + built.stderr
                            clear_use_break(rootfs)
                            trunk_settle = tuple(
                                sorted(
                                    {c.atom for c in branch.cuts if is_installed(rootfs, c.atom)}
                                )
                            )
                            if trunk_settle:
                                container.run(iso_settle_argv(trunk_settle, jobs=self.jobs))
                            branch.mark = branch.store.save(
                                rootfs,
                                checkpoint.INSTALL,
                                branch.fps.install,
                                image=branch.key,
                                run_id=run.run_id,
                                data={
                                    "since": trunk_since,
                                    "reused": list(parse_reused_atoms(text)),
                                    **branch.assembler._plan_data(checkpoint.plan_tokens(text)),
                                },
                            )
                            step.add(built=branch.key, checkpoint=checkpoint.INSTALL)
                        else:
                            step.add(restored=branch.key)
                        trunk_since = int(branch.mark.data["since"])
                        trunk_reused = tuple(branch.mark.data["reused"])
                        branch_cuts = tuple(c for c in cuts if c not in branch.cuts)
                        # the image's configuration over the trunk's
                        self._configure(rootfs, cuts=branch_cuts, atoms=atoms)
                        step.add(packages=len(trunk_reused))
                with run.step("install") as step:
                    if resumed is not None and installed_state:
                        since = int(resumed.data["since"])
                        reused = tuple(resumed.data["reused"])
                        plan = tuple(resumed.data["plan"])
                        step.add(restored=resumed.step, packages=len(reused))
                    else:
                        # an install-partial resumes Portage's own merge list
                        partial = resumed is not None
                        if resumed is not None:
                            since = int(resumed.data["since"])
                            earlier = tuple(resumed.data["reused"])
                            # it keeps the plan of the install it continues
                            earlier_plan = tuple(resumed.data.get("plan") or ())
                            argv = ISO_RESUME_ARGV
                        elif branch is not None:
                            since, earlier, earlier_plan = trunk_since, trunk_reused, ()
                            argv = iso_branch_argv(recipe, jobs=self.jobs)
                        else:
                            since, earlier, earlier_plan = int(time.time()), (), ()
                            argv = iso_emerge_argv(recipe, jobs=self.jobs)
                        # off a trunk the install's own list is only the branch: the
                        # image's plan is asked of the resolver, as a fresh one would see it
                        branched = branch is not None or bool(
                            resumed is not None and resumed.data.get("branch")
                        )
                        # a resume continues an install whose plan was judged when it began
                        judged: tuple[str, ...] | None = None
                        if resumed is None:
                            judged = self._refuse_stale_plan(container, recipe)
                            step.add(stale_check="judged" if judged is not None else "no index")
                        try:
                            installed = container.run(argv)
                        except subprocess.CalledProcessError as err:
                            output = (err.output or "") + (err.stderr or "")
                            saved = False
                            if (
                                store is not None
                                and fps is not None
                                and checkpoint.has_resume_list(rootfs)
                            ):
                                merged = earlier + merged_reused_atoms(output)
                                store.save(
                                    rootfs,
                                    checkpoint.PARTIAL,
                                    fps.partial,
                                    image=key,
                                    run_id=run.run_id,
                                    data={
                                        "since": since,
                                        "branch": branched,
                                        "reused": list(merged),
                                        # the whole list: emerge prints it before merging
                                        "plan": list(
                                            earlier_plan or checkpoint.plan_tokens(output)
                                        ),
                                    },
                                )
                                step.add(checkpoint=checkpoint.PARTIAL)
                                saved = True
                            corrupt = corrupt_binpkgs(output)
                            if corrupt:
                                step.add(corrupt_binpkgs=list(corrupt))
                                then = (
                                    "the install resumes where it stopped"
                                    if saved
                                    else "the install runs again"
                                )
                                raise AssemblerError(
                                    f"corrupt binpkg in {self.binhost_dir}: "
                                    f"{', '.join(corrupt)} (its size or digest does not match "
                                    f"the index). Rebuild or restore it and assemble again: {then}."
                                ) from err
                            raise
                        reused = earlier + parse_reused_atoms(installed.stdout + installed.stderr)
                        if branched and judged is not None:
                            plan = judged  # resolved once, before the install
                        elif branched:
                            pretended = container.run([*iso_emerge_argv(recipe), "--pretend"])
                            plan = checkpoint.plan_tokens(pretended.stdout + pretended.stderr)
                        else:
                            plan = earlier_plan or checkpoint.plan_tokens(
                                installed.stdout + installed.stderr
                            )
                        step.add(packages=len(reused), resumed=partial, plan=len(plan))
                        outside = binpkgs_outside_tree(plan, repos)
                        if outside:
                            step.add(outside_tree=list(outside))
                            raise AssemblerError(
                                f"{len(outside)} binpkg(s) installed from outside the pinned "
                                f"tree: {', '.join(outside[:10])}"
                                + (" ..." if len(outside) > 10 else "")
                                + ". The image would not match its pins; the trail lists them all."
                            )
                with run.step("settle") as step:
                    if resumed is not None and installed_state:
                        step.add(restored=resumed.step)
                    else:
                        # the cut packages again, from their final binpkgs
                        clear_use_break(rootfs)
                        settle = tuple(
                            sorted({c.atom for c in branch_cuts if is_installed(rootfs, c.atom)})
                        )
                        if settle:
                            container.run(iso_settle_argv(settle, jobs=self.jobs))
                        step.add(atoms=list(settle))
                        if store is not None and fps is not None:
                            # a newer install makes this image's later checkpoints stale
                            store.release_step(checkpoint.PACKAGES, key)
                            store.release_step(checkpoint.PARTIAL, key)
                            store.save(
                                rootfs,
                                checkpoint.INSTALL,
                                fps.install,
                                image=key,
                                run_id=run.run_id,
                                data={
                                    "since": since,
                                    "reused": list(reused),
                                    **self._plan_data(plan),
                                },
                            )
                            step.add(checkpoint=checkpoint.INSTALL)
                with run.step("depclean") as step:
                    if resumed is not None and final_packages:
                        step.add(restored=resumed.step)
                    else:
                        # The stage3 under the ISO keeps what the closure does not reach:
                        # its own gcc and binutils slots, bootstrap leftovers (F77).
                        cleaned = container.run(ISO_DEPCLEAN_ARGV)
                        step.add(removed=_depclean_count(cleaned.stdout + cleaned.stderr))
                with run.step("preserved-rebuild") as step:
                    if resumed is not None and final_packages:
                        step.add(restored=resumed.step)
                    else:
                        container.run(
                            [
                                "emerge",
                                *ISO_SETTLE_OPTIONS,
                                *_jobs_args(self.jobs),
                                "@preserved-rebuild",
                            ]
                        )
                        if store is not None and fps is not None:
                            store.save(
                                rootfs,
                                checkpoint.PACKAGES,
                                fps.packages,
                                image=key,
                                run_id=run.run_id,
                                data={
                                    "since": since,
                                    "reused": list(reused),
                                    **self._plan_data(plan),
                                },
                            )
                            step.add(checkpoint=checkpoint.PACKAGES)
                with run.step("rootfs") as step:
                    # the other layers' files, after the checkpoints (INSTALL_ROOTFS_LAYERS)
                    late = tuple(x for x in recipe.portage_layers if x not in INSTALL_ROOTFS_LAYERS)
                    written = apply_rootfs(
                        rootfs, recipe, variants_dir=config.variants_dir(), layers=late
                    )
                    step.add(layers=list(late), files=list(written))
                with run.step("system") as step:
                    version = f"{when:%Y.%m.%d}"
                    build = {
                        "VERSION": f"{version} ({recipe.flavor}, {recipe.init})",
                        "VERSION_ID": version,
                        "BUILD_ID": run.run_id or name,
                        "IMAGE_ID": f"bentoo-{recipe.flavor}-{recipe.init}-{recipe.arch}",
                        "IMAGE_VERSION": version,
                        "VARIANT": recipe.flavor.title(),
                        "VARIANT_ID": recipe.flavor,
                    }
                    step.add(**apply_system(container, system_cfg, init=recipe.init, build=build))
                if stage4:
                    # the configured system, BEFORE the live user and autologin
                    with run.step("stage4") as step:
                        # packed before `live` and `finalize`: the install's
                        # secrets are removed and refused here too (story 007)
                        removed = remove_generated_secrets(rootfs)
                        if removed:
                            step.add(removed=removed)
                        secrets = build_time_secrets(rootfs, system_cfg.live)
                        if secrets:
                            raise AssemblerError(
                                "the stage4 would carry build-time secrets: "
                                + "; ".join(f"/{path}" for path in secrets)
                            )
                        tarball4 = output_dir / f"{name}.stage4.tar.xz"
                        publish.make_stage4(rootfs, tarball4, exclude_file, threads=self.jobs)
                        artifacts.append(tarball4)
                        run.artifact(tarball4, role="stage4")
                with run.step("live") as step:
                    step.add(**apply_live(container, system_cfg, init=recipe.init))
                with run.step("initramfs") as step:
                    kver = _kernel_version(rootfs)
                    initramfs = rootfs / "boot" / f"initramfs-{kver}.img"
                    container.run(_dracut_argv(kver, Path("/boot") / initramfs.name))
                    step.add(
                        kernel=kver,
                        initramfs_bytes=initramfs.stat().st_size if initramfs.is_file() else None,
                    )
            # after the LAST container command: nspawn rewrites resolv.conf (F81)
            with run.step("finalize") as step:
                step.add(**finalize(rootfs, system_cfg, init=recipe.init))
            with run.step("verify-config") as step:
                problems = verify(rootfs, system_cfg, init=recipe.init, live=True)
                step.add(problems=problems)
                if problems:
                    raise AssemblerError(
                        "the image's configuration did not apply: " + "; ".join(problems)
                    )
            packages = audit.harvest_packages(
                rootfs, merges=_emerge_log_merges(rootfs, since=since), reused=reused
            )
            run.attach("packages", packages)
            kernel = _locate_kernel(rootfs, kver)

            with run.step("toolbox") as step:
                toolbox_root = config.scratch_dir() / "toolbox" / f"{recipe.arch}-{recipe.init}"
                extracted = toolbox.ensure_rootfs(toolbox_tar, toolbox_root)
                # the image read-only; the scratch (squashfs, exclude list, SBOM)
                # and the output directory (the ISO and its staging) writable
                tools = toolbox.Toolbox(
                    toolbox_root,
                    ro={"rootfs": rootfs},
                    rw={"scratch": rootfs.parent, "out": output_dir},
                )
                tool_versions = tools.versions()
                step.add(tarball=toolbox_tar.name, extracted=extracted, **tool_versions)

            extra: dict[str, str | Path] = {
                "bentoo/world": "".join(f"{a}\n" for a in atoms),
                "bentoo/packages.txt": "".join(
                    f"{a}\n" for a in sorted(str(p["atom"]) for p in packages)
                ),
            }
            if sbom:
                with run.step("sbom") as step:
                    sbom_file = publish.write_sbom(rootfs, output_dir / f"{name}.iso.spdx.json")
                    step.add(written=sbom_file is not None)
                    if sbom_file is not None:
                        # compressed on the medium (236 -> 32 MiB), plain beside it
                        packed = publish.compress_sbom(
                            sbom_file, rootfs.parent / f"{key}.spdx.json.zst"
                        )
                        extra["bentoo/sbom.spdx.json.zst"] = packed
                        squashfs_files.append(packed)  # scratch, gone at cleanup
                        artifacts.append(sbom_file)
                        run.artifact(sbom_file, role="sbom", digest=False)

            volume = image.volume_id(recipe.flavor)
            packages_file = publish.write_packages(output_dir / f"{name}.iso.packages", packages)
            artifacts.append(packages_file)
            rootfs_bytes = _tree_bytes(rootfs)
            sums: dict[str, str] = {}
            for profile in compressions:
                suffix = "" if profile == "zstd" else f"-{profile}"
                squashfs = rootfs.parent / f"{key}.{profile}.squashfs"
                squashfs_files.append(squashfs)
                with run.step(f"squashfs:{profile}") as step:
                    image.make_squashfs(
                        rootfs,
                        squashfs,
                        tools=tools,
                        compression=profile,
                        exclude_file=exclude_file,
                        processors=self.jobs,
                    )
                    squashed = squashfs.stat().st_size
                    step.add(rootfs_bytes=rootfs_bytes, squashfs_bytes=squashed)
                    if squashed:
                        run.metric(f"squashfs.{profile}.ratio", round(rootfs_bytes / squashed, 3))
                iso = output_dir / f"{name}{suffix}.iso"
                info = build_info(
                    recipe,
                    name=f"{name}{suffix}",
                    build_id=run.run_id or name,
                    kernel=kver,
                    compression=profile,
                    volume=volume,
                    packages=len(packages),
                    world_atoms=len(atoms),
                    stage3=pointer.model_dump(mode="json"),
                    tools=tool_versions,
                )
                with run.step(f"iso:{profile}") as step:
                    image.build_iso(
                        squashfs,
                        iso,
                        tools=tools,
                        kernel=kernel,
                        initramfs=initramfs,
                        volume=volume,
                        title=publish.title(recipe, when),
                        build_id=run.run_id or name,
                        text_target="multi-user.target" if recipe.init == "systemd" else None,
                        open_nvidia=ships_nvidia_driver(rootfs),
                        extra={
                            **extra,
                            "bentoo/version": f"{name}{suffix}\n",
                            "bentoo/build.json": json.dumps(info, indent=1, default=str) + "\n",
                        },
                    )
                    step.add(iso_bytes=iso.stat().st_size if iso.is_file() else None)
                with run.step(f"publish:{profile}"):
                    digests, sha256 = publish.write_digests(iso)
                    contents = publish.write_contents(
                        squashfs,
                        output_dir / f"{name}{suffix}.iso.contents.gz",
                        tools=tools,
                        processors=self.jobs,
                    )
                    sums[iso.name] = sha256
                    artifacts += [digests, contents]
                    isos.append(iso)
                run.artifact(iso, role=f"iso:{profile}")
            for extra_file in (packages_file, *artifacts):
                if extra_file.is_file() and extra_file.name not in sums:
                    sums[extra_file.name] = publish.sha256_of(extra_file)
            artifacts.append(publish.update_sha256sums(output_dir, sums))
            artifacts.append(publish.write_latest(output_dir, key, isos[0]))
        except BaseException:
            keep_rootfs = True  # keep the rootfs for debugging on failure
            raise

        if not keep_rootfs:
            with run.step("cleanup"):
                # the checkpoints stay: the next run of this image (a configuration
                # change, a new day's version) resumes from them
                try:
                    checkpoint.remove_tree(rootfs, backend)
                except OSError, checkpoint.CheckpointError:
                    shutil.rmtree(rootfs, ignore_errors=True)
                for squashfs in squashfs_files:
                    squashfs.unlink(missing_ok=True)  # already copied into the ISO
        return AssembleResult(name=name, isos=tuple(isos), artifacts=tuple(artifacts))
