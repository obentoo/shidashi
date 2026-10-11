"""``worker job N <arch>`` completes a provisioned worker's entry first (story 020, task 5.1).

A provisioned entry is recorded before the worker ever booted: no address, no CPU
flags, no image. The first job sent to it must not be refused for flags nobody read
yet: the job's opening contact reads the worker's CPU flags and image, saves them to
N's registry entry -- N's alone, and never over an entry re-pinned meanwhile -- and the
CPU check then judges the filled entry. A CPU that really lacks the arch is still
refused with today's "lacks" message, naming only what the real CPU lacks. An entry
that is not provisioned is never re-read by a job.

Against the fake worker of tests/_fake_worker.py: the real CLI, ``worker.job`` and
``remote`` run through the fake's ``ssh`` shim; mDNS is a table patched in as
``find`` (and ``mdns.browse``), as in tests/test_cli_worker_resolve.py. The re-pin
"during the probe" is an ``ssh`` wrapper ahead of the fake's on PATH: on the first
connection, before it is made, the registry is replaced with N re-pinned.

Requirements exercised: R3.5.
"""

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli as cli_mod
from shidashi import config, isaguard, mdns, remote, workers
from shidashi.cli import app
from tests._fake_worker import BUILD_ID, ZEN3_CPUINFO_FLAGS, FakeWorker, seed_host_cache

runner = CliRunner()

NEIGHBOUR = "bentoo-lab2"  # a like-named second provisioned worker
NEIGHBOUR_AT = "192.0.2.51"
#: The job every test sends: an arch-guarded command (assemble v3).
JOB = ["kde", "--", "assemble", "v3", "kde", "systemd"]
#: The real CPU of the "lacking" scenarios: Zen 3 without avx2 -- v3 needs it.
NO_AVX2 = tuple(f for f in ZEN3_CPUINFO_FLAGS if f != "avx2")


class _Lan:
    """mDNS as a table: ``answers`` maps a worker name to the address it answers from."""

    def __init__(self) -> None:
        self.answers: dict[str, str] = {}
        self.names: list[str] = []

    def find(self, name: str, **_kw: Any) -> str | None:
        self.names.append(name)
        return self.answers.get(name)

    def browse(self, *args: Any, **kw: Any) -> list[mdns.Found]:
        service = str(args[0] if args else kw.get("service", ""))
        if "shidashi-worker" not in service:
            return []
        return [mdns.Found(n, a, 22, (("v", "1"),)) for n, a in sorted(self.answers.items())]


@pytest.fixture
def fw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWorker]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "200")
    w = FakeWorker.install(tmp_path / "fw", monkeypatch)
    seed_host_cache(w)  # what the job's push reads
    yield w
    w.close()


@pytest.fixture
def lan(monkeypatch: pytest.MonkeyPatch) -> _Lan:
    fake = _Lan()
    monkeypatch.setattr(mdns, "browse", fake.browse)
    for module in (mdns, cli_mod, remote):
        monkeypatch.setattr(module, "find", fake.find, raising=False)
    return fake


def _job(fw: FakeWorker, capfd: pytest.CaptureFixture[str]) -> tuple[Result, str]:
    result = runner.invoke(app, ["worker", "job", fw.name, *JOB])
    captured = capfd.readouterr()
    return result, result.output + captured.out + captured.err


def _registry() -> dict[str, workers.WorkerEntry]:
    return workers.load_registry(config.workers_dir() / "workers.json")


def _dumps() -> dict[str, dict[str, Any]]:
    return {name: e.model_dump(mode="json") for name, e in _registry().items()}


def _provisioned(fw: FakeWorker, **changes: Any) -> workers.WorkerEntry:
    """N as ``provision`` records it: pinned, never seen -- no address, flags or image."""
    raw = fw.entry().model_dump(mode="json")
    for field in ("address", "cpu_flags", "image"):
        raw.pop(field)
    raw["provisioned"] = True
    raw.update(changes)
    return workers.WorkerEntry.model_validate(raw)


def _no_traceback(result: Result, out: str) -> None:
    assert "Traceback" not in out
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception


def _started(fw: FakeWorker, out: str) -> bool:
    return "fake-shidashi: assemble v3 kde systemd" in out or bool(fw.job_invocations())


def _lacks_only_avx2(fw: FakeWorker, out: str) -> bool:
    """Today's refusal, naming exactly what the REAL CPU lacks -- not the empty entry's
    whole list (``it lacks aes, avx, avx2, ...``)."""
    flat = " ".join(out.split())
    refusal = rf"{re.escape(fw.name)}'s CPU cannot run v3: it lacks avx2(?![\w,])"
    return re.search(refusal, flat) is not None


def _repin_on_first_ssh(fw: FakeWorker, monkeypatch: pytest.MonkeyPatch, repinned: Path) -> Path:
    """An ``ssh`` ahead of the fake's: the first connection first swaps the registry for
    ``repinned`` (atomically), then goes on as usual. Returns the marker it leaves."""
    wrap = fw.base / "repin-bin"
    wrap.mkdir()
    marker = fw.base / "repinned.done"
    registry = config.workers_dir() / "workers.json"
    script = wrap / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ ! -e "{marker}" ]; then\n'
        f'  : > "{marker}"\n'
        f'  cp "{repinned}" "{registry}.repin.tmp" && mv "{registry}.repin.tmp" "{registry}"\n'
        "fi\n"
        f'exec "{fw.base / "host-bin" / "ssh"}" "$@"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrap}{os.pathsep}{os.environ['PATH']}")
    return marker


