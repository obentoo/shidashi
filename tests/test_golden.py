"""Golden integration cases — the real ``variants/`` tree, merged (R11.3, D24).

Anchors the two canonical images to concrete resolved values:

* ``v3 × minimal × systemd`` — the chain ``base → minimal``, console only.
* ``v3 × kde × systemd``     — the chain ``base → minimal → desktop → kde``.

The final configuration and sets of both were proven identical to the
pre-D24 model on 2026-09-26 (apply_portage + install_sets over all ten
flavor × init combinations); what changed is the ORDER in which the chain
installs them, which is the point of the stage tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shidashi import config
from shidashi.phases import phase_target
from shidashi.recipe import ResolvedRecipe

_VARIANTS_DIR = Path(__file__).resolve().parent.parent / "variants"


@pytest.fixture(autouse=True)
def _point_at_shipped_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(_VARIANTS_DIR))


def _resolve(arch: str, target: str, init: str) -> ResolvedRecipe:
    return config.load_recipe(arch, target, init)


def _targets(r: ResolvedRecipe) -> dict[str, tuple[str, ...]]:
    return {p.name: phase_target(p, r) for p in r.phases}


def test_golden_v3_minimal_systemd() -> None:
    r = _resolve("v3", "minimal", "systemd")
    assert r.flavor == "minimal"
    assert r.stages == ("base", "minimal")
    assert r.profile == "default/linux/amd64/23.0/no-multilib/systemd"
    assert r.sets == ("base", "extra-system")
    assert "gpu" not in r.sets
    assert _targets(r) == {
        "base": ("@world", "@base"),
        "minimal": ("@extra-system",),
    }
    base, minimal = r.phases
    assert base.emptytree and not base.ships
    assert minimal.ships and not minimal.emptytree
    assert base.layers == ("base", "arch/v3", "init/systemd")
    assert minimal.layers == ("base", "arch/v3", "minimal", "init/systemd")
    assert r.portage_layers == ("base", "arch/v3", "minimal", "init/systemd")
    assert r.tier == 1
    assert r.goamd64 == "v3"
    assert r.runnable_on_build_host is True


def test_golden_v3_kde_systemd() -> None:
    r = _resolve("v3", "kde", "systemd")
    assert r.flavor == "kde"
    assert r.stages == ("base", "minimal", "desktop", "kde")
    assert r.profile == "default/linux/amd64/23.0/no-multilib/systemd"
    assert r.sets == (
        "base", "extra-system",
        "gpu", "fonts", "desktop-int", "audio", "vpn", "print", "sandbox",
        "kde", "extra-desktop", "extra-media", "extra-dev", "extra-virt", "kde-dm-plasma",
    )
    assert _targets(r) == {
        "base": ("@world", "@base"),
        "minimal": ("@extra-system",),
        "desktop": ("@gpu", "@fonts", "@desktop-int", "@audio", "@vpn", "@print", "@sandbox"),
        # init_sets: the display manager joins the flavor phase, per init
        "flavor": (
            "@kde", "@extra-desktop", "@extra-media", "@extra-dev", "@extra-virt",
            "@kde-dm-plasma",
        ),
    }
    # minimal ships on its way to kde: the graphical stages start from it settled
    assert tuple(p.name for p in r.phases if p.ships) == ("minimal", "flavor")
    # the configuration grows stage by stage; the graphical USE enters at desktop
    assert [p.layers[-2] for p in r.phases] == ["arch/v3", "minimal", "desktop", "flavor/kde"]
    assert r.portage_layers == (
        "base", "arch/v3", "minimal", "desktop", "flavor/kde", "init/systemd",
    )
