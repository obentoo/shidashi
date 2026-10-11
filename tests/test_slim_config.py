"""Tests of the ``slim:`` section of ``system.yaml`` (story 021, task 1.1).

``SystemConfig.slim`` declares what a slimmed image keeps: the keep list that becomes
its whole world, the commands that must survive, the kernel-module subtrees and the
firmware it drops or keeps, the paths it prunes and its squashfs budget. Only an image
whose layers declare it gets one; every other image loads ``slim`` as ``None``. The
section merges across layers like every other key: lists add up (each entry once),
scalars are replaced, the mapping itself is merged rather than replaced.

Every refusal is paired with the neighbouring value that must still be accepted, so a
validator that refuses too much is caught as surely as one that refuses too little.

Requirements exercised: R1.1, R1.5, R1.6, R2.1, R2.3, R3.2.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from shidashi import config, system
from shidashi.recipe import ResolvedRecipe

#: A complete, valid slim section: every field, with the entries a validator could
#: wrongly refuse (a leading ``**``, a ``..`` inside a segment name, a slotted atom).
_VALID: dict[str, Any] = {
    "keep": ["sys-apps/systemd", "dev-lang/python:3.14", "net-misc/openssh"],
    "commands": ["/usr/sbin/sshd", "/usr/bin/python3"],
    "drop_modules": ["drivers/gpu", "drivers/net/ethernet/mellanox", "sound"],
    "keep_firmware": ["intel-ucode/*", "amd-ucode/*", "regulatory.db*"],
    "prune": ["usr/share/doc/**", "usr/share/doc/pkg-1..2/**", "**/*.a"],
    "budget_mb": 600,
}


def _recipe(layers: tuple[str, ...]) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="v3",
        flavor="worker",
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
        portage_layers=layers,
    )


def _variants(tmp_path: Path, layers: dict[str, dict[str, Any]]) -> Path:
    """A variants tree: livecd.yaml, a base with the required scalars, and ``layers``
    (layer name -> the system.yaml mapping it holds)."""
    root = tmp_path / "variants"
    root.mkdir(parents=True)
    (root / "livecd.yaml").write_text("user: bentoo\npassword: bentoo\n")
    files = {
        "base": {"hostname": "bentoo", "locale": "en_US.UTF-8", "keymap": "us", "timezone": "UTC"},
        **layers,
    }
    for layer, data in files.items():
        (root / layer).mkdir(parents=True, exist_ok=True)
        (root / layer / "system.yaml").write_text(yaml.safe_dump(data))
    return root


def _load(tmp_path: Path, layers: dict[str, dict[str, Any]]) -> system.SystemConfig:
    variants = _variants(tmp_path, layers)
    recipe = _recipe(("base", "init/systemd", *layers))
    return system.load_system_config(recipe, variants_dir=variants)


def _load_slim(tmp_path: Path, slim: dict[str, Any]) -> system.SystemConfig:
    return _load(tmp_path, {"worker": {"slim": slim}})


# --- what a slim section is ---------------------------------------------------------


def test_a_declared_slim_section_loads_every_field(tmp_path: Path) -> None:
    cfg = _load_slim(tmp_path, _VALID)
    slim = cfg.slim
    assert slim is not None
    assert tuple(slim.keep) == tuple(_VALID["keep"])
    assert tuple(slim.commands) == tuple(_VALID["commands"])
    assert tuple(slim.drop_modules) == tuple(_VALID["drop_modules"])
    assert tuple(slim.keep_firmware) == tuple(_VALID["keep_firmware"])
    assert tuple(slim.prune) == tuple(_VALID["prune"])
    assert slim.budget_mb == 600


def test_the_budget_is_optional(tmp_path: Path) -> None:
    """A slim section without ``budget_mb`` has no budget -- not a zero one."""
    slim = {k: v for k, v in _VALID.items() if k != "budget_mb"}
    cfg = _load_slim(tmp_path, slim)
    assert cfg.slim is not None
    assert cfg.slim.budget_mb is None


def test_layers_without_a_slim_section_load_none(tmp_path: Path) -> None:
    """Hostile half of R1.6: an image whose layers never declare ``slim`` must not get
    an empty section (which would still run a slim step) -- it gets none at all."""
    cfg = _load(tmp_path, {"worker": {"hostname": "shidashi-worker"}})
    assert cfg.slim is None


@pytest.mark.parametrize("target", ["minimal", "kde"])
def test_the_shipped_images_other_than_the_worker_declare_no_slim(target: str) -> None:
    """R1.6: minimal and kde load with ``slim`` unset -- they are assembled as before."""
    recipe = config.load_recipe("v3", target, "systemd")
    cfg = system.load_system_config(recipe, variants_dir=config.variants_dir())
    assert cfg.slim is None


# --- merged across layers -----------------------------------------------------------


def test_slim_merges_across_layers_lists_add_up_and_scalars_replace(tmp_path: Path) -> None:
    """minimal's section and the worker's on top of it: the keep lists add up, each
    atom once; a key only the lower layer sets survives (the mapping is merged, not
    replaced); the budget is the upper layer's."""
    cfg = _load(
        tmp_path,
        {
            "minimal": {
                "slim": {
                    "keep": ["sys-apps/systemd", "dev-lang/python"],
                    "prune": ["usr/share/doc/**"],
                    "budget_mb": 900,
                }
            },
            "worker": {
                "slim": {
                    "keep": ["sys-apps/systemd", "dev-lang/python:3.14", "net-misc/openssh"],
                    "commands": ["/usr/sbin/sshd"],
                    "budget_mb": 600,
                }
            },
        },
    )
    slim = cfg.slim
    assert slim is not None
    # the same atom twice collapses to one ...
    assert list(slim.keep).count("sys-apps/systemd") == 1
    # ... but an atom and its slotted form are two entries, both kept (hostile collapse)
    assert tuple(slim.keep) == (
        "sys-apps/systemd",
        "dev-lang/python",
        "dev-lang/python:3.14",
        "net-misc/openssh",
    )
    assert tuple(slim.prune) == ("usr/share/doc/**",)  # only the lower layer set it
    assert tuple(slim.commands) == ("/usr/sbin/sshd",)  # only the upper layer set it
    assert tuple(slim.drop_modules) == () and tuple(slim.keep_firmware) == ()
    assert slim.budget_mb == 600


