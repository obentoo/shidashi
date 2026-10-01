"""Tests of shidashi.assembler — ISO Assembler (OVERVIEW §7/§18.6, Phase 1).

In the idiom of tests/test_factory.py (UNIT off-host): the argv builders and the
kernel/initramfs locators are **pure** (tested without root), the root guard
is checked with a monkeypatched ``os.geteuid``, and the orchestration of
:meth:`Assembler.assemble` runs in full with seed/portage/Container/image
monkeypatched — no real nspawn/emerge/dracut/mksquashfs (host-gated).
"""

import os
from pathlib import Path

import pytest

import shidashi.assembler as asm
from shidashi import image
from shidashi.assembler import (
    Assembler,
    AssemblerError,
    _build_binds,
    _dracut_argv,
    _install_sets,
    _kernel_version,
    _locate_kernel,
    iso_emerge_argv,
)
from shidashi.image import ImageError
from shidashi.recipe import Phase, ResolvedRecipe, UseBreak
from shidashi.resolve import ResolveError


@pytest.fixture(autouse=True)
def _system_config_stubbed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The system/live configuration has its own tests (test_system.py); here it
    only has to happen, in its place in the order."""
    calls: list[str] = []

    def apply_system(
        _c: object, _cfg: object, *, init: str, build: object = None
    ) -> dict[str, object]:
        calls.append("system")
        return {}

    def apply_live(_c: object, _cfg: object, *, init: str) -> dict[str, object]:
        calls.append("live")
        return {}

    def verify(_r: object, _cfg: object, *, init: str, live: bool) -> list[str]:
        calls.append("verify")
        return []

    # the world file's own tests are in test_world.py; these recipes are synthetic
    from shidashi import world

    monkeypatch.setattr(world, "current_atoms", lambda recipe, variants_dir: ("app-misc/a",))
    monkeypatch.setattr(asm, "apply_system", apply_system)
    monkeypatch.setattr(asm, "apply_live", apply_live)
    monkeypatch.setattr(asm, "verify", verify)
    return calls


@pytest.fixture(autouse=True)
def _no_tree_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No unit test may fetch the real ::gentoo snapshot (49 MB, D26)."""
    tree = tmp_path / "pinned-gentoo"
    tree.mkdir()
    monkeypatch.setattr(asm, "pinned_repos", lambda **_k: {"gentoo": tree})


def _recipe(
    *,
    flavor: str = "kde",
    # EMPTY default: install_sets now FAILS LOUDLY on a declared set without a file,
    # so a test that does not care about sets must not declare any.
    sets: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="znver5",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v4",
        rustflags="",
        cpu_flags_x86=("avx2",),
        tier=1,
        runnable_on_build_host=True,
        sets=sets,
        exclude=exclude,
        phases=(),
        portage_layers=("base", "arch/znver5", "flavor/kde", "init/systemd"),
        seed_source="download",
    )


def _pointer() -> object:
    from shidashi.seed import Stage3Pointer

    return Stage3Pointer(
        init="systemd",
        base_url="https://distfiles.gentoo.org/x",
        snapshot="20260524T170105Z",
        filename="stage3-amd64-nomultilib-systemd-20260524T170105Z.tar.xz",
        sha512="0" * 128,
    )


# --- iso_emerge_argv / _dracut_argv (PURE) -----------------------------------


def test_iso_emerge_argv_targets_system_plus_flavor_sets() -> None:
    # §7/§9.3 — --emptytree pulls EVERYTHING arch-native (incl. @system) + the flavor's sets.
    assert iso_emerge_argv(_recipe(sets=("graphics", "kde"))) == [
        "emerge",
        "--usepkgonly",
        "--binpkg-respect-use=y",
        "--emptytree",
        "--verbose",
        "@system",
        "@graphics",
        "@kde",
    ]


def test_iso_emerge_argv_jobs_merges_binpkgs_in_parallel() -> None:
    argv = iso_emerge_argv(_recipe(sets=("kde",)), jobs=8)
    assert argv[:7] == [
        "emerge",
        "--usepkgonly",
        "--binpkg-respect-use=y",
        "--emptytree",
        "--verbose",
        "--jobs",
        "8",
    ]
    assert argv[-2:] == ["@system", "@kde"]


