"""``shidashi worker ... N`` reaches N wherever DHCP placed it (story 020, task 3.2).

When N's recorded address does not answer, or N has none yet (a provisioned worker
that never answered), the command looks N up over mDNS ONCE, retries ONCE at the
address found and records it in ``workers.json``; nobody answering is exit 1 naming
N, the 5 s timeout and ``--address``. A changed host key is never retried, and the
worker found over mDNS is still verified against N's pin (``HostKeyAlias=N``). The
first status of a provisioned worker completes its entry: address, CPU flags, image.

Against the fake worker of tests/_fake_worker.py: the real ``remote.run`` and
``worker.status`` run, through an ``ssh`` shim that records every connection and
fails the ones aimed at an address told to fail. mDNS is a table of names to
addresses, patched in as ``mdns.find`` and, under it, as ``mdns.browse`` -- so the
lookup is counted however the command reaches it, and no packet leaves the host.

Provisioned entries are built with ``model_validate``: the file type-checks before
and after task 1.2.

Requirements exercised: R3.2, R3.3, R3.4, R3.5.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli as cli_mod
from shidashi import config, mdns, remote, workers
from shidashi.cli import app
from tests._fake_worker import BUILD_ID, FakeWorker

runner = CliRunner()

STALE = "192.0.2.99"  # where N was last seen; nothing answers there any more
ELSEWHERE = "192.0.2.77"  # an address that does not answer either
IMPOSTOR = "192.0.2.66"  # answers for N over mDNS, but with another host key
_MISMATCH_STDERR = (
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
    "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
    "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
    "The fingerprint for the ED25519 key sent by the remote host is\n"
    "SHA256:ImpostorImpostorImpostorImpostorImpostor00.\n"
    "Host key verification failed.\n"
)


class _Lan:
    """mDNS as a table: ``answers`` maps a worker name to the address it answers from.

    Patched in as ``mdns.find`` and, beneath it, as ``mdns.browse`` (answering the
    worker service only): a lookup counts once whichever of the two the command uses.
    """

    def __init__(self) -> None:
        self.answers: dict[str, str] = {}
        self.names: list[str] = []  # names asked through find
        self.timeouts: list[object] = []  # the wait of every lookup

    @property
    def lookups(self) -> int:
        return len(self.timeouts)

    def find(self, name: str, **kw: Any) -> str | None:
        self.names.append(name)
        self.timeouts.append(kw.get("timeout"))
        return self.answers.get(name)

    def browse(self, *args: Any, **kw: Any) -> list[mdns.Found]:
        service = str(args[0] if args else kw.get("service", ""))
        self.timeouts.append(args[1] if len(args) > 1 else kw.get("wait", kw.get("timeout")))
        if "shidashi-worker" not in service:
            return []
        return [mdns.Found(n, a, 22, (("v", "1"),)) for n, a in sorted(self.answers.items())]


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "200")
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    yield w
    w.close()


@pytest.fixture
def lan(monkeypatch: pytest.MonkeyPatch) -> _Lan:
    fake = _Lan()
    monkeypatch.setattr(mdns, "browse", fake.browse)
    for module in (mdns, cli_mod, remote):
        monkeypatch.setattr(module, "find", fake.find, raising=False)
    return fake


def _invoke(args: list[str], capfd: pytest.CaptureFixture[str]) -> tuple[Result, str]:
    result = runner.invoke(app, ["worker", *args])
    captured = capfd.readouterr()
    return result, result.output + captured.out + captured.err


def _registry() -> dict[str, workers.WorkerEntry]:
    return workers.load_registry(config.workers_dir() / "workers.json")


def _dumps() -> dict[str, dict[str, Any]]:
    return {name: e.model_dump(mode="json") for name, e in _registry().items()}


def _known_hosts() -> bytes:
    return (config.workers_dir() / "known_hosts").read_bytes()


def _provisioned(fw: FakeWorker) -> workers.WorkerEntry:
    """N as ``provision`` records it: pinned, never seen -- no address, flags or image."""
    raw = fw.entry().model_dump(mode="json")
    for field in ("address", "cpu_flags", "image"):
        raw.pop(field)
    raw["provisioned"] = True
    return workers.WorkerEntry.model_validate(raw)


def _ssh_hosts(fw: FakeWorker) -> list[str]:
    """Where each ssh connection went, in order."""
    return [str(c["target"]) for c in fw.calls("ssh")]


def _not_answering(fw: FakeWorker, address: str) -> None:
    fw.fail(
        host=address,
        code=255,
        stderr=f"ssh: connect to host {address} port 22: Connection timed out\r\n",
    )


def _no_traceback(result: Result, out: str) -> None:
    assert "Traceback" not in out
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


# --- hostile first: what must never be retried, trusted or rewritten -----------------


def test_status_a_changed_host_key_at_the_recorded_address_is_refused_without_a_lookup(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.4: a key mismatch is a security event, not an outage -- no mDNS, no retry,
    and the provisioned entry is not completed from a worker that failed its pin."""
    raw = _provisioned(fw).model_dump(mode="json")
    fw.register(workers.WorkerEntry.model_validate({**raw, "address": fw.address}))
    fw.host_key_changed()
    lan.answers[fw.name] = ELSEWHERE
    before, pins = _dumps(), _known_hosts()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    assert "does not match the pin" in out
    assert lan.lookups == 0
    assert _ssh_hosts(fw) == [fw.address]
    assert _dumps() == before  # no cpu_flags, no image, no address change
    assert _known_hosts() == pins
    _no_traceback(result, out)


