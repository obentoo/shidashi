"""Tests for the guarded integration with Portage (shidashi.portage_api).

UNIT: deterministic regardless of the host. The Portage state is forced
via ``monkeypatch`` on ``shidashi.portage_api`` — we never depend on the host having
(or not having) ``sys-apps/portage`` installed. It checks that:

* importing the module never raises and ``PORTAGE_AVAILABLE`` is a ``bool`` (R7.1);
* with Portage absent, ``require_portage`` and the read helpers raise
  :class:`PortageUnavailableError` (R7.2);
* with Portage present (fake), the gate opens and ``require_portage`` returns the
  module.

NB: task T7.2 WILL ADD an integration test to this file (importing
``shidashi.recipe``/``shidashi.cli`` with Portage absent). The file is kept
extensible; T7.2 is not implemented here.
"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

import shidashi.portage_api as portage_api
from shidashi.portage_api import (
    PortageUnavailableError,
    configured_repos,
    portage_version,
    require_portage,
)
from tests._variants_tree import write_variants


@pytest.fixture
def portage_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the 'non-Gentoo host' state in shidashi.portage_api."""
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", False)
    monkeypatch.setattr(portage_api, "_portage", None)


# --- import is safe and PORTAGE_AVAILABLE is a bool (R7.1) ------------------


def test_import_never_raises_and_flag_is_bool() -> None:
    # the module was already imported at the top without an exception; reinforce the contract
    import importlib

    reloaded = importlib.import_module("shidashi.portage_api")
    assert isinstance(reloaded.PORTAGE_AVAILABLE, bool)


def test_unavailable_error_is_exception_subclass() -> None:
    assert issubclass(PortageUnavailableError, Exception)


# --- Portage absent: the gate and the helpers raise (R7.2) ------------------


def test_require_portage_raises_when_absent(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError):
        require_portage()


def test_require_portage_message_is_actionable(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError) as excinfo:
        require_portage()
    msg = str(excinfo.value)
    assert "Gentoo" in msg
    assert "sys-apps/portage" in msg


def test_portage_version_raises_when_absent(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError):
        portage_version()


def test_configured_repos_raises_when_absent(portage_absent: None) -> None:
    with pytest.raises(PortageUnavailableError):
        configured_repos()


# --- defensive guard: flag True but module None still raises ---------------


def test_require_portage_raises_when_flag_true_but_module_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", None)
    with pytest.raises(PortageUnavailableError):
        require_portage()


# --- Portage present (fake): the gate opens and returns the module ---------


def test_require_portage_returns_module_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(VERSION="3.0.66")
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake)
    # the gate opens and returns the configured module object (proved via an attribute,
    # avoiding an identity check between ModuleType and SimpleNamespace)
    returned = require_portage()
    assert getattr(returned, "VERSION", None) == "3.0.66"


def test_portage_version_reads_fake_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(VERSION="3.0.66")
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake)
    assert portage_version() == "3.0.66"


def test_configured_repos_reads_fake_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the fake mirrors the shape portage.settings.repositories.prepos (iterable of names)
    repositories = SimpleNamespace(prepos=["gentoo", "bentoo", "guru"])
    fake = SimpleNamespace(settings=SimpleNamespace(repositories=repositories))
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake)
    assert configured_repos() == ("bentoo", "gentoo", "guru")


def test_module_object_satisfies_require_portage_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a real ModuleType is also accepted by the gate
    fake_mod = ModuleType("fake_portage")
    monkeypatch.setattr(portage_api, "PORTAGE_AVAILABLE", True)
    monkeypatch.setattr(portage_api, "_portage", fake_mod)
    assert require_portage() is fake_mod


# --- T7.2: the recipe/CLI path never triggers an import of portage_api (R7.3) -
#
# INTEGRATION: exercises the Portage-free path end to end (importing
# ``shidashi.recipe`` and ``shidashi.cli``, running ``recipe show``/``validate`` via Typer
# CliRunner over a variants/ tree in tmp_path) and proves that NOTHING on that
# path imports ``shidashi.portage_api``. Since that module is already loaded by the
# top of this test file, we remove it from ``sys.modules`` BEFORE exercising
# the path and assert that it does NOT reappear afterwards — that is, the
# recipe/CLI path does not trigger ``import shidashi.portage_api`` (Portage is
# absent: the gate is never hit).

_runner = CliRunner()


@pytest.fixture
def variants_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared stage-format tree (tests/_variants_tree.py), pointed at by the env."""
    root = write_variants(tmp_path / "variants")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


def test_recipe_cli_path_never_imports_portage_api(variants_tree: Path) -> None:
    # start from a state where portage_api is NOT loaded: we remove the
    # module (and the parent package, so that a re-import of shidashi does not pull it in).
    # Re-importing shidashi.recipe creates NEW pydantic classes; if we do not restore
    # sys.modules at the end, later tests (e.g. test_resolve) that import
    # those classes at different moments see diverging copies (model_type).
    # That is why we save and restore the affected modules in a try/finally.
    _names = ("shidashi.portage_api", "shidashi.recipe", "shidashi.cli", "shidashi")
    _saved = {name: sys.modules.get(name) for name in _names}
    try:
        for name in _names:
            sys.modules.pop(name, None)
        assert "shidashi.portage_api" not in sys.modules

        # importing the recipe layer and the CLI must NOT trigger portage_api
        import shidashi.cli as cli
        import shidashi.recipe as recipe

        assert "shidashi.portage_api" not in sys.modules

        # exercise the merge directly through the recipe layer (Portage absent)
        resolved = recipe.merge(
            recipe.load_base(_BASE_PATH(variants_tree)),
            recipe.load_arch(_RECIPE_PATH(variants_tree, "arch", "v3")),
            recipe.load_chain("minimal", lambda name: variants_tree / name / "recipe.yaml"),
            recipe.load_init(_RECIPE_PATH(variants_tree, "init", "systemd")),
        )
        assert resolved.arch == "v3"
        assert "shidashi.portage_api" not in sys.modules

        # exercise the CLI path: recipe show / validate exit with 0 without Portage
        show = _runner.invoke(cli.app, ["recipe", "show", "v3", "minimal", "systemd"])
        assert show.exit_code == 0, show.stdout
        validate = _runner.invoke(cli.app, ["recipe", "validate", "v3", "minimal", "systemd"])
        assert validate.exit_code == 0, validate.stdout

        # central proof of R7.3: no step of the recipe/CLI path imported
        # portage_api (the module stays out of sys.modules)
        assert "shidashi.portage_api" not in sys.modules
    finally:
        # restore the original modules so the rest of the suite is not polluted
        for name, module in _saved.items():
            if module is not None:
                sys.modules[name] = module
            else:
                sys.modules.pop(name, None)


def _BASE_PATH(root: Path) -> Path:
    return root / "base" / "recipe.yaml"


def _RECIPE_PATH(root: Path, axis: str, name: str) -> Path:
    return root / axis / name / "recipe.yaml"