def test_iso_emerge_argv_empty_sets_falls_back_to_world() -> None:
    # minimal declares no sets → @world (= @system + what the base seeded), --emptytree.
    assert iso_emerge_argv(_recipe(sets=())) == [
        "emerge",
        "--usepkgonly",
        "--binpkg-respect-use=y",
        "--emptytree",
        "--verbose",
        "@world",
    ]


def test_dracut_argv_adds_dmsquash_live() -> None:
    argv = _dracut_argv("6.12.0", Path("/boot/initramfs-6.12.0.img"))
    assert argv == [
        "dracut",
        "--add",
        "dmsquash-live",
        # modules-load.d in the initrd named modules it does not carry (vboxdrv)
        "--omit",
        "systemd-modules-load",
        "--no-hostonly",
        "--force",
        "/boot/initramfs-6.12.0.img",
        "6.12.0",
    ]


# --- _kernel_version / _locate_kernel (PUROS) --------------------------------


def test_kernel_version_single(tmp_path: Path) -> None:
    (tmp_path / "lib" / "modules" / "6.12.0-bentoo").mkdir(parents=True)
    assert _kernel_version(tmp_path) == "6.12.0-bentoo"


def test_kernel_version_none_raises(tmp_path: Path) -> None:
    (tmp_path / "lib" / "modules").mkdir(parents=True)
    with pytest.raises(AssemblerError, match="exactly one kernel"):
        _kernel_version(tmp_path)


def test_kernel_version_ambiguous_raises(tmp_path: Path) -> None:
    for v in ("6.12.0", "6.13.0"):
        (tmp_path / "lib" / "modules" / v).mkdir(parents=True)
    with pytest.raises(AssemblerError, match="exactly one kernel"):
        _kernel_version(tmp_path)


def test_locate_kernel_prefers_exact(tmp_path: Path) -> None:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-6.12.0").write_bytes(b"k")
    (boot / "vmlinuz-old").write_bytes(b"k")
    assert _locate_kernel(tmp_path, "6.12.0") == boot / "vmlinuz-6.12.0"


def test_locate_kernel_globs_when_no_exact(tmp_path: Path) -> None:
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-something").write_bytes(b"k")
    assert _locate_kernel(tmp_path, "6.12.0") == boot / "vmlinuz-something"


def test_locate_kernel_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "boot").mkdir()
    with pytest.raises(AssemblerError, match="vmlinuz"):
        _locate_kernel(tmp_path, "6.12.0")


# --- _build_binds / _install_sets --------------------------------------------


def test_build_binds_binhost_ro_and_no_rw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asm, "bind_repos", lambda d, **_k: [(Path("/h/repo"), Path("/var/db/repos/gentoo"))]
    )
    binds_ro, binds_rw = _build_binds(Path("/bh/znver5"), Path("/rootfs/etc/portage/repos.conf"))
    assert binds_ro[0] == (Path("/h/repo"), Path("/var/db/repos/gentoo"))
    assert (Path("/bh/znver5"), asm._BINHOST_DST) in binds_ro  # binhost mounted RO
    assert binds_rw == []  # Assembler only reads (--usepkgonly): nothing RW


def test_install_sets_copies_curated_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # D25: every set lives in the kits library; the category folder is invisible
    # to the result -- both land flat in /etc/portage/sets.
    variants = tmp_path / "variants"
    (variants / "kits" / "graphics").mkdir(parents=True)
    (variants / "kits" / "graphics" / "graphics").write_text("media-libs/mesa\n")
    (variants / "kits" / "desktops").mkdir(parents=True)
    (variants / "kits" / "desktops" / "kde").write_text("kde-plasma/plasma-meta\n")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    rootfs = tmp_path / "rootfs"

    _install_sets(rootfs, _recipe(sets=("graphics", "kde")))

    sets_dir = rootfs / "etc" / "portage" / "sets"
    assert (sets_dir / "graphics").read_text() == "media-libs/mesa\n"
    assert (sets_dir / "kde").read_text() == "kde-plasma/plasma-meta\n"


# --- root guard ---------------------------------------------------------------


