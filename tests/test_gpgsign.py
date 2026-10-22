"""``shidashi.gpgsign``: the maintainer's gpg on the host (story 030, task 1.1; R1.6, R6.4).

Contract from design.md "shidashi/gpgsign.py": ``as_maintainer(argv)`` (runuser as
``SUDO_USER`` with their ``HOME`` under sudo, ``argv`` unchanged otherwise),
``host_gpg(argv, what, stdin=None) -> bytes`` (any failure is a ``GpgError``),
``check_key(key)`` (an unknown key or no agent is a ``GpgError`` naming the key),
``detach_sign(path, *, key) -> <path>.asc`` (a detached, ASCII-armored signature made by
the maintainer's agent), ``export_public(key, dest) -> dest`` (empty export = ``GpgError``)
and ``GpgError``, a ``BinhostError`` so story 029's callers keep catching it.

``gpg`` and ``runuser`` on the host are fakes on ``PATH`` (the tests/test_publish_pool.py
pattern): each call is logged to ``FAKE_GPG_LOG`` with its argv and ``HOME``. The fake
``gpg --detach-sign`` "signs" with ``<key> <sha256 of the data>`` inside an armored block,
so a test can tell which file was signed, and by which key. Real signatures are
tests/test_release_sign_host.py's.
"""

import getpass
import hashlib
import json
import os
import pwd
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import

GpgError: Any = try_import("shidashi.gpgsign", "GpgError")
as_maintainer: Any = try_import("shidashi.gpgsign", "as_maintainer")
host_gpg: Any = try_import("shidashi.gpgsign", "host_gpg")
check_key: Any = try_import("shidashi.gpgsign", "check_key")
detach_sign: Any = try_import("shidashi.gpgsign", "detach_sign")
export_public: Any = try_import("shidashi.gpgsign", "export_public")
BinhostError: Any = try_import("shidashi.binhost", "BinhostError")

FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
OTHER = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"

_FAKE_MAINTAINER_GPG = r"""
import hashlib, json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_GPG_LOG"], "a") as log:
    log.write(json.dumps({"tool": name, "argv": args, "home": os.environ.get("HOME")}) + "\n")
if name in ("sudo", "runuser"):
    import pwd
    user, i = None, 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in ("-u", "--user"):
            user, i = args[i + 1], i + 2
            continue
        if a.startswith("--user="):
            user, i = a.split("=", 1)[1], i + 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    env = dict(os.environ)
    if user:
        env["HOME"] = pwd.getpwnam(user).pw_dir
    os.execvpe(args[i], args[i:], env)


def value_of(*flags):
    for flag in flags:
        if flag in args:
            return args[args.index(flag) + 1]
    return None


def emit(text, where):
    if where is not None and where != "-":
        Path(where).write_text(text)
    else:
        sys.stdout.write(text)


if "--list-secret-keys" in args or "-K" in args:
    if os.environ.get("FAKE_GPG_NO_AGENT"):
        sys.stderr.write("gpg: can't connect to the agent: IPC connect call failed\n")
        sys.exit(2)
    known = os.environ.get("FAKE_GPG_SECRET_KEYS", "").split(",")
    if any(k and k in args for k in known):
        print("sec   ed25519 2026-10-21 [SC]")
        sys.exit(0)
    sys.stderr.write("gpg: error reading key: No secret key\n")
    sys.exit(2)
if "--detach-sign" in args or "-b" in args:
    key = value_of("--local-user", "-u", "--default-key")
    out = value_of("--output", "-o")
    data = Path(args[-1]).read_bytes()
    if os.environ.get("FAKE_GPG_SIGN_FAIL"):
        if out is not None and out != "-":
            Path(out).write_text("half a signature")  # what a gpg that died leaves
        sys.stderr.write("gpg: signing failed: No secret key\n")
        sys.exit(2)
    sig = (
        "-----BEGIN PGP SIGNATURE-----\n\n"
        + f"{key} {hashlib.sha256(data).hexdigest()}\n"
        + "-----END PGP SIGNATURE-----\n"
    )
    emit(sig, out)
    sys.exit(0)
if "--export" in args:
    if os.environ.get("FAKE_GPG_EXPORT_EMPTY"):
        sys.exit(0)  # gpg exports nothing, exit 0, for a key it does not have
    out = value_of("--output", "-o")
    key = [a for a in args if not a.startswith("-") and a != out][-1]
    emit(
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
        + f"public key {key}\n"
        + "-----END PGP PUBLIC KEY BLOCK-----\n",
        out,
    )
    sys.exit(0)
if os.environ.get("FAKE_GPG_EXIT"):
    sys.stderr.write("gpg: something broke\n")
    sys.exit(int(os.environ["FAKE_GPG_EXIT"]))
sys.exit(0)
"""


def fake_signature(key: str, data: bytes) -> str:
    """What the fake ``gpg --detach-sign`` writes for ``data`` signed by ``key``."""
    return (
        "-----BEGIN PGP SIGNATURE-----\n\n"
        f"{key} {hashlib.sha256(data).hexdigest()}\n"
        "-----END PGP SIGNATURE-----\n"
    )


