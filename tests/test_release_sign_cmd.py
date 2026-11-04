"""``shidashi release sign <dir> --key FPR`` (story 033, Task 1; R1.1-R1.6, Q4, Q11).

Contract from design.md "shidashi release sign" and tasks.md 1.1-1.3:

- ``release_publish_cli.signable_manifest(directory) -> dict``: the directory's
  ``release.json`` when its ``signed`` is exactly ``false``; ``ReleaseSignError`` naming the
  file when it is missing, unreadable, not JSON, not an object, or its ``signed`` is
  missing or anything but ``false`` (a pre-032 release has no ``signed``: it was signed by
  ``release --key``). It writes nothing.
- The command signs the directory as ``shidashi release --key`` does (story 030's staged
  signing): exit 0 once every signature verified, with ``release.json`` saying
  ``"signed": true`` and covered by the signed ``SHA256SUMS``. Exit 1 naming the key, the
  file, the directory and its holder, or the failing step; 130 on Ctrl-C. A refusal changes
  no file; a signing failure after ``release.json`` was rewritten restores
  ``"signed": false``, so a rerun can sign it.

The maintainer's gpg is ``FakeMaintainerGpg`` (tests/test_release_core.py): a "signature" is
the signer and the SHA-256 of the signed bytes, so a file changed after it was signed fails
as with gpg. The real-gpg path is tests/test_release_publish_contracts.py.

The command is spelled ``release sign`` (story text, R1.1). Story 032 task 4.2 may register
its read-side commands as ``release-report`` instead of a ``release`` group; if it did,
``SIGN`` below is the one line to change.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import cli, gpgsign, publish
from shidashi import release as release_core
from tests._pending import try_import
from tests.test_release_core import (
    FPR,
    KEY_FILE,
    STRANGER,
    FakeMaintainerGpg,
    fake_signature,
    listed,
    release_tree,
)
from tests.test_release_lock import held_by_another_process

signable_manifest: Any = try_import("shidashi.release_publish_cli", "signable_manifest")
ReleaseSignError: Any = try_import("shidashi.release_publish_cli", "ReleaseSignError")

#: The command's words before its arguments (R1.1).
SIGN = ["release", "sign"]
DAY = "2026.10.25"
#: Lock files are bookkeeping, never release content: never listed, never uploaded. A
#: refusal that took the directory's lock first may leave one behind.
LOCK_NAMES = {".release.lock", ".SHA256SUMS.lock"}

runner = CliRunner()


# --- the release directory as story 032's `release --unsigned` leaves it -------------------------


def unsigned_manifest() -> dict[str, Any]:
    """A ``release.json`` body of an unsigned release: story 032's keys, ``signed`` false."""
    return {
        "date": "2026-10-25",
        "started": "2026-10-25T00:00:03Z",
        "finished": "2026-10-25T09:12:44Z",
        "shidashi": {"commit": "ba26a6e", "dirty": False},
        "pins": {"stage3": {"snapshot": "20261018T124555Z"}, "gentoo": {}, "overlays": {}},
        "upstream_stage3": {},
        "variants": [
            {
                "arch": "v3",
                "init": "systemd",
                "status": "ok",
                "isos": [
                    f"bentoo-{DAY}-minimal-systemd-v3.iso",
                    f"bentoo-{DAY}-kde-systemd-v3-xz.iso",
                ],
                "generation": "20260823T153057Z",
                "toolchain": {"gcc": "15.2.1", "glibc": "2.42", "binutils": "2.45"},
                "publish": "skipped: unsigned",
            }
        ],
        "signed": False,
    }


def unsigned_release_tree(directory: Path) -> dict[str, Any]:
    """An unsigned release in ``directory``: ISOs, DIGESTS, a pointer, the outcome files,
    a run bundle and ``release.json`` with ``signed: false``, all in ``SHA256SUMS``; no
    signature and no exported key. Returns the manifest written."""
    release_tree(directory)
    bundle = directory / f"bentoo-{DAY}-minimal-systemd-v3.build.tar.zst"
    bundle.write_bytes(b"run bundle: events, logs, packages")
    publish.update_sha256sums(directory, {bundle.name: publish.sha256_of(bundle)})
    body = unsigned_manifest()
    release_core.write_manifest(directory, body)
    directory.chmod(0o755)
    return body