def test_assemble_requires_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(AssemblerError, match="root"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")


# --- assemble() orchestration (everything monkeypatched) ---------------------


class _FakeContainer:
    """Fake Container: records the ``run`` argv and is a no-op context manager."""

    instances: list[_FakeContainer] = []

    def __init__(
        self,
        rootfs: Path,
        *,
        ephemeral: bool,
        binds: list[tuple[Path, Path]],
        binds_rw: list[tuple[Path, Path]],
        log: Path | None = None,
    ) -> None:
        self.log = log
        self.rootfs = rootfs
        self.binds = binds
        self.binds_rw = binds_rw
        self.runs: list[list[str]] = []
        _FakeContainer.instances.append(self)

    def __enter__(self) -> _FakeContainer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, argv: list[str], **kw: object) -> object:
        from shidashi.container import CommandResult

        self.runs.append(argv)
        return CommandResult(0, "", "")


@pytest.mark.parametrize("jobs", [None, 6])
def test_assemble_orchestrates_seed_emerge_dracut_squashfs_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: int | None
) -> None:
    _FakeContainer.instances = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))

    seed_tar = tmp_path / "stage3.tar"
    seed_tar.write_bytes(b"S")
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(asm, "fetch_stage3", lambda pointer, *, cache_dir, download: seed_tar)

    def fake_extract(tarball: Path, rootfs: Path) -> None:
        (rootfs / "lib" / "modules" / "6.12.0-bentoo").mkdir(parents=True)
        boot = rootfs / "boot"
        boot.mkdir(parents=True)
        (boot / "vmlinuz-6.12.0-bentoo").write_bytes(b"K")
        (rootfs / "etc" / "portage").mkdir(parents=True)
        # what the first emerge installs; the settle only redoes installed cuts
        (rootfs / "var/db/pkg/media-video/pipewire-1.6.9").mkdir(parents=True)

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)

    # A single event list to pin the ORDER (not only the occurrence): apply_portage
    # MUST precede the emerge, otherwise the resolved USE ≠ the binpkgs' (§18.6).
    events: list[str] = []
    monkeypatch.setattr(
        asm, "apply_portage", lambda rootfs, recipe, *, variants_dir: events.append("apply_portage")
    )
    monkeypatch.setattr(asm, "apply_rootfs", lambda *_a, **_k: ())
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])

    cuts_seen: list[str] = []

    class _OrchContainer(_FakeContainer):
        def run(self, argv: list[str], **kw: object) -> object:
            events.append(argv[0])
            cut = self.rootfs / "etc/portage/package.use/zz-shidashi-use-break"
            cuts_seen.append(cut.read_text() if cut.is_file() else "")
            return super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _OrchContainer)

    sq_calls: list[tuple[Path, Path]] = []
    iso_calls: list[tuple[Path, Path, Path, Path]] = []

    processors_seen: list[int | None] = []

    iso_kwargs: list[dict[str, object]] = []
    squash_kwargs: list[dict[str, object]] = []

    def fake_squashfs(rootfs: Path, output: Path, **kw: object) -> Path:
        sq_calls.append((rootfs, output))
        processors_seen.append(kw.get("processors"))  # type: ignore[arg-type]
        squash_kwargs.append(kw)
        output.write_bytes(b"SQ")
        return output

    def fake_iso(
        squashfs: Path, output: Path, *, kernel: Path, initramfs: Path, **kw: object
    ) -> Path:
        iso_calls.append((squashfs, output, kernel, initramfs))
        iso_kwargs.append(kw)
        output.parent.mkdir(parents=True, exist_ok=True)  # o build_iso real faz isto
        output.write_bytes(b"ISO")
        return output

    monkeypatch.setattr(image, "make_squashfs", fake_squashfs)
    monkeypatch.setattr(image, "build_iso", fake_iso)
    # the host tools of the artifacts (unsquashfs, syft) are publish.py's tests' concern
    from shidashi import publish

    def fake_contents(squashfs: Path, dest: Path) -> Path:
        dest.write_bytes(b"C")
        return dest

    monkeypatch.setattr(publish, "write_contents", fake_contents)
    monkeypatch.setattr(publish, "write_sbom", lambda rootfs, dest: None)

    import datetime

    out_dir = tmp_path / "dist"
    out = out_dir / "bentoo-2026.09.30-kde-systemd-znver5.iso"
    # sets=() on purpose: this test exercises the assemble ORCHESTRATION, and
    # install_sets would fail loudly on a declared set without a curated file in tmp_path.
    recipe = _recipe(sets=()).model_copy(
        update={
            "phases": (
                Phase(
                    name="desktop",
                    stage="desktop",
                    use_break=(UseBreak(atom="media-video/pipewire", flag="ffmpeg"),),
                ),
            )
        }
    )
    from shidashi import audit

    with audit.run(tmp_path / "runs", command="assemble", argv=[]) as trail:
        result = Assembler(recipe, tmp_path / "binhost" / "znver5", jobs=jobs).assemble(
            out_dir, now=datetime.datetime(2026, 9, 30, 1, 0, tzinfo=datetime.UTC)
        )
    manifest = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))
    # every step of the ISO is in the audit trail, in order, and the ISO with its hash
    assert [st["step"] for st in manifest["steps"]] == [
        "seed",
        "configure",
        "install",
        "settle",
        "depclean",
        "preserved-rebuild",
        "system",
        "live",
        "initramfs",
        "finalize",
        "verify-config",
        "sbom",
        "squashfs:zstd",
        "iso:zstd",
        "publish:zstd",
        "cleanup",
    ]
    assert all(st["status"] == "ok" for st in manifest["steps"])
    assert manifest["artifacts"][0]["role"] == "iso:zstd"
    assert manifest["artifacts"][0]["sha256"] == audit.sha256_file(out)
    assert "packages" in [a.removesuffix(".json") for a in manifest["attachments"]]

    assert result.isos == (out,) and result.name == "bentoo-2026.09.30-kde-systemd-znver5"
    # beside the ISO, the published artifacts of the major distributions
    names = {p.name for p in result.artifacts}
    assert {
        out.name + ".DIGESTS",
        out.name + ".packages",
        out.name + ".contents.gz",
        "SHA256SUMS",
        "latest-znver5-kde-systemd.txt",
    } <= names
    sums = (out_dir / "SHA256SUMS").read_text()
    assert f"{audit.sha256_file(out)}  {out.name}" in sums
    # the medium carries its metadata; the squashfs its exclude list, zstd by default
    extra = iso_kwargs[0]["extra"]
    assert set(extra) >= {  # type: ignore[call-overload]
        "bentoo/world",
        "bentoo/packages.txt",
        "bentoo/version",
        "bentoo/build.json",
    }
    assert iso_kwargs[0]["volume"] == "BENTOO_KDE"
    assert iso_kwargs[0]["text_target"] == "multi-user.target"
    assert squash_kwargs[0]["compression"] == "zstd"
    exclude_file = Path(str(squash_kwargs[0]["exclude_file"]))
    assert exclude_file.name == "znver5-kde-systemd.squashfs-exclude"
    assert "dev/*" in exclude_file.read_text().splitlines()  # livecd.yaml's squashfs_exclude
    assert out.read_bytes() == b"ISO"
    # §18.6 — apply_portage strictly BEFORE the emerge (not only "was called").
    assert events.index("apply_portage") < events.index("emerge")
    # order inside the container: emerge --usepkgonly, then the stage3's leftovers
    # go (depclean + preserved-rebuild from binpkgs only), then dracut.
    inst = _FakeContainer.instances[0]
    parallel = ["--jobs", str(jobs)] if jobs else []
    final = ["emerge", "--usepkgonly", "--binpkg-respect-use=y", "--oneshot", *parallel]
    assert inst.runs[0] == iso_emerge_argv(recipe, jobs=jobs)
    # F76: the image installs under the chain's cuts (the cut binpkg), then the
    # cut package is settled from its final binpkg, with the cut file gone
    assert cuts_seen[0] == "media-video/pipewire -ffmpeg\n"
    assert inst.runs[1] == [*final, "media-video/pipewire"]
    assert cuts_seen[1:] == ["", "", "", ""]
    # binaries only: build deps do not keep the stage3's leftovers (F77)
    assert inst.runs[2] == ["emerge", "--depclean", "--with-bdeps=n"]
    assert inst.runs[3] == [*final, "@preserved-rebuild"]
    # --jobs also caps mksquashfs; without it mksquashfs keeps every CPU
    assert processors_seen == [jobs]
    assert inst.runs[4][0] == "dracut" and "dmsquash-live" in inst.runs[4]
    # binhost mounted RO in the container (and nothing RW) — the binhost_dir→Container thread.
    assert (tmp_path / "binhost" / "znver5", asm._BINHOST_DST) in inst.binds
    assert inst.binds_rw == []
    # kernel located and chained squashfs → ISO.
    assert sq_calls and iso_calls
    assert iso_calls[0][0] == sq_calls[0][1]  # build_iso receives the produced squashfs
    assert iso_calls[0][2].name == "vmlinuz-6.12.0-bentoo"
    # success without --keep → rootfs AND intermediate squashfs removed from the scratch.
    assemble_dir = tmp_path / "scratch" / "assemble"
    assert not (assemble_dir / "znver5-kde-systemd").exists()
    assert not (assemble_dir / "znver5-kde-systemd.zstd.squashfs").exists()


