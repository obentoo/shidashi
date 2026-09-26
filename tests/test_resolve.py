"""UNIT + INTEGRAÇÃO de shidashi.resolve — o coração do fluxo pretend.

UNIT (determinista, CI não-Gentoo):
* ``_layer_dirs`` mapeia ``portage_layers`` → ``variants/<layer>/portage/`` (R3.1);
* ``apply_portage`` sobrepõe arquivos camada-a-camada num tmp dir, camadas
  posteriores sobrescrevendo as anteriores; layer ausente → ResolveError (R3.1);
* ``bind_repos`` recebe um DIRETÓRIO ``repos.conf/`` (estilo eselect-repo),
  itera os ``*.conf`` nele, parseia stanzas ``[<name>]`` / ``location = <path>``
  (stdlib ``configparser``), mapeia o ``location`` de cada repo declarado (sob o
  host ``/var/db/repos/<name>``) para o mesmo caminho no container como bind RO,
  e levanta ResolveError nomeando repo ausente (R3.2, R3.3, R6.3) — raiz de repos
  do host forçada via ``tmp_path`` (não dependemos do host real);
* ``parse_cycle_breaks`` extrai átomo+flag+sinal de saída capturada do emerge,
  incl. o ciclo ``libsdl2 ↔ pipewire ↔ ffmpeg`` (§18.2) (R5.2);
* ``parse_packages`` extrai a lista de átomos resolvidos da saída do
  ``emerge --pretend``, independente do parsing de ciclos (R5.2);
* ``CycleBreak``/``PretendReport`` são frozen pydantic;
* ``ResolveError`` carrega ``raw_output`` opcional (raw emerge em hard-conflict);
* ``pretend_resolve(..., keep=False)`` levanta ResolveError ANTES de qualquer
  trabalho quando não-root (R5.1, R6.1) — ``os.geteuid`` é monkeypatched.

INTEGRAÇÃO (host-gated, R5.1/R5.3/R5.4): pipeline real em ``v3 × minimal ×
systemd`` exige root+Gentoo → PULA fora do host privilegiado (Red diferido).

Contrato (design.md §resolve, refinado): Frozen pydantic
``CycleBreak(atom, flag, enable, raw_line)`` e ``PretendReport(arch, flavor,
init, packages, cycle_breaks, raw_output)``. ``bind_repos(repos_conf_dir: Path)``
(DIRETÓRIO de ``*.conf``). ``parse_packages(output: str) -> tuple[str, ...]``.
``ResolveError(msg, *, raw_output: str | None = None)`` expõe ``.raw_output``.
``pretend_resolve(arch, flavor, init, *, download=True, keep=False)``.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shidashi import resolve
from shidashi.recipe import ResolvedRecipe
from shidashi.resolve import (
    CycleBreak,
    PretendReport,
    ResolveError,
    _layer_dirs,
    apply_portage,
    apply_rootfs,
    bind_repos,
    parse_cycle_breaks,
    parse_packages,
    pretend_resolve,
)

_NEEDS_HOST = os.geteuid() != 0 or shutil.which("systemd-nspawn") is None
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST, reason="exige root + systemd-nspawn + stage3 seedado (host Gentoo)"
)

_LAYERS = ("base", "arch/v3", "flavor/minimal", "init/systemd")


def _recipe() -> ResolvedRecipe:
    # constrói um ResolvedRecipe mínimo com portage_layers conhecido; só esse
    # campo importa para _layer_dirs/apply_portage.

    return ResolvedRecipe(
        arch="v3",
        flavor="minimal",
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v3",
        rustflags="",
        cpu_flags_x86=(),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=_LAYERS,
    )


# --- ResolveError ------------------------------------------------------------


def test_resolve_error_is_exception_subclass() -> None:
    assert issubclass(ResolveError, Exception)


def test_resolve_error_carries_optional_raw_output() -> None:
    # raw_output é opcional: ausente por padrão (None), presente quando o erro
    # transporta a saída crua do emerge (hard-conflict, §Error Handling).
    plain = ResolveError("nope")
    assert getattr(plain, "raw_output", None) is None
    with_raw = ResolveError("hard conflict", raw_output="!!! conflito\n...emerge...")
    assert with_raw.raw_output == "!!! conflito\n...emerge..."


# --- _layer_dirs (R3.1) ------------------------------------------------------


def test_layer_dirs_maps_each_layer(tmp_path: Path) -> None:
    dirs = _layer_dirs(_recipe(), tmp_path)
    expected = [tmp_path / layer / "portage" for layer in _LAYERS]
    assert dirs == expected


# --- apply_portage layering (R3.1) -------------------------------------------


def _seed_layer(variants: Path, layer: str, rel: str, content: str) -> None:
    target = variants / layer / "portage" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_apply_portage_concatenates_make_conf_and_keeps_unique_files(tmp_path: Path) -> None:
    # make.conf é UM arquivo lido pelo shell e os layers trazem FRAGMENTOS, logo
    # ele é concatenado — não sobrescrito. Sobrescrever fazia o make.conf de 133
    # linhas da base virar o fragmento de 6 linhas do init (F28).
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", "FROM_BASE")
    _seed_layer(variants, "arch/v3", "package.use/arch", "ARCH")
    _seed_layer(variants, "flavor/minimal", "package.use/flavor", "FLAVOR")
    _seed_layer(variants, "init/systemd", "make.conf", "FROM_INIT")

    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)

    apply_portage(rootfs, _recipe(), variants_dir=variants)

    portage = rootfs / "etc" / "portage"
    make_conf = (portage / "make.conf").read_text(encoding="utf-8")
    assert "FROM_BASE" in make_conf
    assert "FROM_INIT" in make_conf
    # e na ORDEM dos layers, que é o que dá sentido ao "último vence" do shell
    assert make_conf.index("FROM_BASE") < make_conf.index("FROM_INIT")
    # o arquivo montado nomeia a origem de cada fragmento
    assert "layer: base" in make_conf
    assert "layer: init/systemd" in make_conf

    # arquivos exclusivos de camadas intermediárias seguem preservados
    assert (portage / "package.use" / "arch").read_text(encoding="utf-8") == "ARCH"
    assert (portage / "package.use" / "flavor").read_text(encoding="utf-8") == "FLAVOR"


def test_apply_portage_assembled_make_conf_gives_the_last_assignment(tmp_path: Path) -> None:
    # Dentro do arquivo montado vale a regra do shell: a última atribuição vence.
    # É esse o efeito de especialização do eixo arch sobre a base.
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", 'COMMON_FLAGS="-O2"\nUSE="a b"\n')
    _seed_layer(variants, "arch/v3", "make.conf", 'COMMON_FLAGS="-march=x86-64-v3 -O2"\n')
    _seed_layer(variants, "flavor/minimal", "package.use/keep", "KEEP")
    _seed_layer(variants, "init/systemd", "make.conf", 'USE="${USE} systemd"\n')

    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    apply_portage(rootfs, _recipe(), variants_dir=variants)

    make_conf = rootfs / "etc" / "portage" / "make.conf"
    out = subprocess.run(
        ["bash", "-c", f'. "{make_conf}"; printf "%s|%s" "$COMMON_FLAGS" "$USE"'],
        capture_output=True,
        text=True,
        check=True,
    )
    flags, use = out.stdout.split("|")
    assert flags == "-march=x86-64-v3 -O2"          # arch venceu a base
    assert sorted(use.split()) == ["a", "b", "systemd"]  # init SOMOU, não trocou


def test_apply_portage_raises_when_two_layers_provide_the_same_file(tmp_path: Path) -> None:
    # Fora do make.conf, esses caminhos são DIRETÓRIOS que o Portage lê como
    # união: dois layers no mesmo caminho não se combinam, um apaga o outro. Era
    # perda silenciosa — package.use/system caía de 69 linhas para 4 (F28).
    variants = tmp_path / "variants"
    _seed_layer(variants, "base", "make.conf", "BASE")
    _seed_layer(variants, "base", "package.use/system", "SESSENTA E NOVE LINHAS")
    _seed_layer(variants, "arch/v3", "package.use/arch", "ARCH")
    _seed_layer(variants, "flavor/minimal", "package.use/flavor", "FLAVOR")
    _seed_layer(variants, "init/systemd", "package.use/system", "QUATRO LINHAS")

    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)

    with pytest.raises(ResolveError) as excinfo:
        apply_portage(rootfs, _recipe(), variants_dir=variants)
    msg = str(excinfo.value)
    # o erro precisa nomear OS DOIS layers e o caminho, senão não é acionável
    assert "package.use/system" in msg
    assert "base" in msg
    assert "init/systemd" in msg


# --- apply_rootfs ------------------------------------------------------------


def _seed_rootfs_file(variants: Path, layer: str, rel: str, content: str) -> None:
    target = variants / layer / "rootfs" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_apply_rootfs_copies_each_layers_tree_over_the_root(tmp_path: Path) -> None:
    """A layer's rootfs/ lands at / -- the stage3's locale.gen (~500 commented
    entries) is replaced by the curated one before the bootstrap's locale-gen."""
    variants = tmp_path / "variants"
    _seed_rootfs_file(variants, "base", "etc/locale.gen", "en_US.UTF-8 UTF-8\n")
    _seed_rootfs_file(variants, "init/systemd", "etc/systemd/x.conf", "X")
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc" / "locale.gen").write_text("# stage3 default\n", encoding="utf-8")

    copied = apply_rootfs(rootfs, _recipe(), variants_dir=variants)

    assert (rootfs / "etc/locale.gen").read_text(encoding="utf-8") == "en_US.UTF-8 UTF-8\n"
    assert (rootfs / "etc/systemd/x.conf").read_text(encoding="utf-8") == "X"
    assert copied == ("/etc/locale.gen", "/etc/systemd/x.conf")


