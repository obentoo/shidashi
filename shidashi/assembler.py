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
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from shidashi import audit, config, image, publish, toolbox, world
from shidashi.container import Container
from shidashi.phases import (
    ISO_EMERGE_OPTIONS,
    ISO_SETTLE_OPTIONS,
    clear_use_break,
    image_cuts,
    image_targets,
    is_installed,
    parse_reused_atoms,
    write_cuts,
)
from shidashi.recipe import TOOLBOX_STAGE, ResolvedRecipe
from shidashi.resolve import apply_portage, apply_rootfs, bind_repos, install_sets
from shidashi.seed import extract_stage3, fetch_stage3, load_pointer
from shidashi.system import (
    apply_live,
    apply_system,
    finalize,
    load_livecd,
    load_system_config,
    verify,
)
from shidashi.tree import pinned_repos

__all__ = ["Assembler", "AssemblerError"]

# Fixed binhost target inside the container: the base ``make.conf`` points PKGDIR
# here, so the host-side binhost is bind-mounted over this path (same as the
# Factory, which mounts the output PKGDIR at the same destination — OVERVIEW §6.3).
_BINHOST_DST = Path("/var/cache/binpkgs")


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


def iso_settle_argv(atoms: tuple[str, ...], *, jobs: int | None = None) -> list[str]:
    """The settle of the cut packages: their final binpkgs. Pure."""
    return ["emerge", *ISO_SETTLE_OPTIONS, *_jobs_args(jobs), *atoms]


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
        #: emerge --jobs and mksquashfs -processors; None = emerge serial,
        #: mksquashfs on every CPU.
        self.jobs = jobs

    def assemble(
        self,
        output_dir: Path,
        *,
        download: bool = True,
        keep: bool = False,
        compressions: Sequence[str] = ("zstd",),
        stage4: bool = False,
        sbom: bool = True,
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
            # checked now, not after the half-hour install that needs it
            toolbox_tar = toolbox.tarball_path(
                recipe, snapshot=pointer.snapshot, fork_points_dir=config.fork_points_dir()
            )
            if not toolbox_tar.is_file():
                raise AssemblerError(
                    f"no toolbox for {recipe.arch}/{recipe.init} at {toolbox_tar}; build it "
                    f"first: shidashi factory {recipe.arch} {TOOLBOX_STAGE} {recipe.init}"
                )
            tarball = fetch_stage3(pointer, cache_dir=config.cache_dir(), download=download)
            # a fresh stage3 into a fresh directory: a failed --keep run leaves its
            # rootfs, and extracting over it would inherit what that run left
            shutil.rmtree(rootfs, ignore_errors=True)
            extract_stage3(tarball, rootfs)
            step.add(stage3=tarball.name, stage3_sha512=pointer.sha512, rootfs=str(rootfs))

        with run.step("configure") as step:
            apply_rootfs(rootfs, recipe, variants_dir=config.variants_dir())
            apply_portage(rootfs, recipe, variants_dir=config.variants_dir())
            _install_sets(rootfs, recipe)
            # the chain's cycle cuts, as the factory built under them: a fresh stage3
            # meets every cycle again, and the cut binpkgs are in the PKGDIR
            cuts = image_cuts(recipe)
            write_cuts(rootfs, cuts)
            # loaded (and validated) now: a broken system.yaml fails before the
            # half-hour install, not after it
            system_cfg = load_system_config(recipe, variants_dir=config.variants_dir())
            # the flat package list, as committed in variants/<stage>/world.<init>
            atoms = world.current_atoms(recipe, config.variants_dir())
            world.write_to_image(rootfs, atoms)
            run.attach("world", list(atoms))
            step.add(
                world=len(atoms),
                system=system_cfg.model_dump(exclude={"live": {"password"}}),
                layers=list(recipe.portage_layers),
                sets=list(recipe.sets),
                cuts=[f"{c.atom} {'' if c.enable else '-'}{c.flag}" for c in cuts],
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
                with run.step("install") as step:
                    since = int(time.time())
                    installed = container.run(iso_emerge_argv(recipe, jobs=self.jobs))
                    reused = parse_reused_atoms(installed.stdout + installed.stderr)
                    step.add(packages=len(reused))
                with run.step("settle") as step:
                    # the cut packages again, from their final binpkgs
                    clear_use_break(rootfs)
                    settle = tuple(sorted({c.atom for c in cuts if is_installed(rootfs, c.atom)}))
                    if settle:
                        container.run(iso_settle_argv(settle, jobs=self.jobs))
                    step.add(atoms=list(settle))
                with run.step("depclean") as step:
                    # The stage3 under the ISO keeps what the closure does not reach:
                    # its own gcc and binutils slots, bootstrap leftovers (F77).
                    cleaned = container.run(ISO_DEPCLEAN_ARGV)
                    step.add(removed=_depclean_count(cleaned.stdout + cleaned.stderr))
                with run.step("preserved-rebuild"):
                    container.run(
                        [
                            "emerge",
                            *ISO_SETTLE_OPTIONS,
                            *_jobs_args(self.jobs),
                            "@preserved-rebuild",
                        ]
                    )
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
                        tarball4 = output_dir / f"{name}.stage4.tar.xz"
                        publish.make_stage4(rootfs, tarball4, exclude_file)
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
                        squashfs, output_dir / f"{name}{suffix}.iso.contents.gz", tools=tools
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
                shutil.rmtree(rootfs, ignore_errors=True)
                for squashfs in squashfs_files:
                    squashfs.unlink(missing_ok=True)  # already copied into the ISO
        return AssembleResult(name=name, isos=tuple(isos), artifacts=tuple(artifacts))