def test_a_layer_without_slim_keeps_the_section_a_lower_layer_declared(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            "minimal": {"slim": {"keep": ["sys-apps/systemd"], "budget_mb": 700}},
            "worker": {"hostname": "shidashi-worker"},
        },
    )
    assert cfg.slim is not None
    assert tuple(cfg.slim.keep) == ("sys-apps/systemd",)
    assert cfg.slim.budget_mb == 700


# --- refusals: each with the neighbour that stays valid ------------------------------


@pytest.mark.parametrize(
    ("field", "bad", "needle"),
    [
        # a command must be an absolute path inside the image
        ("commands", "usr/sbin/sshd", "usr/sbin/sshd"),
        # module subtrees and prune globs are relative to their root, never above it
        ("drop_modules", "/drivers/gpu", "/drivers/gpu"),
        ("drop_modules", "drivers/../../../etc", "drivers/../../../etc"),
        ("prune", "/usr/share/doc/**", "/usr/share/doc/**"),
        ("prune", "usr/share/../../../etc/**", "usr/share/../../../etc/**"),
        ("prune", "../outside/**", "../outside/**"),
        ("keep_firmware", "/lib/firmware/intel-ucode/*", "/lib/firmware/intel-ucode/*"),
        ("keep_firmware", "../firmware/*", "../firmware/*"),
        # an atom is one token
        ("keep", "sys-apps/systemd net-misc/openssh", "sys-apps/systemd net-misc/openssh"),
    ],
)
def test_an_invalid_entry_is_refused_naming_it(
    tmp_path: Path, field: str, bad: str, needle: str
) -> None:
    good = _load_slim(tmp_path / "good", _VALID)  # the valid neighbour loads
    assert good.slim is not None
    slim = {**_VALID, field: [*_VALID[field], bad]}
    with pytest.raises(system.ConfigurationError) as caught:
        _load_slim(tmp_path / "bad", slim)
    assert needle in str(caught.value)


def test_an_empty_atom_is_refused(tmp_path: Path) -> None:
    assert _load_slim(tmp_path / "good", _VALID).slim is not None
    with pytest.raises(system.ConfigurationError, match="keep"):
        _load_slim(tmp_path / "bad", {**_VALID, "keep": ["sys-apps/systemd", ""]})


@pytest.mark.parametrize("budget", [0, -600])
def test_a_budget_that_is_not_positive_is_refused(tmp_path: Path, budget: int) -> None:
    assert _load_slim(tmp_path / "good", {**_VALID, "budget_mb": 1}).slim is not None
    with pytest.raises(system.ConfigurationError, match="budget_mb"):
        _load_slim(tmp_path / "bad", {**_VALID, "budget_mb": budget})


def test_an_unknown_key_in_the_slim_section_is_refused(tmp_path: Path) -> None:
    """Strict like every config model: a typo must not silently keep nothing."""
    assert _load_slim(tmp_path / "good", _VALID).slim is not None
    with pytest.raises(system.ConfigurationError, match="keep_firmwares"):
        _load_slim(tmp_path / "bad", {**_VALID, "keep_firmwares": ["intel-ucode/*"]})