def test_apply_rootfs_later_layer_wins_and_missing_trees_are_skipped(tmp_path: Path) -> None:
    variants = tmp_path / "variants"
    _seed_rootfs_file(variants, "base", "etc/motd", "base")
    _seed_rootfs_file(variants, "init/systemd", "etc/motd", "init")
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()

    apply_rootfs(rootfs, _recipe(), variants_dir=variants)  # arch/flavor have no rootfs/

    assert (rootfs / "etc/motd").read_text(encoding="utf-8") == "init"


def test_apply_portage_missing_layer_raises(tmp_path: Path) -> None:
    variants = tmp_path / "variants"
    # apenas base existe; arch/v3 ausente → ResolveError
    _seed_layer(variants, "base", "make.conf", "X")
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    with pytest.raises(ResolveError):
        apply_portage(rootfs, _recipe(), variants_dir=variants)


# --- bind_repos (R3.2, R3.3, R6.3) -------------------------------------------
#
# Contrato refinado: bind_repos recebe um DIRETÓRIO repos.conf/ (estilo
# eselect-repo). Itera os *.conf, parseia stanzas [<name>] / location = <path>
# (stdlib configparser) e, para cada repo declarado, exige que seu location de
# host exista sob /var/db/repos/<name>, mapeando-o RO ao mesmo caminho no
# container. A raiz de repos do host é forçada via monkeypatch para o tmp.