def test_assemble_keeps_rootfs_on_emerge_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeContainer.instances = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(
        asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp_path / "s.tar"
    )

    def fake_extract(tarball: Path, rootfs: Path) -> None:
        (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True)
        (rootfs / "boot").mkdir(parents=True)

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, *, variants_dir: None)
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])

    class _BoomContainer(_FakeContainer):
        def run(self, argv: list[str], **kw: object) -> None:
            raise RuntimeError("emerge --usepkgonly blew up")

    monkeypatch.setattr(asm, "Container", _BoomContainer)

    rootfs = tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"
    with pytest.raises(RuntimeError, match="blew up"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    assert rootfs.exists()  # kept for debugging on failure


def test_assemble_keeps_rootfs_on_squashfs_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # separate branch: a POST-container failure (make_squashfs) also keeps the rootfs.
    _FakeContainer.instances = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(
        asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp_path / "s.tar"
    )

    def fake_extract(tarball: Path, rootfs: Path) -> None:
        (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True)
        boot = rootfs / "boot"
        boot.mkdir(parents=True)
        (boot / "vmlinuz-6.12.0").write_bytes(b"K")

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, *, variants_dir: None)
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
    monkeypatch.setattr(asm, "Container", _FakeContainer)  # run() is a no-op (success)

    def boom_squashfs(rootfs: Path, output: Path, **_k: object) -> Path:
        raise ImageError("mksquashfs: disk full")

    monkeypatch.setattr(image, "make_squashfs", boom_squashfs)

    rootfs = tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"
    with pytest.raises(ImageError, match="disk full"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    assert rootfs.exists()  # a post-container ImageError also keeps the rootfs


def test_install_sets_refuses_a_name_defined_twice_in_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D25 replaced "a later layer's set of the same name wins" with one library.
    # Portage's set namespace is flat, so two files with one name in different
    # categories would install whichever the walk met last. That is refused,
    # naming both files -- per-flavor tuning is `exclude:`, which is explicit.
    variants = tmp_path / "variants"
    (variants / "kits" / "media").mkdir(parents=True)
    (variants / "kits" / "media" / "graphics").write_text("A\n")
    (variants / "kits" / "desktops").mkdir(parents=True)
    (variants / "kits" / "desktops" / "graphics").write_text("B\n")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))

    with pytest.raises(ResolveError, match=r"defined twice.*desktops/graphics.*media/graphics"):
        _install_sets(tmp_path / "rootfs", _recipe(sets=("graphics",)))


