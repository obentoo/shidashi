"""Testes de shidashi.assembler — ISO Assembler (OVERVIEW §7/§18.6, Fase 1).

No idioma de tests/test_factory.py (UNIT off-host): os construtores de argv e os
localizadores de kernel/initramfs são **puros** (testados sem root), a guarda de
root é checada com ``os.geteuid`` monkeypatchado, e a orquestração de
:meth:`Assembler.assemble` roda inteira com seed/portage/Container/image
monkeypatchados — sem nspawn/emerge/dracut/mksquashfs reais (host-gated).
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
from shidashi.recipe import ResolvedRecipe, ResolvedUse
from shidashi.resolve import ResolveError


def _recipe(
    *,
    flavor: str = "kde",
    # default VAZIO: install_sets agora FALHA ALTO num set declarado sem arquivo,
    # então um teste que não se importa com sets não deve declarar nenhum.
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
        use=ResolvedUse(enabled=(), disabled=()),
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


# --- iso_emerge_argv / _dracut_argv (PUROS) ----------------------------------


def test_iso_emerge_argv_targets_system_plus_flavor_sets() -> None:
    # §7/§9.3 — --emptytree puxa TUDO arch-native (incl. @system) + os sets do flavor.
    assert iso_emerge_argv(_recipe(sets=("graphics", "kde"))) == [
        "emerge",
        "--usepkgonly",
        "--emptytree",
        "--verbose",
        "@system",
        "@graphics",
        "@kde",
    ]


def test_iso_emerge_argv_empty_sets_falls_back_to_world() -> None:
    # minimal não declara sets → @world (= @system + o que a base seedou), --emptytree.
    assert iso_emerge_argv(_recipe(sets=())) == [
        "emerge",
        "--usepkgonly",
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
    with pytest.raises(AssemblerError, match="exatamente um kernel"):
        _kernel_version(tmp_path)


def test_kernel_version_ambiguous_raises(tmp_path: Path) -> None:
    for v in ("6.12.0", "6.13.0"):
        (tmp_path / "lib" / "modules" / v).mkdir(parents=True)
    with pytest.raises(AssemblerError, match="exatamente um kernel"):
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
        asm, "bind_repos", lambda d: [(Path("/h/repo"), Path("/var/db/repos/gentoo"))]
    )
    binds_ro, binds_rw = _build_binds(Path("/bh/znver5"), Path("/rootfs/etc/portage/repos.conf"))
    assert binds_ro[0] == (Path("/h/repo"), Path("/var/db/repos/gentoo"))
    assert (Path("/bh/znver5"), asm._BINHOST_DST) in binds_ro  # binhost montado RO
    assert binds_rw == []  # Assembler só lê (--usepkgonly): nada RW


def test_install_sets_copies_curated_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variants = tmp_path / "variants"
    (variants / "base" / "sets").mkdir(parents=True)
    (variants / "base" / "sets" / "graphics").write_text("media-libs/mesa\n")
    (variants / "flavor" / "kde" / "sets").mkdir(parents=True)
    (variants / "flavor" / "kde" / "sets" / "kde").write_text("kde-plasma/plasma-meta\n")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    rootfs = tmp_path / "rootfs"

    _install_sets(rootfs, _recipe(sets=("graphics", "kde")))

    sets_dir = rootfs / "etc" / "portage" / "sets"
    assert (sets_dir / "graphics").read_text() == "media-libs/mesa\n"
    assert (sets_dir / "kde").read_text() == "kde-plasma/plasma-meta\n"


# --- guarda de root ----------------------------------------------------------


def test_assemble_requires_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(AssemblerError, match="root"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")


# --- orquestração de assemble() (tudo monkeypatchado) ------------------------


class _FakeContainer:
    """Container falso: registra os argv de ``run`` e é um context manager no-op."""

    instances: list[_FakeContainer] = []

    def __init__(
        self,
        rootfs: Path,
        *,
        ephemeral: bool,
        binds: list[tuple[Path, Path]],
        binds_rw: list[tuple[Path, Path]],
    ) -> None:
        self.rootfs = rootfs
        self.binds = binds
        self.binds_rw = binds_rw
        self.runs: list[list[str]] = []
        _FakeContainer.instances.append(self)

    def __enter__(self) -> _FakeContainer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, argv: list[str], **kw: object) -> None:
        self.runs.append(argv)


def test_assemble_orchestrates_seed_emerge_dracut_squashfs_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    monkeypatch.setattr(asm, "extract_stage3", fake_extract)

    # Lista única de eventos p/ travar a ORDEM (não só a ocorrência): apply_portage
    # PRECISA preceder o emerge, senão a USE resolvida ≠ a dos binpkgs (§18.6).
    events: list[str] = []
    monkeypatch.setattr(
        asm, "apply_portage", lambda rootfs, recipe, *, variants_dir: events.append("apply_portage")
    )
    monkeypatch.setattr(asm, "bind_repos", lambda d: [])

    class _OrchContainer(_FakeContainer):
        def run(self, argv: list[str], **kw: object) -> None:
            events.append(argv[0])
            super().run(argv, **kw)

    monkeypatch.setattr(asm, "Container", _OrchContainer)

    sq_calls: list[tuple[Path, Path]] = []
    iso_calls: list[tuple[Path, Path, Path, Path]] = []

    def fake_squashfs(rootfs: Path, output: Path) -> Path:
        sq_calls.append((rootfs, output))
        output.write_bytes(b"SQ")
        return output

    def fake_iso(squashfs: Path, output: Path, *, kernel: Path, initramfs: Path) -> Path:
        iso_calls.append((squashfs, output, kernel, initramfs))
        output.parent.mkdir(parents=True, exist_ok=True)  # o build_iso real faz isto
        output.write_bytes(b"ISO")
        return output

    monkeypatch.setattr(image, "make_squashfs", fake_squashfs)
    monkeypatch.setattr(image, "build_iso", fake_iso)

    out = tmp_path / "dist" / "bentoo.iso"
    # sets=() de propósito: este teste exercita a ORQUESTRAÇÃO do assemble, e
    # install_sets falharia alto num set declarado sem arquivo curado no tmp_path.
    recipe = _recipe(sets=())
    result = Assembler(recipe, tmp_path / "binhost" / "znver5").assemble(out)

    assert result == out
    assert out.read_bytes() == b"ISO"
    # §18.6 — apply_portage estritamente ANTES do emerge (não só "foi chamado").
    assert events.index("apply_portage") < events.index("emerge")
    # ordem dentro do container: emerge --usepkgonly e depois dracut.
    inst = _FakeContainer.instances[0]
    assert inst.runs[0] == iso_emerge_argv(recipe)
    assert inst.runs[1][0] == "dracut" and "dmsquash-live" in inst.runs[1]
    # binhost montado RO no container (e nada RW) — fio condutor binhost_dir→Container.
    assert (tmp_path / "binhost" / "znver5", asm._BINHOST_DST) in inst.binds
    assert inst.binds_rw == []
    # kernel localizado e encadeado squashfs → ISO.
    assert sq_calls and iso_calls
    assert iso_calls[0][0] == sq_calls[0][1]  # build_iso recebe o squashfs produzido
    assert iso_calls[0][2].name == "vmlinuz-6.12.0-bentoo"
    # sucesso sem --keep → rootfs E squashfs intermediário removidos do scratch.
    assemble_dir = tmp_path / "scratch" / "assemble"
    assert not (assemble_dir / "znver5-kde-systemd").exists()
    assert not (assemble_dir / "znver5-kde-systemd.squashfs").exists()


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
    monkeypatch.setattr(asm, "bind_repos", lambda d: [])

    class _BoomContainer(_FakeContainer):
        def run(self, argv: list[str], **kw: object) -> None:
            raise RuntimeError("emerge --usepkgonly explodiu")

    monkeypatch.setattr(asm, "Container", _BoomContainer)

    rootfs = tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"
    with pytest.raises(RuntimeError, match="explodiu"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    assert rootfs.exists()  # preservado para depuração em falha


def test_assemble_keeps_rootfs_on_squashfs_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ramo distinto: falha PÓS-container (make_squashfs) também preserva o rootfs.
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
    monkeypatch.setattr(asm, "bind_repos", lambda d: [])
    monkeypatch.setattr(asm, "Container", _FakeContainer)  # run() é no-op (sucesso)

    def boom_squashfs(rootfs: Path, output: Path) -> Path:
        raise ImageError("mksquashfs: disco cheio")

    monkeypatch.setattr(image, "make_squashfs", boom_squashfs)

    rootfs = tmp_path / "scratch" / "assemble" / "znver5-kde-systemd"
    with pytest.raises(ImageError, match="disco cheio"):
        Assembler(_recipe(), tmp_path / "binhost").assemble(tmp_path / "out.iso")
    assert rootfs.exists()  # ImageError pós-container também mantém o rootfs


def test_install_sets_later_layer_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §4.2/§13 — mesmo set em dois layers: o posterior (flavor) vence a base.
    variants = tmp_path / "variants"
    (variants / "base" / "sets").mkdir(parents=True)
    (variants / "base" / "sets" / "graphics").write_text("BASE\n")
    (variants / "flavor" / "kde" / "sets").mkdir(parents=True)
    (variants / "flavor" / "kde" / "sets" / "graphics").write_text("FLAVOR\n")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    rootfs = tmp_path / "rootfs"

    _install_sets(rootfs, _recipe(sets=("graphics",)))

    # portage_layers = (base, arch/znver5, flavor/kde, init/systemd): flavor é posterior.
    assert (rootfs / "etc" / "portage" / "sets" / "graphics").read_text() == "FLAVOR\n"


# NB: o caminho privilegiado real (nspawn + emerge --usepkgonly + dracut +
# mksquashfs + grub-mkrescue) é host-gated (root + Gentoo + ferramentas); fica
# para o smoke-test de boot da Fase 1 (QEMU), não para o unit off-host.


# --- _install_sets: @refs transitivas, exclude e falha alta -------------------


def test_install_sets_follows_nested_set_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Portage expande `@outro-set` dentro de um set file, então instalar o
    # agregador exige instalar as folhas -- senão @base resolve para um alvo
    # inexistente DENTRO do container, longe da causa.
    variants = tmp_path / "variants"
    (variants / "base" / "sets").mkdir(parents=True)
    (variants / "base" / "sets" / "agg").write_text("@leaf\n")
    (variants / "base" / "sets" / "leaf").write_text("app-editors/nano\n")
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
    (variants / "base" / "sets").mkdir(parents=True)
    (variants / "base" / "sets" / "leaf").write_text(
        "media-video/vlc\napp-editors/nano  # com comentário\n"
    )
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))
    rootfs = tmp_path / "rootfs"

    _install_sets(rootfs, _recipe(sets=("leaf",), exclude=("media-video/vlc",)))

    written = (rootfs / "etc" / "portage" / "sets" / "leaf").read_text()
    assert "media-video/vlc" not in written.replace(
        "# shidashi: excluded by flavor/kde: media-video/vlc", ""
    )
    assert "app-editors/nano" in written  # o comentário inline não atrapalha
    assert "excluded by flavor/kde" in written  # a subtração fica registrada


def test_install_sets_raises_when_declared_set_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Antes era ignorado em silêncio e só falhava no emerge, dentro do container.
    variants = tmp_path / "variants"
    (variants / "base" / "sets").mkdir(parents=True)
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(variants))

    with pytest.raises(ResolveError, match="ausente"):
        _install_sets(tmp_path / "rootfs", _recipe(sets=("nao-existe",)))