# --- hostile first: what the fill must never touch ------------------------------------


def test_job_never_overwrites_an_entry_re_pinned_during_its_probe(
    fw: FakeWorker,
    lan: _Lan,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """N is re-paired (a new host key) while the job's probe is out: the flags and image
    that probe read belong to the OLD pin and must not land on the new entry."""
    fw.register(_provisioned(fw))
    lan.answers[fw.name] = fw.address
    repinned = _provisioned(
        fw,
        host_key="ssh-ed25519 AAAAC3NzaREPINNED",
        host_key_fingerprint="SHA256:RepinnedRepinnedRepinnedRepinnedRepinned00",
        paired_at="2026-10-10T12:00:00Z",
    )
    doc = fw.base / "repinned.json"
    doc.write_text(json.dumps({fw.name: repinned.model_dump(mode="json")}, indent=1) + "\n")
    marker = _repin_on_first_ssh(fw, monkeypatch, doc)
    result, out = _job(fw, capfd)
    assert marker.exists(), f"the job's opening probe never reached {fw.name}:\n{out}"
    assert _dumps() == {fw.name: repinned.model_dump(mode="json")}, out
    _no_traceback(result, out)


def test_job_fills_n_alone_and_leaves_a_like_named_provisioned_neighbour_as_it_was(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """Third element: a second provisioned worker, empty too and answering over mDNS as
    well, keeps its entry byte for byte; no entry is added or renamed."""
    neighbour = _provisioned(fw, name=NEIGHBOUR)
    fw.register(_provisioned(fw), neighbour)
    lan.answers.update({fw.name: fw.address, NEIGHBOUR: NEIGHBOUR_AT})
    before = _dumps()
    result, out = _job(fw, capfd)
    assert result.exit_code == 0, out
    after = _dumps()
    assert set(after) == {fw.name, NEIGHBOUR}
    assert after[NEIGHBOUR] == before[NEIGHBOUR]
    got = _registry()[fw.name]
    assert "avx2" in got.cpu_flags and got.image == BUILD_ID


@pytest.mark.parametrize(
    "flags",
    [pytest.param((), id="empty-flags"), pytest.param(("sse2",), id="stale-flags")],
)
def test_job_does_not_re_read_a_non_provisioned_entry(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    """Same emptiness, not provisioned: the job judges the entry as recorded (today's
    refusal) and the registry keeps the flags and image it had -- only a provisioned
    entry is completed by a job."""
    raw = fw.entry(cpu_flags=flags).model_dump(mode="json")
    raw["image"] = ""
    fw.register(workers.WorkerEntry.model_validate(raw))
    lan.answers[fw.name] = fw.address
    before = _dumps()
    result, out = _job(fw, capfd)
    assert result.exit_code == 1, out
    assert "lacks" in out
    assert _dumps() == before
    assert not _started(fw, out)
    _no_traceback(result, out)


def test_job_on_a_provisioned_cpu_lacking_the_arch_is_refused_after_the_fill(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """The real CPU lacks avx2: refused with today's message, naming avx2 alone (the
    filled flags, not the empty entry's), nothing pushed or started -- and the flags
    and image the probe read are recorded all the same."""
    assert isaguard.missing("v3", NO_AVX2) == ("avx2",)  # the premise
    fw.set(cpu_flags=list(NO_AVX2))
    fw.register(_provisioned(fw))
    lan.answers[fw.name] = fw.address
    result, out = _job(fw, capfd)
    assert result.exit_code == 1, out
    assert _lacks_only_avx2(fw, out), out
    assert fw.rsync_calls() == []
    assert not _started(fw, out)
    got = _registry()[fw.name]
    assert got.cpu_flags, "the probe's flags were not recorded"
    assert "avx2" not in got.cpu_flags and "fma" in got.cpu_flags
    assert got.image == BUILD_ID
    _no_traceback(result, out)


# --- benign: found, filled, judged on the filled entry, started -----------------------


def test_job_on_a_freshly_provisioned_worker_records_flags_and_image_and_starts(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """R3.5: no address, no flags, no image -- found over mDNS, filled, not refused,
    started; the pin fields stay as provisioned."""
    entry = _provisioned(fw)
    fw.register(entry)
    lan.answers[fw.name] = fw.address
    result, out = _job(fw, capfd)
    assert result.exit_code == 0, out
    assert "lacks" not in out
    assert _started(fw, out)
    got = _registry()[fw.name]
    assert "avx2" in got.cpu_flags and "pni" in got.cpu_flags
    assert got.image == BUILD_ID
    assert got.address == fw.address
    assert (got.host_key, got.host_key_fingerprint, got.paired_at) == (
        entry.host_key,
        entry.host_key_fingerprint,
        entry.paired_at,
    )
    fw.assert_pinned()
    _no_traceback(result, out)


def test_job_on_a_provisioned_entry_with_flags_but_no_image_records_the_image(
    fw: FakeWorker, lan: _Lan, capfd: pytest.CaptureFixture[str]
) -> None:
    """The other half of "cpu_flags OR image": flags already there, image empty."""
    fw.register(_provisioned(fw, cpu_flags=list(ZEN3_CPUINFO_FLAGS)))
    lan.answers[fw.name] = fw.address
    result, out = _job(fw, capfd)
    assert result.exit_code == 0, out
    assert _started(fw, out)
    got = _registry()[fw.name]
    assert got.image == BUILD_ID
    assert set(got.cpu_flags) == set(ZEN3_CPUINFO_FLAGS)