def everything(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (hidden ones too) and its bytes."""
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def content(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` but the lock files."""
    return {name: data for name, data in everything(root).items() if name not in LOCK_NAMES}


def manifest_of(directory: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    return data


def assert_manifest_listed_as_it_is(directory: Path) -> None:
    """``SHA256SUMS`` names ``release.json`` with the digest of the file on disk: a user's
    ``sha256sum -c`` passes on it."""
    on_disk = hashlib.sha256((directory / "release.json").read_bytes()).hexdigest()
    assert listed(directory).get("release.json") == on_disk, (
        "SHA256SUMS does not carry the digest of the release.json on disk"
    )


@pytest.fixture
def maintainer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMaintainerGpg:
    monkeypatch.delenv("SUDO_USER", raising=False)
    for name in ("CACHE", "SCRATCH"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.setattr(cli._err_console, "_width", 10_000, raising=False)
    return FakeMaintainerGpg().install(monkeypatch)


@pytest.fixture
def unsigned(tmp_path: Path) -> Path:
    directory = tmp_path / "releases" / DAY
    unsigned_release_tree(directory)
    return directory


def sign(directory: Path, *extra: str, key: str = FPR) -> Result:
    return runner.invoke(cli.app, [*SIGN, str(directory), "--key", key, *extra])


# --- 1.1 signable_manifest (R1.3) --------------------------------------------------------------


def test_signable_manifest_returns_an_unsigned_manifest_and_writes_nothing(unsigned: Path) -> None:
    """R1.3: the manifest of an unsigned release comes back as it is on disk."""
    before = everything(unsigned)
    assert signable_manifest(unsigned) == unsigned_manifest()
    assert everything(unsigned) == before, "signable_manifest wrote into the release"


def test_signable_manifest_accepts_compact_json_with_any_key_order(unsigned: Path) -> None:
    """R1.3, converse half: the same unsigned manifest spelled another way is still it."""
    body = {"signed": False, **{k: v for k, v in unsigned_manifest().items() if k != "signed"}}
    (unsigned / "release.json").write_text(json.dumps(body, separators=(",", ":")))
    assert signable_manifest(unsigned) == unsigned_manifest()


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("not JSON", b'{"signed": false,'),
        ("not UTF-8", b"\xff\xfe{}"),
        ("not an object", b'[{"signed": false}]'),
        ("already signed", b'{"date": "2026-10-25", "signed": true}'),
        ("pre-032, no signed key", b'{"date": "2026-10-25"}'),
        # falsy, but not false: each means something else, never "unsigned"
        ("signed is 0", b'{"date": "2026-10-25", "signed": 0}'),
        ("signed is the string false", b'{"date": "2026-10-25", "signed": "false"}'),
        ("signed is null", b'{"date": "2026-10-25", "signed": null}'),
    ],
)
def test_signable_manifest_refuses_a_manifest_that_is_not_unsigned(
    unsigned: Path, label: str, raw: bytes
) -> None:
    """R1.3: refused naming the file, the file left byte for byte."""
    (unsigned / "release.json").write_bytes(raw)
    before = everything(unsigned)
    with pytest.raises(ReleaseSignError) as refused:
        signable_manifest(unsigned)
    assert "release.json" in str(refused.value), (label, str(refused.value))
    assert everything(unsigned) == before, label


def test_signable_manifest_refuses_a_missing_manifest(unsigned: Path) -> None:
    """R1.3: no ``release.json`` is refused, naming it."""
    (unsigned / "release.json").unlink()
    with pytest.raises(ReleaseSignError) as refused:
        signable_manifest(unsigned)
    assert str(unsigned / "release.json") in str(refused.value)


# --- 1.2 the sign flow (R1.1, R1.2, R1.4, R1.6) -----------------------------------------------


def test_sign_turns_an_unsigned_release_into_a_verified_one(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.1: ``signed`` becomes true, nothing else in the manifest changes, and the
    directory is signed exactly as ``release --key`` signs one."""
    result = sign(unsigned)
    assert result.exit_code == 0, result.output

    assert manifest_of(unsigned) == {**unsigned_manifest(), "signed": True}
    assert_manifest_listed_as_it_is(unsigned)
    names = listed(unsigned)
    assert KEY_FILE in names, "the public key is not listed in SHA256SUMS"
    assert not [n for n in names if n.endswith(".asc") and n != KEY_FILE], names
    sums = (unsigned / "SHA256SUMS").read_bytes()
    assert (unsigned / "SHA256SUMS.asc").read_text(encoding="utf-8") == fake_signature(FPR, sums)
    for digests in sorted(unsigned.glob("*.iso.DIGESTS")):
        asc = digests.with_name(digests.name + ".asc")
        assert asc.read_text(encoding="utf-8") == fake_signature(FPR, digests.read_bytes())
    assert not (unsigned / ".signing").exists(), "the signing stage was left behind"
    assert release_core.verify_release(unsigned, key=FPR) == []
    assert FPR in result.output, "the verified signer is not printed"


def test_sign_signs_the_final_release_json_not_the_unsigned_one(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.1, Q11: ``release.json`` changes before ``SHA256SUMS`` is signed, so the signed
    sums carry the ``signed: true`` manifest's digest."""
    unsigned_digest = listed(unsigned)["release.json"]
    result = sign(unsigned)
    assert result.exit_code == 0, result.output
    assert listed(unsigned)["release.json"] != unsigned_digest
    assert_manifest_listed_as_it_is(unsigned)
    assert maintainer.signed[-1] == "SHA256SUMS", maintainer.signed


def test_sign_accepts_the_key_spelled_with_spaces_and_0x(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.4, converse half: the same fingerprint spelled as gpg prints it is the same key."""
    spaced = "0x" + " ".join(FPR[i : i + 4] for i in range(0, len(FPR), 4)).lower()
    result = sign(unsigned, key=spaced)
    assert result.exit_code == 0, result.output
    assert maintainer.checked == [FPR], maintainer.checked
    assert manifest_of(unsigned)["signed"] is True


def test_sign_refuses_an_already_signed_directory_naming_sha256sums_asc(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.2: exit 1 naming ``SHA256SUMS.asc``; no file changes."""
    (unsigned / "SHA256SUMS.asc").write_text(fake_signature(FPR, b"earlier"), encoding="utf-8")
    before = everything(unsigned)
    result = sign(unsigned)
    assert result.exit_code == 1, result.output
    assert "SHA256SUMS.asc" in result.output, result.output
    assert everything(unsigned) == before
    assert maintainer.signed == [] and maintainer.exported == []


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("signed true", b'{"date": "2026-10-25", "signed": true}\n'),
        ("not JSON", b"{half a release.json"),
        ("pre-032, no signed key", b'{"date": "2026-10-25"}\n'),
    ],
)
def test_sign_refuses_a_release_json_that_is_not_unsigned(
    unsigned: Path, maintainer: FakeMaintainerGpg, label: str, raw: bytes
) -> None:
    """R1.3: exit 1 naming ``release.json``; no release file changes and nothing is signed."""
    (unsigned / "release.json").write_bytes(raw)
    before = content(unsigned)
    result = sign(unsigned)
    assert result.exit_code == 1, (label, result.output)
    assert "release.json" in result.output, (label, result.output)
    assert content(unsigned) == before, label
    assert maintainer.signed == [] and maintainer.exported == [], label


def test_sign_refuses_a_missing_release_json(unsigned: Path, maintainer: FakeMaintainerGpg) -> None:
    """R1.3: no ``release.json``: exit 1 naming it."""
    (unsigned / "release.json").unlink()
    before = content(unsigned)
    result = sign(unsigned)
    assert result.exit_code == 1, result.output
    assert "release.json" in result.output, result.output
    assert content(unsigned) == before
    assert not (unsigned / "release.json").exists()


def test_sign_of_a_directory_that_does_not_exist_creates_nothing(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.3: no release there, so no ``release.json``: exit 1, and the path is not created."""
    missing = tmp_path / "releases" / DAY
    result = sign(missing)
    assert result.exit_code == 1, result.output
    assert "release.json" in result.output or str(missing) in result.output, result.output
    assert not missing.exists(), "sign created a release directory"


@pytest.mark.parametrize("bad", ["maintainer@example.invalid", "ABCD1234", FPR + "00"])
def test_sign_refuses_a_malformed_key_before_writing_anything(
    unsigned: Path, maintainer: FakeMaintainerGpg, bad: str
) -> None:
    """R1.4: exit 1 naming the key; not even a lock file is written."""
    before = everything(unsigned)
    result = sign(unsigned, key=bad)
    assert result.exit_code == 1, result.output
    assert bad in result.output, result.output
    assert everything(unsigned) == before
    assert maintainer.signed == []


def test_sign_refuses_a_key_the_keyring_has_no_secret_for(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.4: no secret, or no agent answering: exit 1 naming the key, before any write."""
    before = everything(unsigned)
    result = sign(unsigned, key=STRANGER)
    assert result.exit_code == 1, result.output
    assert STRANGER in result.output, result.output
    assert maintainer.checked == [STRANGER]
    assert everything(unsigned) == before
    assert maintainer.signed == [] and maintainer.exported == []


def test_sign_refuses_a_directory_another_process_holds(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.6: exit 1 naming the directory and the holder's pid; nothing signed."""
    before = content(unsigned)
    with held_by_another_process(unsigned / ".release.lock") as holder:
        result = sign(unsigned)
    assert result.exit_code == 1, result.output
    assert str(unsigned) in result.output, result.output
    assert f"pid {holder.pid}" in result.output, result.output
    assert content(unsigned) == before
    assert manifest_of(unsigned)["signed"] is False
    assert maintainer.signed == []


# --- 1.3 a failed signing restores the manifest (R1.5) -----------------------------------------


def assert_restored_unsigned(directory: Path) -> None:
    assert manifest_of(directory) == unsigned_manifest(), "release.json was not restored"
    assert_manifest_listed_as_it_is(directory)
    assert not (directory / "SHA256SUMS.asc").exists(), "a failed signing left SHA256SUMS.asc"
    assert not (directory / ".signing").exists()


def test_restore_after_a_failed_signature_leaves_signed_false(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.5: gpg fails on ``SHA256SUMS`` after ``release.json`` was rewritten: exit 1
    naming the step, and the manifest says ``signed: false`` again."""
    maintainer.fail_on = {"SHA256SUMS"}
    result = sign(unsigned)
    assert result.exit_code == 1, result.output
    assert "sign" in result.output.lower() and "SHA256SUMS" in result.output, result.output
    assert_restored_unsigned(unsigned)


def test_restore_after_a_signature_that_does_not_verify(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.5: the signature is made but does not verify: exit 1, ``signed: false`` again."""
    maintainer.reject = {"SHA256SUMS"}
    result = sign(unsigned)
    assert result.exit_code == 1, result.output
    assert "SHA256SUMS" in result.output, result.output
    assert_restored_unsigned(unsigned)


def test_restore_lets_a_rerun_sign_the_release(
    unsigned: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R1.5: after a failed sign the directory is re-signable."""
    maintainer.fail_on = {"SHA256SUMS"}
    failed = sign(unsigned)
    assert failed.exit_code == 1, failed.output
    maintainer.fail_on = set()
    again = sign(unsigned)
    assert again.exit_code == 0, again.output
    assert manifest_of(unsigned) == {**unsigned_manifest(), "signed": True}
    assert release_core.verify_release(unsigned, key=FPR) == []


def test_restore_after_an_interrupt_while_signing_exits_130(
    unsigned: Path, maintainer: FakeMaintainerGpg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1.5: a Ctrl-C while ``SHA256SUMS`` is signed: exit 130, ``signed: false`` again."""
    signs = maintainer.detach_sign

    def interrupted(path: Path, *, key: str) -> Path:
        if Path(path).name == "SHA256SUMS":
            raise KeyboardInterrupt
        return signs(path, key=key)

    monkeypatch.setattr(gpgsign, "detach_sign", interrupted)
    result = sign(unsigned)
    assert result.exit_code == 130, result.output
    assert_restored_unsigned(unsigned)