# NB: the real privileged path (nspawn + emerge --usepkgonly + dracut +
# mksquashfs + grub-mkrescue) is host-gated (root + Gentoo + tools); it is left
# to the Phase 1 boot smoke test (QEMU), not to the off-host unit tests.


# --- _install_sets: transitive @refs, exclude and loud failure ----------------


def test_install_sets_follows_nested_set_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Portage expands `@other-set` inside a set file, so installing the
    # aggregator requires installing the leaves -- otherwise @base resolves to a
    # nonexistent target INSIDE the container, far from the cause.
    variants = tmp_path / "variants"
    (variants / "kits" / "groups").mkdir(parents=True)
    (variants / "kits" / "groups" / "agg").write_text("@leaf\n")
    (variants / "kits" / "core").mkdir(parents=True)
    (variants / "kits" / "core" / "leaf").write_text("app-editors/nano\n")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    rootfs = tmp_path / "rootfs"

    _install_sets(rootfs, _recipe(sets=("agg",)))

    sets_dir = rootfs / "etc" / "portage" / "sets"
    assert (sets_dir / "agg").exists()
    assert "app-editors/nano" in (sets_dir / "leaf").read_text()


def test_install_sets_applies_flavor_exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants = tmp_path / "variants"
    (variants / "kits" / "core").mkdir(parents=True)
    (variants / "kits" / "core" / "leaf").write_text(
        "media-video/vlc\napp-editors/nano  # with a comment\n"
    )
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    rootfs = tmp_path / "rootfs"

    _install_sets(rootfs, _recipe(sets=("leaf",), exclude=("media-video/vlc",)))

    written = (rootfs / "etc" / "portage" / "sets" / "leaf").read_text()
    assert "media-video/vlc" not in written.replace(
        "# shidashi: excluded by flavor/kde: media-video/vlc", ""
    )
    assert "app-editors/nano" in written  # the inline comment does not get in the way
    assert "excluded by flavor/kde" in written  # the subtraction is recorded


