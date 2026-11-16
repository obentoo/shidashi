"""Re-pairing a worker keeps the arch caches it owns (``cache_archs``).

``kyomei.complete`` recorded a fresh entry for the name, so re-pairing a worker that
owned an arch's cache (``worker cache-owner``) silently gave it back to the host: the
host went back to building that arch itself. The same defect ``provision --replace``
had (fixed in 0e0797d); found while fixing that one (2026-11-15).
"""

from pathlib import Path

from shidashi import workers
from tests.test_kyomei import (  # the fixtures and fakes of the kyomei suite
    WORKER_HOST_KEY,
    _complete,
    _ed25519_line,
    _fingerprint,
    _paired,
    _Proof,
    proof,
    wdir,
)

__all__ = ["proof", "wdir"]  # pytest finds the fixtures through this module


def _owning(wdir: Path, host_key: str, *archs: str) -> workers.WorkerEntry:
    entry = workers.WorkerEntry(
        name="bentoo-lab",
        address="192.168.15.3",
        host_key=host_key,
        host_key_fingerprint=_fingerprint(host_key),
        paired_at="2026-10-01T09:00:00+00:00",
        cpu_flags=("avx2",),
        image="20261001T0900",
        provisioned=True,
        cache_archs=archs,
    )
    workers.save_registry(wdir / "workers.json", {"bentoo-lab": entry})
    (wdir / "known_hosts").write_text(f"bentoo-lab {host_key}\n")
    return entry


def test_re_pairing_the_same_worker_keeps_the_archs_it_owns(wdir: Path, proof: _Proof) -> None:
    _owning(wdir, WORKER_HOST_KEY, "v3")

    entry = _complete(_paired(), wdir)

    assert entry.cache_archs == ("v3",)
    assert workers.load_registry(wdir / "workers.json")["bentoo-lab"].cache_archs == ("v3",)


def test_re_pairing_under_a_new_key_keeps_them_too(wdir: Path, proof: _Proof) -> None:
    """A reinstalled worker brings a new key; its work disk, and the caches on it, stay.
    The command warns that another machine would have to give them back."""
    _owning(wdir, _ed25519_line("bentoo-lab-before-reinstall"), "v3", "v4")

    entry = _complete(_paired(), wdir)

    assert entry.cache_archs == ("v3", "v4")


def test_a_re_pairing_takes_the_new_facts_and_drops_provisioned(wdir: Path, proof: _Proof) -> None:
    """Nothing else is carried: the welcome brings the address, CPU flags and image a
    provisioned entry waits for, so the entry is no longer waiting for them."""
    _owning(wdir, WORKER_HOST_KEY, "v3")

    entry = _complete(_paired(), wdir)

    assert (entry.address, entry.image, entry.provisioned) == (
        "192.168.15.7",
        "20261006T1200",
        False,
    )
    assert entry.cpu_flags == ("avx2", "bmi2", "sse4_2")


def test_a_first_pairing_owns_no_arch(wdir: Path, proof: _Proof) -> None:
    neighbour_key = _ed25519_line("lab2")
    neighbour = workers.WorkerEntry(
        name="lab2",
        address="192.168.15.9",
        host_key=neighbour_key,
        host_key_fingerprint=_fingerprint(neighbour_key),
        paired_at="2026-10-01T09:00:00+00:00",
        cache_archs=("v3",),
    )
    workers.save_registry(wdir / "workers.json", {"lab2": neighbour})

    entry = _complete(_paired(), wdir)

    assert entry.cache_archs == ()
    assert workers.load_registry(wdir / "workers.json")["lab2"] == neighbour


# --- the command: a key that changed may be another machine ---------------------------

from tests.test_cli_kyomei import _Harness, _out  # noqa: E402
from tests.test_kyomei_v4 import (  # noqa: E402 -- the CLI half of the kyomei suite
    NAME,
    OLD_KEY,
    _kyomei,
    _registered,
    _registry,
    env,
    existing_key,
    harness,
)

__all__ += ["env", "existing_key", "harness"]


def _owns(wdir: Path, *archs: str) -> None:
    registry = workers.load_registry(wdir / "workers.json")
    entry = registry[NAME].model_copy(update={"cache_archs": archs})
    workers.save_registry(wdir / "workers.json", {**registry, NAME: entry})


def test_a_replace_under_another_key_says_how_to_give_the_archs_back(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    """Another key may be another machine, whose work disk holds none of those caches:
    the archs are kept, and the command says how to hand them back to the host."""
    _registered(env["wdir"], (NAME, "192.168.15.9", OLD_KEY))
    _owns(env["wdir"], "v3")
    result = _kyomei("--replace", "--address", "192.168.15.6", input="k7m4-q2xp\n")
    out = _out(result)
    assert result.exit_code == 0, out
    assert _registry()[NAME].cache_archs == ("v3",)
    assert "still owns the cache of v3" in out
    assert f"shidashi worker cache-owner {NAME} --arch ARCH --off" in out


def test_a_replace_of_a_worker_that_owns_nothing_warns_about_no_arch(
    harness: _Harness, existing_key: Path, env: dict[str, Path]
) -> None:
    _registered(env["wdir"], (NAME, "192.168.15.9", OLD_KEY))
    result = _kyomei("--replace", "--address", "192.168.15.6", input="k7m4-q2xp\n")
    assert result.exit_code == 0, _out(result)
    assert "still owns the cache" not in _out(result)
