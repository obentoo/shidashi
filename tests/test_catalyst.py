"""Testes da geração de specs do Catalyst (shidashi.catalyst) — story 005.

A função ``render_specs`` é **pura** (receita + stamps → texto dos specs), no
mesmo idioma de ``seed.stage3_url``: unit-testada em CI não-Gentoo, sem I/O nem
relógio/aleatoriedade. A invocação privilegiada do ``catalyst`` (Task 4) é
isolada noutro helper e testada por monkeypatch.
"""

import hashlib
from pathlib import Path

import pytest

import shidashi.catalyst as cat
from shidashi.catalyst import CatalystError, build_stage3_catalyst, render_specs
from tests.test_merge import make_arch, make_base, make_flavor, make_init

from shidashi.recipe import merge  # isort: skip


def _resolved(*, arch: str = "znver5", seed_source: str = "catalyst"):
    return merge(
        make_base(),
        make_arch(arch=arch, seed_source=seed_source),
        make_flavor(),
        make_init(),
    )


def _parse(spec_text: str) -> dict[str, str]:
    """Parseia o texto 'chave: valor' de um spec do catalyst num dict."""
    out: dict[str, str] = {}
    for line in spec_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


_CONFDIR = Path("variants/arch/znver5/portage")
_KW = dict(seed_subpath="amd64/stage3-amd64-nomultilib-systemd-20260524T170105Z",
           version_stamp="20260524T170105Z", snapshot_treeish="abc123", confdir=_CONFDIR)


def test_render_specs_three_targets_in_order() -> None:
    # R2.1 — um spec por target, na ordem stage1 → stage2 → stage3.
    specs = render_specs(_resolved(), **_KW)
    assert list(specs.keys()) == ["stage1", "stage2", "stage3"]
    assert _parse(specs["stage1"])["target"] == "stage1"
    assert _parse(specs["stage2"])["target"] == "stage2"
    assert _parse(specs["stage3"])["target"] == "stage3"


def test_render_specs_stage1_source_is_bootstrap_seed() -> None:
    # R2.2 — stage1.source_subpath aponta para a semente genérica de bootstrap.
    specs = render_specs(_resolved(), **_KW)
    assert _parse(specs["stage1"])["source_subpath"] == _KW["seed_subpath"]


def test_render_specs_source_subpath_chained() -> None:
    # R2.3 — stage2 parte do stage1 construído; stage3 parte do stage2.
    specs = render_specs(_resolved(arch="znver5"), **_KW)
    stamp = _KW["version_stamp"]
    assert _parse(specs["stage2"])["source_subpath"] == f"shidashi/znver5/stage1-amd64-{stamp}"
    assert _parse(specs["stage3"])["source_subpath"] == f"shidashi/znver5/stage2-amd64-{stamp}"


def test_render_specs_portage_confdir_is_arch_portage_dir() -> None:
    # R2.4 — portage_confdir reusa o diretório portage do arch.
    specs = render_specs(_resolved(), **_KW)
    for target in ("stage1", "stage2", "stage3"):
        assert _parse(specs[target])["portage_confdir"] == str(_CONFDIR)


def test_render_specs_rel_type_and_subarch() -> None:
    # R2.5 — rel_type derivado do arch; subarch no baseline genérico amd64.
    specs = render_specs(_resolved(arch="znver5"), **_KW)
    parsed = _parse(specs["stage3"])
    assert parsed["rel_type"] == "shidashi/znver5"
    assert parsed["subarch"] == "amd64"


def test_render_specs_is_deterministic() -> None:
    # R2.6 — mesmas entradas → texto byte-idêntico (sem relógio/aleatoriedade).
    a = render_specs(_resolved(), **_KW)
    b = render_specs(_resolved(), **_KW)
    assert a == b


# --- build_stage3_catalyst (R3.1–R3.5, R4.1, R4.3; integração monkeypatch) ----

_SEED = Path("/var/cache/shidashi/stage3-amd64-nomultilib-systemd-20260524T170105Z.tar.xz")
_STAMP = "20260524T170105Z"


def _build_kw(tmp_path: Path) -> dict:
    return dict(
        version_stamp=_STAMP,
        snapshot_treeish="abc123",
        confdir=Path("variants/arch/znver5/portage"),
        scratch_dir=tmp_path / "scratch",
        output_dir=tmp_path / "out",
    )


def _stage3_name() -> str:
    return f"stage3-amd64-{_STAMP}.tar.xz"


def test_build_invokes_catalyst_per_stage_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R3.2 — um catalyst por spec, na ordem stage1→2→3; R3.5/R4.1 retorna tarball+sha.
    monkeypatch.setattr(cat.shutil, "which", lambda _: "/usr/bin/catalyst")
    out = tmp_path / "out"
    calls: list[str] = []

    def fake_run(spec: Path) -> None:
        calls.append(spec.name)
        if spec.name == "stage3.spec":
            (out / _stage3_name()).write_bytes(b"STAGE3")

    monkeypatch.setattr(cat, "_run_catalyst", fake_run)
    tarball, sha = build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))
    assert calls == ["stage1.spec", "stage2.spec", "stage3.spec"]
    assert tarball == out / _stage3_name()
    assert sha == hashlib.sha512(b"STAGE3").hexdigest()


def test_build_missing_catalyst_raises_before_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R3.3 — catalyst ausente no PATH falha ANTES de qualquer build.
    monkeypatch.setattr(cat.shutil, "which", lambda _: None)
    calls: list[str] = []
    monkeypatch.setattr(cat, "_run_catalyst", lambda s: calls.append(s.name))
    with pytest.raises(CatalystError, match="catalyst"):
        build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))
    assert calls == []


def test_build_aborts_at_failing_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R3.4 — saída não-zero aborta no stage que falhou; stage3 não é tentado.
    monkeypatch.setattr(cat.shutil, "which", lambda _: "/usr/bin/catalyst")
    seen: list[str] = []

    def fake_run(spec: Path) -> None:
        seen.append(spec.name)
        if spec.name == "stage2.spec":
            raise CatalystError("boom stage2")

    monkeypatch.setattr(cat, "_run_catalyst", fake_run)
    with pytest.raises(CatalystError, match="boom stage2"):
        build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))
    assert seen == ["stage1.spec", "stage2.spec"]


def test_build_missing_output_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # catalyst "rodou" mas não produziu o stage3 esperado → erro claro.
    monkeypatch.setattr(cat.shutil, "which", lambda _: "/usr/bin/catalyst")
    monkeypatch.setattr(cat, "_run_catalyst", lambda s: None)
    with pytest.raises(CatalystError, match="não produziu|stage3"):
        build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))


def test_build_cached_sha512_mismatch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R4.3 — tarball reusado cujo sha512 difere do pin persistido → falha.
    monkeypatch.setattr(cat.shutil, "which", lambda _: "/usr/bin/catalyst")
    out = tmp_path / "out"

    def fake_run(spec: Path) -> None:
        if spec.name == "stage3.spec":
            (out / _stage3_name()).write_bytes(b"STAGE3")

    monkeypatch.setattr(cat, "_run_catalyst", fake_run)
    with pytest.raises(CatalystError, match="sha512"):
        build_stage3_catalyst(
            _resolved(), _SEED, expected_sha512="deadbeef", **_build_kw(tmp_path)
        )
