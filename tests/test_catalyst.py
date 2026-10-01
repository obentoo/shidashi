"""Tests for Catalyst spec generation (shidashi.catalyst) — story 005.

The ``render_specs`` function is **pure** (recipe + stamps → spec text), in the
same idiom as ``seed.stage3_url``: unit-tested on non-Gentoo CI, with no I/O and no
clock/randomness. The privileged invocation of ``catalyst`` (Task 4) is
isolated in another helper and tested via monkeypatch.
"""

import hashlib
import shutil
from pathlib import Path
from typing import TypedDict

import pytest

import shidashi.catalyst as cat
from shidashi.catalyst import CatalystError, build_stage3_catalyst, render_specs
from shidashi.recipe import ResolvedRecipe, merge
from tests.test_merge import make_arch, make_base, make_chain, make_init


def _resolved(*, arch: str = "znver5", seed_source: str = "catalyst") -> ResolvedRecipe:
    return merge(
        make_base(),
        make_arch(arch=arch, seed_source=seed_source),
        make_chain(),
        make_init(),
    )


def _parse(spec_text: str) -> dict[str, str]:
    """Parse the 'key: value' text of a catalyst spec into a dict."""
    out: dict[str, str] = {}
    for line in spec_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


_CONFDIR = Path("variants/arch/znver5/portage")


class _SpecKW(TypedDict):
    seed_subpath: str
    version_stamp: str
    snapshot_treeish: str
    confdir: Path


_KW: _SpecKW = {
    "seed_subpath": "amd64/stage3-amd64-nomultilib-systemd-20260524T170105Z",
    "version_stamp": "20260524T170105Z",
    "snapshot_treeish": "abc123",
    "confdir": _CONFDIR,
}


def test_render_specs_three_targets_in_order() -> None:
    # R2.1 — one spec per target, in the order stage1 → stage2 → stage3.
    specs = render_specs(_resolved(), **_KW)
    assert list(specs.keys()) == ["stage1", "stage2", "stage3"]
    assert _parse(specs["stage1"])["target"] == "stage1"
    assert _parse(specs["stage2"])["target"] == "stage2"
    assert _parse(specs["stage3"])["target"] == "stage3"


def test_render_specs_stage1_source_is_bootstrap_seed() -> None:
    # R2.2 — stage1.source_subpath points to the generic bootstrap seed.
    specs = render_specs(_resolved(), **_KW)
    assert _parse(specs["stage1"])["source_subpath"] == _KW["seed_subpath"]


def test_render_specs_source_subpath_chained() -> None:
    # R2.3 — stage2 starts from the built stage1; stage3 starts from stage2.
    specs = render_specs(_resolved(arch="znver5"), **_KW)
    stamp = _KW["version_stamp"]
    assert _parse(specs["stage2"])["source_subpath"] == f"shidashi/znver5/stage1-amd64-{stamp}"
    assert _parse(specs["stage3"])["source_subpath"] == f"shidashi/znver5/stage2-amd64-{stamp}"


def test_render_specs_portage_confdir_is_arch_portage_dir() -> None:
    # R2.4 — portage_confdir reuses the arch's portage directory.
    specs = render_specs(_resolved(), **_KW)
    for target in ("stage1", "stage2", "stage3"):
        assert _parse(specs[target])["portage_confdir"] == str(_CONFDIR)


def test_render_specs_rel_type_and_subarch() -> None:
    # R2.5 — rel_type derived from the arch; subarch at the generic amd64 baseline.
    specs = render_specs(_resolved(arch="znver5"), **_KW)
    parsed = _parse(specs["stage3"])
    assert parsed["rel_type"] == "shidashi/znver5"
    assert parsed["subarch"] == "amd64"


def test_render_specs_is_deterministic() -> None:
    # R2.6 — same inputs → byte-identical text (no clock/randomness).
    a = render_specs(_resolved(), **_KW)
    b = render_specs(_resolved(), **_KW)
    assert a == b


# --- build_stage3_catalyst (R3.1–R3.5, R4.1, R4.3; monkeypatch integration) ----

_SEED = Path("/var/cache/shidashi/stage3-amd64-nomultilib-systemd-20260524T170105Z.tar.xz")
_STAMP = "20260524T170105Z"


class _BuildKW(TypedDict):
    version_stamp: str
    snapshot_treeish: str
    confdir: Path
    scratch_dir: Path
    output_dir: Path


def _build_kw(tmp_path: Path) -> _BuildKW:
    return {
        "version_stamp": _STAMP,
        "snapshot_treeish": "abc123",
        "confdir": Path("variants/arch/znver5/portage"),
        "scratch_dir": tmp_path / "scratch",
        "output_dir": tmp_path / "out",
    }


def _stage3_name() -> str:
    return f"stage3-amd64-{_STAMP}.tar.xz"


def test_build_invokes_catalyst_per_stage_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R3.2 — one catalyst per spec, in the order stage1→2→3; R3.5/R4.1 returns tarball+sha.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catalyst")
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
    # R3.3 — catalyst missing from PATH fails BEFORE any build.
    monkeypatch.setattr(shutil, "which", lambda _: None)
    calls: list[str] = []
    monkeypatch.setattr(cat, "_run_catalyst", lambda s: calls.append(s.name))
    with pytest.raises(CatalystError, match="catalyst"):
        build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))
    assert calls == []


def test_build_aborts_at_failing_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # R3.4 — a non-zero exit aborts at the failing stage; stage3 is not attempted.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catalyst")
    seen: list[str] = []

    def fake_run(spec: Path) -> None:
        seen.append(spec.name)
        if spec.name == "stage2.spec":
            raise CatalystError("boom stage2")

    monkeypatch.setattr(cat, "_run_catalyst", fake_run)
    with pytest.raises(CatalystError, match="boom stage2"):
        build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))
    assert seen == ["stage1.spec", "stage2.spec"]


def test_build_missing_output_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # catalyst "ran" but did not produce the expected stage3 → clear error.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catalyst")
    monkeypatch.setattr(cat, "_run_catalyst", lambda s: None)
    with pytest.raises(CatalystError, match="did not produce|stage3"):
        build_stage3_catalyst(_resolved(), _SEED, **_build_kw(tmp_path))


def test_build_cached_sha512_mismatch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # R4.3 — a reused tarball whose sha512 differs from the persisted pin → failure.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/catalyst")
    out = tmp_path / "out"

    def fake_run(spec: Path) -> None:
        if spec.name == "stage3.spec":
            (out / _stage3_name()).write_bytes(b"STAGE3")

    monkeypatch.setattr(cat, "_run_catalyst", fake_run)
    with pytest.raises(CatalystError, match="sha512"):
        build_stage3_catalyst(_resolved(), _SEED, expected_sha512="deadbeef", **_build_kw(tmp_path))