def test_status_an_mdns_answer_presenting_another_key_is_refused_and_never_recorded(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.4, hostile: mDNS is not trusted -- whoever answers for N at another address
    is verified against N's pin, refused on a mismatch, not retried again, and its
    address does not replace N's (the next command would only meet the impostor)."""
    fw.register(fw.entry(address=STALE))
    _not_answering(fw, STALE)
    fw.fail(host=IMPOSTOR, code=255, stderr=_MISMATCH_STDERR)
    lan.answers[fw.name] = IMPOSTOR
    pins = _known_hosts()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    assert "does not match the pin" in out
    assert _ssh_hosts(fw) == [STALE, IMPOSTOR]
    assert lan.lookups == 1
    assert _registry()[fw.name].address != IMPOSTOR
    assert _known_hosts() == pins  # a found address never re-pins
    fw.assert_pinned()
    _no_traceback(result, out)


def test_status_records_the_found_address_for_n_alone(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """Hostile (third element): another registered worker, answering over mDNS too,
    keeps its entry; N keeps its pin fields; only N's address changes."""
    spare = fw.entry(name="spare", address="192.0.2.50")
    fw.register(fw.entry(address=STALE), spare)
    _not_answering(fw, STALE)
    lan.answers.update({fw.name: fw.address, "spare": "192.0.2.51"})
    before, pins = _dumps(), _known_hosts()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 0, out
    after = _dumps()
    assert after["spare"] == before["spare"]
    assert after[fw.name] == {**before[fw.name], "address": fw.address}
    assert _known_hosts() == pins


def test_status_retries_once_only(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """The address found does not answer either: one lookup, one retry, exit 1."""
    fw.register(fw.entry(address=STALE))
    _not_answering(fw, STALE)
    _not_answering(fw, ELSEWHERE)
    lan.answers[fw.name] = ELSEWHERE
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    assert _ssh_hosts(fw) == [STALE, ELSEWHERE]
    assert lan.lookups == 1
    _no_traceback(result, out)


# --- R3.3: nobody answers -------------------------------------------------------------


def test_status_nobody_answering_over_mdns_exits_1_naming_n_the_timeout_and_address(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(fw.entry(address=STALE))
    _not_answering(fw, STALE)
    lan.answers["bentoo-lab2"] = ELSEWHERE  # a neighbour answers; N does not
    before = _dumps()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    assert fw.name in out
    assert "5 s" in out
    assert "--address" in out
    assert lan.lookups == 1
    assert lan.timeouts == [pytest.approx(5.0)]
    assert _ssh_hosts(fw) == [STALE]
    assert _dumps() == before
    _no_traceback(result, out)


def test_status_of_a_provisioned_worker_nobody_answers_for_exits_1_without_ssh(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(_provisioned(fw))
    before = _dumps()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 1, out
    assert fw.name in out and "5 s" in out and "--address" in out
    assert lan.lookups == 1
    assert _ssh_hosts(fw) == []
    assert _dumps() == before
    _no_traceback(result, out)


# --- benign: found, reached with the pin, recorded --------------------------------------


def test_status_an_unanswering_address_is_looked_up_once_retried_once_and_recorded(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    fw.register(fw.entry(address=STALE))
    _not_answering(fw, STALE)
    lan.answers[fw.name] = fw.address
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 0, out
    assert "reachable" in out and fw.address in out
    assert _ssh_hosts(fw) == [STALE, fw.address]
    assert lan.lookups == 1
    assert lan.names in ([], [fw.name])  # asked for N, when asked by name
    assert _registry()[fw.name].address == fw.address
    fw.assert_pinned()  # HostKeyAlias=N on both attempts, strict checking kept
    retry = fw.calls("ssh")[-1]
    assert "StrictHostKeyChecking=yes" in retry["options"]


def test_status_of_a_provisioned_worker_finds_it_and_completes_its_entry(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.2, R3.5: no address yet -- looked up, reached, and the address, CPU flags
    and image of the first status recorded; the pin fields stay as provisioned."""
    entry = _provisioned(fw)
    fw.register(entry)
    lan.answers[fw.name] = fw.address
    pins = _known_hosts()
    result, out = _invoke(["status", fw.name], capfd)
    assert result.exit_code == 0, out
    assert _ssh_hosts(fw) == [fw.address]
    assert lan.lookups == 1
    got = _registry()[fw.name]
    assert got.address == fw.address
    assert "avx2" in got.cpu_flags
    assert got.image == BUILD_ID
    assert (got.host_key, got.host_key_fingerprint, got.paired_at) == (
        entry.host_key,
        entry.host_key_fingerprint,
        entry.paired_at,
    )
    assert _known_hosts() == pins
    fw.assert_pinned()


def test_status_address_option_reaches_it_without_a_lookup_and_records_it(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.3's way by hand: ``--address`` is used as given, never second-guessed by mDNS."""
    fw.register(_provisioned(fw))
    lan.answers[fw.name] = ELSEWHERE
    result, out = _invoke(["status", fw.name, "--address", fw.address], capfd)
    assert result.exit_code == 0, out
    assert lan.lookups == 0
    assert _ssh_hosts(fw) == [fw.address]
    got = _registry()[fw.name]
    assert got.address == fw.address
    assert "avx2" in got.cpu_flags and got.image == BUILD_ID
    fw.assert_pinned()


def test_run_on_a_provisioned_worker_finds_it_first_and_records_it(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.2 holds for every worker command, not only status."""
    fw.register(_provisioned(fw))
    lan.answers[fw.name] = fw.address
    result, out = _invoke(["run", fw.name, "--", "printf", "%s\\n", "hello"], capfd)
    assert result.exit_code == 0, out
    assert "hello" in out.splitlines()
    assert lan.lookups == 1
    assert _ssh_hosts(fw) == [fw.address]
    assert _registry()[fw.name].address == fw.address
    fw.assert_pinned()
