"""Testes da geração de specs do Catalyst (shidashi.catalyst) — story 005.

A função ``render_specs`` é **pura** (receita + stamps → texto dos specs), no
mesmo idioma de ``seed.stage3_url``: unit-testada em CI não-Gentoo, sem I/O nem
relógio/aleatoriedade. A invocação privilegiada do ``catalyst`` (Task 4) é
isolada noutro helper e testada por monkeypatch.
"""

from pathlib import Path

from shidashi.catalyst import render_specs
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
