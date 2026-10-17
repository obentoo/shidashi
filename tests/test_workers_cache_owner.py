"""Story 028: which worker owns an arch's cache (``WorkerEntry.cache_archs``)."""

import json
from pathlib import Path

import pytest

from shidashi import workers
from shidashi.workers import RegistryError, WorkerEntry


def _entry(name: str, **kw: object) -> WorkerEntry:
    return WorkerEntry(
        name=name,
        host_key="ssh-ed25519 AAAA",
        host_key_fingerprint="SHA256:x",
        paired_at="2026-10-17T00:00:00Z",
        **kw,  # type: ignore[arg-type]
    )


def test_a_registry_written_before_cache_archs_loads_with_no_owner(tmp_path: Path) -> None:
    path = tmp_path / "workers.json"
    old = _entry("bentoo-lab").model_dump(mode="json")
    del old["cache_archs"]
    path.write_text(json.dumps({"bentoo-lab": old}))
    entries = workers.load_registry(path)
    assert entries["bentoo-lab"].cache_archs == ()
    assert workers.cache_owner(entries, "v3") is None


def test_the_owner_is_found_by_arch() -> None:
    entries = {
        "bentoo-lab": _entry("bentoo-lab", cache_archs=("v3",)),
        "other": _entry("other"),
    }
    assert workers.cache_owner(entries, "v3") == "bentoo-lab"


def test_an_arch_nobody_owns_has_no_owner() -> None:
    entries = {"bentoo-lab": _entry("bentoo-lab", cache_archs=("v3",))}
    assert workers.cache_owner(entries, "znver5") is None


def test_two_claimants_of_one_arch_are_refused_naming_both() -> None:
    entries = {
        "a": _entry("a", cache_archs=("v3",)),
        "b": _entry("b", cache_archs=("v3",)),
    }
    with pytest.raises(RegistryError, match=r"v3.*\ba\b.*\bb\b"):
        workers.cache_owner(entries, "v3")


def test_cache_archs_survive_a_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "workers.json"
    workers.save_registry(path, {"bentoo-lab": _entry("bentoo-lab", cache_archs=("v3",))})
    assert workers.load_registry(path)["bentoo-lab"].cache_archs == ("v3",)
