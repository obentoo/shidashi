"""``shidashi release upload <dir> --remote R [--keep 4] [--dry-run] [--yes] [--output-root D]``
end to end (story 033, Task 4; R2.1-R2.3, R3.1-R3.5, R4.1-R4.5, R5.1-R5.4, Q4, Q11).

Contract (design.md "shidashi release upload", "Data Models"): preflight before any rclone
call (rclone on PATH, a 0600 config of the caller, signatures verifying against the key the
directory carries); the public set to ``releases/<date>/``, data first, ``SHA256SUMS.asc``
last, a failed copy leaving the index untouched; ``index.json`` (schema 1, newest first, one
entry per date) and ``latest.json`` written through a temporary object, an unreadable index
never overwritten; beyond ``--keep`` the oldest leave the index before they are purged, never
the current one, without a host copy only with ``--yes``; ``--dry-run`` changes nothing.

The bucket is ``FakeRclone`` (tests/_fake_rclone.py), the gpg ``FakeMaintainerGpg``.
``UPLOAD`` is the one line to change if story 032 registered ``release-report``.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli, release_sign
from shidashi import release as release_core
from tests._fake_rclone import FakeRclone, install_fake_rclone
from tests.test_release_core import FPR, STRANGER, FakeMaintainerGpg, fake_public_key, listed
from tests.test_release_sign_cmd import DAY, unsigned_release_tree
from tests.test_release_upload import INDEX, entry

UPLOAD = ["release", "upload"]
LATEST = "releases/latest.json"
DATED = re.compile(r"releases/\d{4}\.\d{2}\.\d{2}")

runner = CliRunner()


def signed_release_tree(directory: Path) -> None:
    """An unsigned release signed as ``release sign`` leaves it: ``signed: true``, the key
    exported and listed, every signature in place. Needs ``FakeMaintainerGpg`` installed."""
    body = unsigned_release_tree(directory)
    release_core.write_manifest(directory, {**body, "signed": True})
    release_sign.sign_release(directory, key=FPR)


def public_names(directory: Path) -> set[str]:
    """R3.1 applied to ``directory``: what the bucket must hold under ``releases/<date>/``."""
    data = {n for n in listed(directory) if not n.endswith(".build.tar.zst")}
    seals = {p.name for p in directory.glob("*.asc")}
    return data | seals | {"SHA256SUMS"}


@dataclass
class UploadLab:
    root: Path
    rclone: FakeRclone
    maintainer: FakeMaintainerGpg
    runs: Path

    def release(self, date: str = DAY) -> Path:
        directory = self.root / date
        signed_release_tree(directory)
        return directory

    def upload(self, directory: Path, *extra: str) -> Result:
        argv = [*UPLOAD, str(directory), "--remote", self.rclone.remote]
        return runner.invoke(cli.app, [*argv, "--output-root", str(self.root), *extra])

    def seed(self, *dates: str) -> None:
        """Earlier complete releases in the bucket and in its index, newest first."""
        for date in dates:
            for name in ("SHA256SUMS", "SHA256SUMS.asc", "bentoo-release-key.asc"):
                self._put(f"releases/{date}/{name}", f"{name} of {date}\n".encode())
            self._put(f"releases/{date}/bentoo-{date}-minimal-systemd-v3.iso", b"iso")
        ordered = sorted(dates, reverse=True)
        index = {
            "schema": 1,
            "updated": "2026-10-18T10:00:00Z",
            "releases": [entry(d) for d in ordered],
        }
        self._put(INDEX, json.dumps(index).encode())
        if ordered:
            self._put(LATEST, json.dumps({"schema": 1, "release": entry(ordered[0])}).encode())

    def on_host(self, *dates: str, pruned: bool = False) -> None:
        for date in dates:
            (self.root / ".pruned" / date if pruned else self.root / date).mkdir(
                parents=True, exist_ok=True
            )

    def _put(self, path: str, data: bytes) -> None:
        target = self.rclone.obj(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def index(self) -> dict[str, Any]:
        """The bucket's index; empty when there is none."""
        path = self.rclone.obj(INDEX)
        data: dict[str, Any] = json.loads(path.read_bytes()) if path.is_file() else {}
        return data

    def index_dates(self) -> list[str]:
        return [e.get("date") for e in self.index().get("releases", [])]

    def sent(self, date: str) -> set[str]:
        """The names under ``releases/<date>/`` in the bucket."""
        folder = self.rclone.obj(f"releases/{date}")
        return {p.name for p in folder.iterdir()} if folder.is_dir() else set()

    def copies(self) -> list[str]:
        """The destination of every ``copyto``, in order."""
        return [
            c["args"][-1].partition("/")[2] for c in self.rclone.calls() if c["cmd"] == "copyto"
        ]

    def moves(self) -> list[tuple[str, str]]:
        return [
            (c["args"][0].partition("/")[2], c["args"][1].partition("/")[2])
            for c in self.rclone.calls()
            if c["cmd"] == "moveto"
        ]

    def purges(self) -> list[dict[str, Any]]:
        return [c for c in self.rclone.calls() if c["cmd"] == "purge"]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UploadLab:
    for name in ("CACHE", "SCRATCH"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setattr(cli._err_console, "_width", 10_000, raising=False)
    fake = install_fake_rclone(tmp_path, monkeypatch)
    maintainer = FakeMaintainerGpg().install(monkeypatch)
    root = tmp_path / "releases"
    root.mkdir()
    return UploadLab(root, fake, maintainer, Path(os.environ["SHIDASHI_RUNS"]))


def streams(result: Result) -> str:
    """Everything the command printed, on either stream."""
    seen = [result.output]
    for name in ("stdout", "stderr"):
        try:
            seen.append(str(getattr(result, name)))
        except ValueError:  # a stream the runner did not capture apart
            continue
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        seen.append(repr(result.exception))
    return "\n".join(seen)


# --- 4.1 preflight (R2.1-R2.3) --------------------------------------------------------------------


def test_preflight_refuses_an_unsigned_directory_before_any_rclone_call(lab: UploadLab) -> None:
    """R2.3: no signature, nothing sent."""
    directory = lab.root / DAY
    unsigned_release_tree(directory)
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert "SHA256SUMS.asc" in result.output or "signed" in result.output, result.output
    assert lab.rclone.calls() == []


def test_preflight_refuses_a_sha256sums_changed_after_signing(lab: UploadLab) -> None:
    """R2.3: the signature no longer verifies: exit 1 naming SHA256SUMS, nothing sent."""
    directory = lab.release()
    with (directory / "SHA256SUMS").open("a", encoding="utf-8") as sums:
        sums.write("0" * 64 + "  extra.iso\n")
    (directory / "extra.iso").write_bytes(b"planted")
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert "SHA256SUMS" in result.output, result.output
    assert lab.rclone.calls() == []


def test_preflight_checks_against_the_key_the_directory_carries(lab: UploadLab) -> None:
    """R2.3, hostile half: the signatures are the maintainer's, but the key shipped beside
    them is another one: a user could not verify the release, so it is not sent."""
    directory = lab.release()
    (directory / "bentoo-release-key.asc").write_text(fake_public_key(STRANGER), encoding="utf-8")
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert lab.rclone.calls() == []


def test_preflight_refuses_when_rclone_is_not_on_path(
    lab: UploadLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.1: exit 1 naming rclone, before any transfer."""
    directory = lab.release()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert "rclone" in result.output, result.output
    assert lab.rclone.calls() == [] and lab.rclone.snapshot() == {}


@pytest.mark.parametrize("problem", ["missing", "0644"])
def test_preflight_refuses_a_config_that_is_missing_or_readable_by_others(
    lab: UploadLab, problem: str
) -> None:
    """R2.2: exit 1 naming the config file, before any transfer."""
    directory = lab.release()
    if problem == "missing":
        lab.rclone.config.unlink()
    else:
        lab.rclone.config.chmod(0o644)
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert str(lab.rclone.config) in result.output, result.output
    assert lab.rclone.calls() == []


# --- 4.2 transfer and index (R3.1-R3.5, R4.1-R4.5) -----------------------------------------------


def test_transfer_lays_out_the_public_set_under_releases_date(lab: UploadLab) -> None:
    """R3.1, R3.3: every public file, byte for byte; no run bundle, no hidden file."""
    directory = lab.release()
    result = lab.upload(directory)
    assert result.exit_code == 0, result.output
    folder = lab.rclone.obj(f"releases/{DAY}")
    sent = lab.sent(DAY)
    assert sent == public_names(directory)
    for name in sent:
        assert (folder / name).read_bytes() == (directory / name).read_bytes(), name
    assert not [n for n in sent if n.endswith(".build.tar.zst") or n.startswith(".")]


def test_transfer_sends_data_before_the_seal_and_sha256sums_asc_last(lab: UploadLab) -> None:
    """R3.2: a reader never sees signed sums naming files not there yet."""
    directory = lab.release()
    result = lab.upload(directory)
    assert result.exit_code == 0, result.output
    mine = [
        p.removeprefix(f"releases/{DAY}/") for p in lab.copies() if p.startswith(f"releases/{DAY}/")
    ]
    seal = {"SHA256SUMS"} | {p.name for p in directory.glob("*.asc")}
    data_at = [i for i, name in enumerate(mine) if name not in seal]
    assert data_at and max(data_at) < mine.index("SHA256SUMS"), mine
    assert mine[-1] == "SHA256SUMS.asc", mine
    last_release_copy = max(
        i for i, p in enumerate(lab.copies()) if p.startswith(f"releases/{DAY}/")
    )
    index_ops = [i for i, p in enumerate(lab.copies()) if not p.startswith(f"releases/{DAY}/")]
    assert all(i > last_release_copy for i in index_ops), lab.copies()


def test_transfer_writes_the_index_and_latest_through_a_temporary_object(lab: UploadLab) -> None:
    """R4.5: never a copy straight onto ``index.json`` or ``latest.json``; the temporary
    objects are gone afterwards."""
    result = lab.upload(lab.release())
    assert result.exit_code == 0, result.output
    assert INDEX not in lab.copies() and LATEST not in lab.copies(), lab.copies()
    moved_onto = {dst for _src, dst in lab.moves()}
    assert {INDEX, LATEST} <= moved_onto, lab.moves()
    for src, _dst in lab.moves():
        assert src in lab.copies(), (src, lab.copies())
    left = {p.name for p in lab.rclone.obj("releases").iterdir() if p.is_file()}
    assert left == {"index.json", "latest.json"}, left


def test_transfer_index_lists_this_release_first_then_the_earlier_ones(lab: UploadLab) -> None:
    """R4.1, R4.3, Q12: schema 1; the new entry names its files; latest is it alone."""
    lab.seed("2026.10.11", "2026.10.18")
    lab.on_host("2026.10.11", "2026.10.18")
    directory = lab.release()
    result = lab.upload(directory)
    assert result.exit_code == 0, result.output
    index = lab.index()
    assert index["schema"] == 1 and isinstance(index["updated"], str)
    assert lab.index_dates() == [DAY, "2026.10.18", "2026.10.11"]
    first = index["releases"][0]
    isos = sorted(n for n in listed(directory) if n.endswith(".iso"))
    assert sorted(first["isos"]) == isos, first
    assert {k: v for k, v in first.items() if k != "isos"} == {
        k: v for k, v in entry(DAY).items() if k != "isos"
    }, first
    for path in ("sha256sums", "signature", "pubkey"):
        assert lab.rclone.obj(first[path]).is_file(), path
    latest = json.loads(lab.rclone.obj(LATEST).read_bytes())
    assert latest == {"schema": 1, "release": first}


def test_transfer_of_the_same_date_again_replaces_its_entry(lab: UploadLab) -> None:
    """R4.2: one entry per date."""
    directory = lab.release()
    first = lab.upload(directory)
    assert first.exit_code == 0, first.output
    again = lab.upload(directory)
    assert again.exit_code == 0, again.output
    assert lab.index_dates() == [DAY]


@pytest.mark.parametrize("which", ["an iso", "SHA256SUMS.asc"])
def test_transfer_failure_names_the_file_and_leaves_the_index_unchanged(
    lab: UploadLab, which: str
) -> None:
    """R3.4: the release is not advertised; the earlier index and latest stay byte for byte,
    whether the first data file or the very last signature fails."""
    lab.seed("2026.10.18")
    lab.on_host("2026.10.18")
    directory = lab.release()
    name = (
        sorted(n for n in listed(directory) if n.endswith(".iso"))[0]
        if which == "an iso"
        else which
    )
    index_before = lab.rclone.obj(INDEX).read_bytes()
    latest_before = lab.rclone.obj(LATEST).read_bytes()
    lab.rclone.fail("copyto", f"releases/{DAY}/{name}", f"ERROR : {name}: 500 Internal Error\n")
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    assert name in result.output, result.output
    assert lab.rclone.obj(INDEX).read_bytes() == index_before
    assert lab.rclone.obj(LATEST).read_bytes() == latest_before
    assert lab.moves() == [] and lab.purges() == []
    if which == "an iso":
        assert not lab.rclone.obj(f"releases/{DAY}/SHA256SUMS.asc").exists()


def test_transfer_failure_redacts_credentials_from_every_output_stream(lab: UploadLab) -> None:
    """R3.5, Q4: rclone's stderr carries the access key; no output, log or audit trail does,
    and the error still names the file."""
    directory = lab.release()
    lab.rclone.fail("copyto", f"releases/{DAY}/SHA256SUMS", lab.rclone.leaky_stderr())
    result = lab.upload(directory)
    assert result.exit_code == 1, result.output
    shown = streams(result)
    assert "SHA256SUMS" in shown, shown
    secrets_ = (lab.rclone.access_key_id, lab.rclone.secret_access_key)
    for secret in secrets_:
        assert secret not in shown, shown
    for root in (lab.runs, directory, lab.rclone.bucket):
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert not [s for s in secrets_ if s.encode() in data], path
    for call in lab.rclone.calls():
        assert not [s for s in secrets_ if s in " ".join(call["argv"])], call["argv"]


@pytest.mark.parametrize(
    ("label", "raw"),
    [("not JSON", b'{"schema": 1, "releases": ['), ("schema 2", b'{"schema": 2, "releases": []}')],
)
def test_transfer_refuses_a_remote_index_it_cannot_read_and_leaves_it(
    lab: UploadLab, label: str, raw: bytes
) -> None:
    """R4.4: exit 1 naming ``releases/index.json``; never overwritten."""
    lab.rclone.obj(INDEX).parent.mkdir(parents=True)
    lab.rclone.obj(INDEX).write_bytes(raw)
    result = lab.upload(lab.release())
    assert result.exit_code == 1, (label, result.output)
    assert INDEX in result.output, (label, result.output)
    assert lab.rclone.obj(INDEX).read_bytes() == raw, label
    assert not lab.rclone.obj(LATEST).exists(), label
    assert lab.moves() == [] and lab.purges() == [], label


# --- 4.3 retention and --dry-run (R5.1-R5.4) -----------------------------------------------------

EARLIER = ["2026.10.18", "2026.10.11", "2026.10.04", "2026.09.27"]


def test_retention_drops_the_oldest_after_the_index_no_longer_names_it(lab: UploadLab) -> None:
    """R5.1: five releases, keep 4: the oldest leaves the index first, then the bucket.
    Its host copy is under ``.pruned/`` -- story 032's prune set it aside -- and counts."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER[:3])
    lab.on_host(EARLIER[3], pruned=True)
    result = lab.upload(lab.release())
    assert result.exit_code == 0, result.output
    assert lab.index_dates() == [DAY, *EARLIER[:3]]
    (purge,) = lab.purges()
    assert purge["args"][0].endswith(f"releases/{EARLIER[3]}"), purge["args"]
    assert EARLIER[3] not in (purge["index_dates"] or []), purge["index_dates"]
    assert not lab.rclone.obj(f"releases/{EARLIER[3]}").exists()
    for date in [DAY, *EARLIER[:3]]:
        assert lab.rclone.obj(f"releases/{date}/SHA256SUMS").is_file(), date
    assert json.loads(lab.rclone.obj(LATEST).read_bytes())["release"]["date"] == DAY


def test_retention_keeps_and_reports_a_release_without_a_host_copy(lab: UploadLab) -> None:
    """R5.3: the oldest has no host copy: it stays in the bucket and is named."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER[:3])
    result = lab.upload(lab.release())
    assert lab.index_dates()[:1] == [DAY], result.output
    assert lab.purges() == [], lab.purges()
    assert lab.rclone.obj(f"releases/{EARLIER[3]}/SHA256SUMS").is_file()
    assert EARLIER[3] in lab.index_dates()
    assert EARLIER[3] in result.output, result.output


def test_retention_a_lookalike_host_directory_is_no_copy(lab: UploadLab) -> None:
    """R5.3, hostile half: ``2026.09.27.partial`` and ``.pruned/2026.09.27-old`` are not
    the release ``2026.09.27``."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER[:3])
    (lab.root / f"{EARLIER[3]}.partial").mkdir()
    (lab.root / ".pruned" / f"{EARLIER[3]}-old").mkdir(parents=True)
    result = lab.upload(lab.release())
    assert lab.index_dates()[:1] == [DAY], result.output
    assert lab.purges() == [], lab.purges()
    assert lab.rclone.obj(f"releases/{EARLIER[3]}/SHA256SUMS").is_file()


def test_retention_yes_deletes_a_release_without_a_host_copy(lab: UploadLab) -> None:
    """R5.3: ``--yes`` opts in."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER[:3])
    result = lab.upload(lab.release(), "--yes")
    assert result.exit_code == 0, result.output
    assert [p["args"][0].rsplit("/", 1)[-1] for p in lab.purges()] == [EARLIER[3]]
    assert not lab.rclone.obj(f"releases/{EARLIER[3]}").exists()
    assert lab.index_dates() == [DAY, *EARLIER[:3]]


def test_retention_honours_keep(lab: UploadLab) -> None:
    """R5.1: ``--keep 2``."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER)
    result = lab.upload(lab.release(), "--keep", "2")
    assert result.exit_code == 0, result.output
    assert lab.index_dates() == [DAY, EARLIER[0]]
    assert sorted(p["args"][0].rsplit("/", 1)[-1] for p in lab.purges()) == sorted(EARLIER[1:])


def test_retention_never_deletes_the_release_just_uploaded(lab: UploadLab) -> None:
    """R5.2: an older release uploaded again, with four newer ones in the bucket."""
    newer = ["2026.10.25", "2026.10.18", "2026.10.11", "2026.10.04"]
    lab.seed(*newer)
    lab.on_host(*newer)
    old = "2026.09.27"
    result = lab.upload(lab.release(old))
    assert result.exit_code == 0, result.output
    assert not [p for p in lab.purges() if p["args"][0].endswith(old)], lab.purges()
    assert lab.rclone.obj(f"releases/{old}/SHA256SUMS.asc").is_file()
    assert old in lab.index_dates()


def test_retention_purges_nothing_outside_a_dated_release(lab: UploadLab) -> None:
    """R5.1, hostile half: the remote index is data, not orders. An entry whose date is
    empty or a path would make ``purge releases/<date>`` remove the whole prefix."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER)
    index = lab.index()
    index["releases"] += [{**entry("2026.09.20"), "date": ""}, {**entry("x"), "date": "../.."}]
    lab.rclone.obj(INDEX).write_bytes(json.dumps(index).encode())
    result = lab.upload(lab.release(), "--yes")
    refused = result.exit_code == 1 and INDEX in result.output
    assert refused or lab.index_dates()[:1] == [DAY], result.output
    for purge in lab.purges():
        target = purge["args"][0].partition("/")[2]
        assert DATED.fullmatch(target), target
    for date in EARLIER[:3]:
        assert lab.rclone.obj(f"releases/{date}/SHA256SUMS").is_file(), date
    assert lab.rclone.obj(INDEX).is_file()


def test_retention_purge_failure_names_the_date_with_the_index_already_trimmed(
    lab: UploadLab,
) -> None:
    """R5.1: a failed delete leaves orphan objects, never an index naming missing files."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER)
    lab.rclone.fail("purge", f"releases/{EARLIER[3]}", "ERROR : purge: 500 Internal Error\n")
    result = lab.upload(lab.release())
    assert result.exit_code == 1, result.output
    assert EARLIER[3] in result.output, result.output
    assert EARLIER[3] not in lab.index_dates()
    assert lab.index_dates()[:1] == [DAY]


def test_dry_run_prints_the_plan_and_changes_nothing_in_the_bucket(lab: UploadLab) -> None:
    """R5.4: the upload set, the new index and the retention plan; no copy, move or purge."""
    lab.seed(*EARLIER)
    lab.on_host(*EARLIER)
    directory = lab.release()
    before = lab.rclone.snapshot()
    result = lab.upload(directory, "--dry-run")
    assert result.exit_code == 0, result.output
    assert lab.rclone.snapshot() == before
    assert not {"copyto", "moveto", "purge"} & set(lab.rclone.commands()), lab.rclone.commands()
    iso = sorted(n for n in listed(directory) if n.endswith(".iso"))[0]
    assert iso in result.output and "SHA256SUMS.asc" in result.output, result.output
    assert DAY in result.output and EARLIER[3] in result.output, result.output