_ESELECT_REPO_CONF = """\
[gentoo]
location = {root}/gentoo

[bentoo]
location = {root}/bentoo
"""


def _write_repos_conf_dir(tmp_path: Path, host_repos: Path) -> Path:
    """Cria um diretório repos.conf/ com um eselect-repo.conf de duas stanzas."""
    repos_conf_dir = tmp_path / "repos.conf"
    repos_conf_dir.mkdir()
    (repos_conf_dir / "eselect-repo.conf").write_text(
        _ESELECT_REPO_CONF.format(root=host_repos), encoding="utf-8"
    )
    return repos_conf_dir


def test_bind_repos_declares_ro_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host_repos = tmp_path / "var" / "db" / "repos"
    (host_repos / "gentoo").mkdir(parents=True)
    (host_repos / "bentoo").mkdir(parents=True)
    # redireciona a raiz de repos do host para o tmp (não dependemos do host real)
    monkeypatch.setattr(resolve, "_HOST_REPOS_ROOT", host_repos, raising=False)

    repos_conf_dir = _write_repos_conf_dir(tmp_path, host_repos)

    pairs = bind_repos(repos_conf_dir)
    srcs = {src for src, _dst in pairs}
    assert host_repos / "gentoo" in srcs
    assert host_repos / "bentoo" in srcs
    # cada par mapeia o location do host → o MESMO caminho no container
    for src, dst in pairs:
        assert dst == src


def test_bind_repos_missing_repo_raises_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_repos = tmp_path / "var" / "db" / "repos"
    (host_repos / "gentoo").mkdir(parents=True)  # bentoo declarado mas ausente
    monkeypatch.setattr(resolve, "_HOST_REPOS_ROOT", host_repos, raising=False)

    repos_conf_dir = _write_repos_conf_dir(tmp_path, host_repos)

    with pytest.raises(ResolveError) as excinfo:
        bind_repos(repos_conf_dir)
    assert "bentoo" in str(excinfo.value)


# --- parse_cycle_breaks (R5.2) — núcleo da curadoria §18.2 -------------------