@pytest.fixture
def host_gpg_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake ``gpg``/``gpg2``/``runuser``/``sudo`` first on ``PATH``; returns the call log."""
    gpgbin = tmp_path / "host-gpg-bin"
    gpgbin.mkdir()
    tool = gpgbin / "fake-gpg"
    tool.write_text(f"#!{sys.executable}\n{_FAKE_MAINTAINER_GPG}", encoding="utf-8")
    tool.chmod(0o755)
    for name in ("gpg", "gpg2", "sudo", "runuser"):
        (gpgbin / name).symlink_to(tool)
    log = tmp_path / "gpg-calls.jsonl"
    monkeypatch.setenv("PATH", f"{gpgbin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GPG_LOG", str(log))
    monkeypatch.setenv("FAKE_GPG_SECRET_KEYS", FPR)
    monkeypatch.delenv("SUDO_USER", raising=False)
    return log


def calls(log: Path, tool: str | None = None) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return [e for e in entries if tool is None or e["tool"] == tool]


def gpg_calls(log: Path) -> list[dict[str, Any]]:
    return [e for e in calls(log) if e["tool"] in ("gpg", "gpg2")]


def under_sudo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """euid 0 with ``SUDO_USER`` = this user, and ``HOME`` pointing elsewhere as root's
    would; returns the invoking user's real home."""
    user = getpass.getuser()
    monkeypatch.setenv("SUDO_USER", user)
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    return pwd.getpwnam(user).pw_dir


# --- as_maintainer / host_gpg (moved from publish_pool, behaviour unchanged) ----------------


def test_as_maintainer_leaves_the_command_alone_without_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    argv = ["gpg", "--batch", "--list-secret-keys", FPR]
    assert as_maintainer(argv) == argv


def test_as_maintainer_runs_the_command_as_the_invoking_user_with_their_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6.4: under sudo the agent and keyring are SUDO_USER's, never root's."""
    home = under_sudo(tmp_path, monkeypatch)
    argv = ["gpg", "--batch", "--list-secret-keys", FPR]
    wrapped = as_maintainer(argv)
    assert "--" in wrapped, wrapped
    assert wrapped[: wrapped.index("--") + 1] == ["runuser", "-u", getpass.getuser(), "--"]
    assert f"HOME={home}" in wrapped
    assert wrapped[-len(argv) :] == argv


def test_host_gpg_returns_stdout_and_turns_a_failure_into_a_gpg_error_with_its_stderr(
    host_gpg_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = host_gpg(["gpg", "--armor", "--export", FPR], "export the key")
    assert f"public key {FPR}".encode() in out
    monkeypatch.setenv("FAKE_GPG_EXIT", "2")
    with pytest.raises(GpgError) as err:
        host_gpg(["gpg", "--card-status"], "read the card")
    assert "something broke" in str(err.value)
    assert "read the card" in str(err.value)


def test_a_gpg_error_is_still_a_binhost_error_for_story_029s_callers(
    host_gpg_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_EXIT", "2")
    with pytest.raises(BinhostError):
        host_gpg(["gpg", "--card-status"], "read the card")


# --- check_key (R1.6) ------------------------------------------------------------------------


def test_check_key_accepts_a_key_the_maintainer_holds_the_secret_of(
    host_gpg_log: Path,
) -> None:
    assert check_key(FPR) is None
    listings = [c for c in gpg_calls(host_gpg_log) if FPR in c["argv"]]
    assert len(listings) == 1, listings
    (listing,) = listings
    assert "--list-secret-keys" in listing["argv"] or "-K" in listing["argv"]


def test_check_key_refuses_a_key_with_no_secret_naming_it(host_gpg_log: Path) -> None:
    """Hostile half first: a key the keyring does not hold must not pass because gpg ran."""
    with pytest.raises(GpgError) as err:
        check_key(OTHER)
    assert OTHER in str(err.value)
    assert gpg_calls(host_gpg_log), "the keyring was never asked"


def test_check_key_refuses_when_no_agent_answers_naming_the_key(
    host_gpg_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GPG_NO_AGENT", "1")
    with pytest.raises(GpgError) as err:
        check_key(FPR)
    assert FPR in str(err.value)


def test_check_key_under_sudo_asks_the_invoking_users_keyring(
    host_gpg_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = under_sudo(tmp_path, monkeypatch)
    check_key(FPR)
    assert calls(host_gpg_log, "runuser"), "gpg did not run as the maintainer"
    assert {c["home"] for c in gpg_calls(host_gpg_log)} == {home}


# --- detach_sign (R6.2, R6.4) ----------------------------------------------------------------


def test_detach_sign_writes_the_armored_signature_of_that_file_beside_it(
    host_gpg_log: Path, tmp_path: Path
) -> None:
    data = tmp_path / "SHA256SUMS"
    data.write_text("0" * 64 + "  release.json\n", encoding="utf-8")
    asc = detach_sign(data, key=FPR)
    assert Path(asc) == tmp_path / "SHA256SUMS.asc"
    assert Path(asc).read_text(encoding="utf-8") == fake_signature(FPR, data.read_bytes())
    signing = [c for c in gpg_calls(host_gpg_log) if "--detach-sign" in c["argv"]]
    assert len(signing) == 1, signing
    argv = signing[0]["argv"]
    assert "--armor" in argv or "-a" in argv
    assert FPR in argv, "signed without naming --key"


def test_detach_sign_of_a_digests_file_is_its_name_plus_asc_never_the_isos(
    host_gpg_log: Path, tmp_path: Path
) -> None:
    """The derived name: ``x.iso.DIGESTS`` -> ``x.iso.DIGESTS.asc``. ``x.iso.asc`` (a suffix
    swap) would be a signature beside the ISO that signs something else. A sibling's
    DIGESTS is left alone."""
    iso = tmp_path / "bentoo-2026.10.25-kde-systemd-v3.iso"
    iso.write_bytes(b"iso")
    mine = tmp_path / f"{iso.name}.DIGESTS"
    mine.write_text("# SHA512 HASH\nabc  x\n", encoding="utf-8")
    sibling = tmp_path / "bentoo-2026.10.25-kde-systemd-v3-xz.iso.DIGESTS"
    sibling.write_text("# SHA512 HASH\ndef  y\n", encoding="utf-8")
    asc = detach_sign(mine, key=FPR)
    assert Path(asc).name == f"{iso.name}.DIGESTS.asc"
    assert not (tmp_path / f"{iso.name}.asc").exists()
    assert not (tmp_path / f"{sibling.name}.asc").exists()
    assert Path(asc).read_text(encoding="utf-8") == fake_signature(FPR, mine.read_bytes())


def test_under_sudo_the_signature_is_made_by_the_invoking_users_agent(
    host_gpg_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6.4: gpg signs as SUDO_USER with their HOME -- root's HOME is elsewhere here."""
    home = under_sudo(tmp_path, monkeypatch)
    data = tmp_path / "SHA256SUMS"
    data.write_text("0" * 64 + "  release.json\n", encoding="utf-8")
    asc = detach_sign(data, key=FPR)
    assert Path(asc) == tmp_path / "SHA256SUMS.asc"
    assert Path(asc).read_text(encoding="utf-8") == fake_signature(FPR, data.read_bytes())
    signing = [c for c in gpg_calls(host_gpg_log) if "--detach-sign" in c["argv"]]
    assert signing and {c["home"] for c in signing} == {home}
    assert calls(host_gpg_log, "runuser"), "gpg did not run as the maintainer"


