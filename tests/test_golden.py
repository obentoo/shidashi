"""Golden integration cases — merge real de ``variants/`` (R11.3).

Ancora as duas combinações canônicas da story em valores resolvidos concretos,
sobre a árvore ``variants/`` realmente enviada:

* ``v3 × minimal × systemd`` — perfil systemd, sem desktop (phase omitida).
* ``v3 × kde × systemd``     — perfil systemd, USE qt6/kde/wayland, set kde.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shidashi import config
from shidashi.recipe import (
    ResolvedRecipe,
    load_arch,
    load_base,
    load_flavor,
    load_init,
    merge,
)

_VARIANTS_DIR = Path(__file__).resolve().parent.parent / "variants"


@pytest.fixture(autouse=True)
def _point_at_shipped_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(_VARIANTS_DIR))


def _resolve(arch: str, flavor: str, init: str) -> ResolvedRecipe:
    return merge(
        load_base(config.base_path()),
        load_arch(config.recipe_path("arch", arch)),
        load_flavor(config.recipe_path("flavor", flavor)),
        load_init(config.recipe_path("init", init)),
    )


def test_golden_v3_minimal_systemd() -> None:
    r = _resolve("v3", "minimal", "systemd")
    assert r.profile == "default/linux/amd64/23.0/no-multilib/systemd"
    assert r.use.enabled == ("systemd",)
    assert r.use.disabled == ("gnome", "gtk", "kde", "qt6")
    assert r.sets == ("graphics", "bentoo-apps")
    # minimal tem sets vazio → a phase `desktop` é omitida (R2.5)
    assert tuple(p.name for p in r.phases) == ("rebuild", "graphics", "apps")
    assert "desktop" not in {p.name for p in r.phases}
    assert r.tier == 1
    assert r.goamd64 == "v3"
    assert r.runnable_on_build_host is True
    assert r.portage_layers == ("base", "arch/v3", "flavor/minimal", "init/systemd")


def test_golden_v3_kde_systemd() -> None:
    r = _resolve("v3", "kde", "systemd")
    assert r.profile == "default/linux/amd64/23.0/no-multilib/systemd"
    # USE ordenado/deduplicado; kde adiciona qt6/kde/wayland, init adiciona systemd
    assert r.use.enabled == ("kde", "qt6", "systemd", "wayland")
    assert {"qt6", "kde", "wayland"} <= set(r.use.enabled)
    assert r.use.disabled == ("gnome", "gtk", "webkit")
    # set kde entra na união ordenada após os sets de base
    assert r.sets == ("graphics", "bentoo-apps", "kde")
    assert "kde" in r.sets
    # kde tem desktop → a phase `desktop` permanece
    assert tuple(p.name for p in r.phases) == ("rebuild", "graphics", "desktop", "apps")
    assert r.portage_layers == ("base", "arch/v3", "flavor/kde", "init/systemd")