def test_install_sets_raises_when_declared_set_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to be silently ignored and only failed at the emerge, inside the container.
    variants = tmp_path / "variants"
    (variants / "kits" / "core").mkdir(parents=True)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))

    with pytest.raises(ResolveError, match="missing"):
        _install_sets(tmp_path / "rootfs", _recipe(sets=("does-not-exist",)))


def test_locate_kernel_finds_the_image_of_a_uki_install(tmp_path: Path) -> None:
    """installkernel[uki] (the base's SYSTEMD="boot uki ukify") puts a UKI in
    /boot/EFI/Linux and NO /boot/vmlinuz. The kernel image is still where
    kernel-install keeps it: usr/lib/modules/<kver>/vmlinuz, a relative symlink
    to the dist-kernel's bzImage -- the pipeline's kde image, 2026-09-29."""
    kver = "7.2.6-gentoo-dist"
    (tmp_path / "boot" / "EFI" / "Linux").mkdir(parents=True)
    (tmp_path / "boot" / "EFI" / "Linux" / f"x-{kver}.efi").write_bytes(b"UKI")
    bz = tmp_path / "usr" / "src" / f"linux-{kver}" / "arch" / "x86" / "boot" / "bzImage"
    bz.parent.mkdir(parents=True)
    bz.write_bytes(b"KERNEL")
    mods = tmp_path / "usr" / "lib" / "modules" / kver
    mods.mkdir(parents=True)
    (mods / "vmlinuz").symlink_to(f"../../../src/linux-{kver}/arch/x86/boot/bzImage")

    found = _locate_kernel(tmp_path, kver)
    assert found == mods / "vmlinuz"
    assert found.read_bytes() == b"KERNEL"


def test_a_configuration_that_did_not_apply_fails_the_assemble_before_the_squashfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F79: the first ISO shipped without a hostname, a service or an account and
    nothing noticed. verify() naming anything fails the build, the rootfs kept."""
    _FakeContainer.instances = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
    monkeypatch.setattr(
        asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp_path / "s.tar"
    )

    def fake_extract(tarball: Path, rootfs: Path) -> None:
        (rootfs / "lib" / "modules" / "6.12.0").mkdir(parents=True)

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)
    monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, *, variants_dir: None)
    monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
    monkeypatch.setattr(asm, "Container", _FakeContainer)
    monkeypatch.setattr(
        asm, "verify", lambda r, cfg, *, init, live: ["NetworkManager.service is not enabled"]
    )
    squashed: list[Path] = []

    def make_squashfs(_rootfs: Path, output: Path, **_k: object) -> Path:
        squashed.append(output)
        return output

    monkeypatch.setattr(image, "make_squashfs", make_squashfs)

    with pytest.raises(AssemblerError, match="NetworkManager.service is not enabled"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    assert squashed == []  # never packed
    assert (tmp_path / "scratch" / "assemble" / "znver5-kde-systemd").exists()


def test_ships_nvidia_driver_reads_the_vdb(tmp_path: Path) -> None:
    from shidashi.assembler import ships_nvidia_driver

    assert not ships_nvidia_driver(tmp_path)
    vdb = tmp_path / "var/db/pkg/x11-drivers"
    (vdb / "nvidia-settings-595.10").mkdir(parents=True)
    assert not ships_nvidia_driver(tmp_path)
    (vdb / "nvidia-drivers-615.71.09").mkdir()
    assert ships_nvidia_driver(tmp_path)