# Fixture inspirada na saída real do emerge ao reportar dependências circulares
# com sugestões de "change USE". Contém o ciclo libsdl2 ↔ pipewire ↔ ffmpeg.
_EMERGE_CYCLE = """\
These are the packages that would be merged, in order:

Calculating dependencies... done!

!!! Multiple package instances within a single package slot have been pulled
!!! into the dependency graph, resulting in a slot conflict:

  circular dependencies:

    (media-libs/libsdl2-2.30.5:0/0::gentoo, ebuild scheduled for merge) depends on
     (media-video/pipewire-1.2.1:0/0.4::gentoo, ebuild scheduled for merge) (buildtime)
      (media-video/ffmpeg-6.1.1:0/58.60.60::gentoo, ebuild scheduled for merge) (buildtime)
       (media-libs/libsdl2-2.30.5:0/0::gentoo, ebuild scheduled for merge) (buildtime)

   It might be possible to break this cycle
   by applying any of the following changes:
   - media-libs/libsdl2-2.30.5 (Change USE: -pipewire)
   - media-video/ffmpeg-6.1.1 (Change USE: +sdl)
"""

# Fixture de saída "limpa" do emerge --pretend: a lista de pacotes resolvidos,
# uma linha [ebuild ...] por átomo, sem ciclos. Captura o formato real do
# emerge --pretend --emptytree @world.
_EMERGE_PACKAGES = """\
These are the packages that would be merged, in order:

Calculating dependencies... done!

[ebuild  N     ] sys-libs/zlib-1.3.1:0/1::gentoo  USE="minizip" 0 KiB
[ebuild  N     ] dev-libs/openssl-3.3.1:0/3::gentoo  USE="asm" 15123 KiB
[ebuild  N     ] sys-apps/portage-3.0.66.1::gentoo  0 KiB

Total: 3 packages (3 new), Size of downloads: 15138 KiB
"""


def test_parse_cycle_breaks_extracts_atom_flag_sign() -> None:
    breaks = parse_cycle_breaks(_EMERGE_CYCLE)
    assert isinstance(breaks, tuple)
    assert len(breaks) == 2
    by_atom = {b.atom: b for b in breaks}
    # "-pipewire" → desabilitar
    sdl = by_atom["media-libs/libsdl2-2.30.5"]
    assert sdl.flag == "pipewire"
    assert sdl.enable is False
    # "+sdl" → habilitar
    ff = by_atom["media-video/ffmpeg-6.1.1"]
    assert ff.flag == "sdl"
    assert ff.enable is True


def test_parse_cycle_breaks_empty_on_clean_output() -> None:
    assert parse_cycle_breaks(_EMERGE_PACKAGES) == ()


# --- parse_packages (R5.2) — lista de átomos resolvidos, independente de ciclos


def test_parse_packages_extracts_resolved_atom_list() -> None:
    pkgs = parse_packages(_EMERGE_PACKAGES)
    assert isinstance(pkgs, tuple)
    # extrai os átomos das linhas [ebuild ...], independente do parsing de ciclos
    assert pkgs == (
        "sys-libs/zlib-1.3.1",
        "dev-libs/openssl-3.3.1",
        "sys-apps/portage-3.0.66.1",
    )


def test_parse_packages_empty_when_no_ebuild_lines() -> None:
    # saída sem linhas [ebuild ...] → lista vazia (não levanta)
    assert parse_packages("Calculating dependencies... done!\n") == ()


# --- modelos frozen ----------------------------------------------------------


def test_cycle_break_is_frozen() -> None:
    cb = CycleBreak(atom="x/y-1", flag="foo", enable=True, raw_line="raw")
    with pytest.raises(Exception):  # noqa: B017
        cb.flag = "bar"


def test_pretend_report_holds_packages_and_breaks() -> None:
    cb = CycleBreak(atom="x/y-1", flag="foo", enable=False, raw_line="raw")
    rep = PretendReport(
        arch="v3",
        flavor="minimal",
        init="systemd",
        packages=("x/y-1", "a/b-2"),
        cycle_breaks=(cb,),
        raw_output="...",
    )
    assert rep.packages == ("x/y-1", "a/b-2")
    assert rep.cycle_breaks[0].flag == "foo"


# --- non-root guard (R5.1, R6.1) — falha antes de qualquer trabalho ----------


def test_pretend_resolve_non_root_raises_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ResolveError) as excinfo:
        pretend_resolve("v3", "minimal", "systemd", keep=False)
    # mensagem acionável menciona root
    assert "root" in str(excinfo.value).lower()


# --- INTEGRAÇÃO host-gated (R5.1, R5.3, R5.4) --------------------------------


@_skip_privileged
def test_pretend_resolve_returns_nonempty_package_list() -> None:
    report = pretend_resolve("v3", "minimal", "systemd")
    assert isinstance(report, PretendReport)
    assert len(report.packages) > 0
