"""Regressions from the tech review of story 020, task 3.2.

``--address`` takes ASCII digits only for its port (a Unicode digit crashed ``int()``
or was saved as is), and an address proven against N's pin is never written onto a
pin that changed while the command ran.
"""

from pathlib import Path

import pytest
import typer

from shidashi import cli, config, remote, workers


@pytest.mark.parametrize("address", ["host:²", "host:١٢", "10.0.0.5:２２"])
def test_a_port_of_non_ascii_digits_is_a_usage_error(address: str) -> None:
    with pytest.raises(typer.BadParameter):
        cli._ssh_address(address)


@pytest.mark.parametrize("address", ["host:²", "host:١٢"])
def test_split_address_reads_only_ascii_digits_as_a_port(address: str) -> None:
    assert remote.split_address(address) == (address, remote.SSH_PORT)


def _entry(fingerprint: str) -> workers.WorkerEntry:
    return workers.WorkerEntry(
        name="lab",
        address="192.0.2.1",
        host_key=f"ssh-ed25519 {fingerprint}",
        host_key_fingerprint=fingerprint,
        paired_at="2026-10-10T12:00:00+00:00",
    )


def test_an_address_is_not_recorded_onto_a_pin_that_changed_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    repinned = _entry("SHA256:new")
    workers.save_registry(path, {"lab": repinned})
    returned = cli._record_address(_entry("SHA256:old"), "192.0.2.77")
    assert workers.load_registry(path)["lab"] == repinned
    assert returned.address == "192.0.2.1"


def test_an_address_is_recorded_when_the_pin_is_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    workers.save_registry(path, {"lab": _entry("SHA256:same")})
    cli._record_address(_entry("SHA256:same"), "192.0.2.77")
    assert workers.load_registry(path)["lab"].address == "192.0.2.77"