def test_a_signature_gpg_cannot_make_is_a_gpg_error_and_leaves_no_asc(
    host_gpg_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6.6: the fake gpg leaves half a file at ``--output`` when it fails, as a gpg that
    died would; nothing named ``.asc`` may remain."""
    monkeypatch.setenv("FAKE_GPG_SIGN_FAIL", "1")
    data = tmp_path / "SHA256SUMS"
    data.write_text("0" * 64 + "  release.json\n", encoding="utf-8")
    with pytest.raises(GpgError) as err:
        detach_sign(data, key=FPR)
    assert "SHA256SUMS" in str(err.value)
    assert sorted(p.name for p in tmp_path.glob("SHA256SUMS*")) == ["SHA256SUMS"]


# --- export_public (R6.1) --------------------------------------------------------------------


def test_export_public_writes_the_armored_public_key_of_that_key(
    host_gpg_log: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bentoo-release-key.asc"
    assert Path(export_public(FPR, dest)) == dest
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert f"public key {FPR}" in text
    assert "PRIVATE" not in text
    exporting = [c for c in gpg_calls(host_gpg_log) if "--export" in c["argv"]]
    assert len(exporting) == 1 and FPR in exporting[0]["argv"], exporting


def test_export_public_of_a_key_the_keyring_lacks_is_a_gpg_error_and_writes_nothing(
    host_gpg_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gpg exits 0 and prints nothing for a key it does not have: still a refusal."""
    monkeypatch.setenv("FAKE_GPG_EXPORT_EMPTY", "1")
    dest = tmp_path / "bentoo-release-key.asc"
    with pytest.raises(GpgError) as err:
        export_public(OTHER, dest)
    assert OTHER in str(err.value)
    assert not dest.exists()


def test_export_public_under_sudo_reads_the_invoking_users_keyring(
    host_gpg_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = under_sudo(tmp_path, monkeypatch)
    dest = tmp_path / "bentoo-release-key.asc"
    export_public(FPR, dest)
    exporting = [c for c in gpg_calls(host_gpg_log) if "--export" in c["argv"]]
    assert exporting and {c["home"] for c in exporting} == {home}
    assert f"public key {FPR}" in dest.read_text(encoding="utf-8")
