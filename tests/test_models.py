"""Testes dos modelos de receita e loaders YAML (kaji.recipe)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from kaji.recipe import (
    ArchFragment,
    BaseFragment,
    FlavorFragment,
    InitFragment,
    Phase,
    RecipeConflictError,
    ResolvedRecipe,
    ResolvedUse,
    UsePrefer,
    load_arch,
    load_base,
    load_flavor,
    load_init,
)

# --- dicts válidos representativos por modelo ---------------------------------

VALID: dict[type, dict[str, Any]] = {
    UsePrefer: {"add": ["qt6"], "drop": ["-gtk"]},
    Phase: {"name": "system", "packages": ["sys-apps/foo"], "use_break": ["-doc"]},
    BaseFragment: {
        "profile_base": "default/linux/amd64/23.0",
        "sets": ["@system"],
        "phases": [{"name": "system", "packages": ["sys-apps/foo"]}],
    },
    ArchFragment: {
        "arch": "amd64",
        "common_flags": "-O2 -pipe",
        "goamd64": "v2",
        "rustflags": "-C target-cpu=x86-64-v2",
        "cpu_flags_x86": ["sse2", "avx"],
        "runnable_on_build_host": True,
        "tier": 1,
    },
    FlavorFragment: {
        "flavor": "desktop",
        "use_prefer": {"add": ["qt6"], "drop": ["-gtk"]},
        "sets": ["@desktop"],
        "override_ok": True,
    },
    InitFragment: {
        "init": "openrc",
        "profile_suffix": "openrc",
        "use_prefer": {"add": ["-systemd"]},
        "phases_prepend": [{"name": "early"}],
    },
    ResolvedUse: {"enabled": ["qt6"], "disabled": ["gtk"]},
    ResolvedRecipe: {
        "arch": "amd64",
        "flavor": "desktop",
        "init": "openrc",
        "profile": "default/linux/amd64/23.0/openrc",
        "common_flags": "-O2 -pipe",
        "goamd64": "v2",
        "rustflags": "-C target-cpu=x86-64-v2",
        "cpu_flags_x86": ["sse2"],
        "tier": 1,
        "runnable_on_build_host": True,
        "use": {"enabled": ["qt6"], "disabled": ["gtk"]},
        "sets": ["@system", "@desktop"],
        "phases": [{"name": "system"}],
        "portage_layers": ["base", "arch/amd64"],
    },
}

ALL_MODELS = list(VALID.keys())


# --- (a) cada modelo constrói a partir de um dict válido ----------------------


@pytest.mark.parametrize("model", ALL_MODELS)
def test_constructs_from_valid_dict(model: type) -> None:
    instance = model(**VALID[model])
    assert isinstance(instance, model)


def test_tuple_coercion_from_list() -> None:
    # listas YAML/dict coagem automaticamente para tuplas
    frag = ArchFragment(**VALID[ArchFragment])
    assert frag.cpu_flags_x86 == ("sse2", "avx")
    assert isinstance(frag.cpu_flags_x86, tuple)


def test_nested_models_typed() -> None:
    base = BaseFragment(**VALID[BaseFragment])
    assert isinstance(base.phases[0], Phase)
    flavor = FlavorFragment(**VALID[FlavorFragment])
    assert isinstance(flavor.use_prefer, UsePrefer)


def test_defaults_applied() -> None:
    frag = ArchFragment(
        arch="arm64",
        common_flags="-O2",
        goamd64="",
        rustflags="",
        cpu_flags_x86=(),
    )
    assert frag.runnable_on_build_host is False
    assert frag.tier == 2
    flavor = FlavorFragment(flavor="minimal")
    assert flavor.use_prefer == UsePrefer()
    assert flavor.override_ok is False


# --- (b) chave desconhecida levanta ValidationError ---------------------------


@pytest.mark.parametrize("model", ALL_MODELS)
def test_unknown_key_rejected(model: type) -> None:
    payload = {**VALID[model], "bogus_key": 123}
    with pytest.raises(ValidationError):
        model(**payload)


# --- (c) instâncias são imutáveis (frozen) ------------------------------------


@pytest.mark.parametrize("model", ALL_MODELS)
def test_instances_are_frozen(model: type) -> None:
    instance = model(**VALID[model])
    field = next(iter(type(instance).model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))


# --- (d) cada loader parseia um YAML representativo em tmp_path ---------------


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_base(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path / "base.yaml", VALID[BaseFragment])
    frag = load_base(p)
    assert frag == BaseFragment(**VALID[BaseFragment])
    assert frag.phases[0].name == "system"


def test_load_arch(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path / "arch.yaml", VALID[ArchFragment])
    frag = load_arch(p)
    assert frag == ArchFragment(**VALID[ArchFragment])
    assert frag.cpu_flags_x86 == ("sse2", "avx")


def test_load_flavor(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path / "flavor.yaml", VALID[FlavorFragment])
    frag = load_flavor(p)
    assert frag == FlavorFragment(**VALID[FlavorFragment])
    assert frag.use_prefer.add == ("qt6",)


def test_load_init(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path / "init.yaml", VALID[InitFragment])
    frag = load_init(p)
    assert frag == InitFragment(**VALID[InitFragment])
    assert frag.phases_prepend[0].name == "early"


# --- (e) YAML malformado / chave desconhecida é rejeitado ---------------------


def test_loader_rejects_unknown_key(tmp_path: Path) -> None:
    bad = {**VALID[BaseFragment], "unexpected": True}
    p = _write_yaml(tmp_path / "bad.yaml", bad)
    with pytest.raises(ValidationError):
        load_base(p)


def test_loader_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(TypeError):
        load_arch(p)


def test_loader_rejects_missing_required_field(tmp_path: Path) -> None:
    # ArchFragment exige cpu_flags_x86; ausência -> ValidationError
    incomplete = {k: v for k, v in VALID[ArchFragment].items() if k != "cpu_flags_x86"}
    p = _write_yaml(tmp_path / "incomplete.yaml", incomplete)
    with pytest.raises(ValidationError):
        load_arch(p)


# --- RecipeConflictError ------------------------------------------------------


def test_recipe_conflict_error_carries_fields() -> None:
    err = RecipeConflictError("qt6", "flavor/desktop", "init/openrc")
    assert isinstance(err, Exception)
    assert err.flag == "qt6"
    assert err.layer_a == "flavor/desktop"
    assert err.layer_b == "init/openrc"
    msg = str(err)
    assert "qt6" in msg
    assert "flavor/desktop" in msg
    assert "init/openrc" in msg
