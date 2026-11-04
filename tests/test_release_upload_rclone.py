"""The rclone config check, the runner and the redaction (story 033, Task 3; R2.2, R2.4,
R3.4, R3.5, Q4).

Contract from design.md "RcloneRunner" and tasks.md 3.1-3.2, all in
``shidashi.release_upload``:

- ``rclone_config() -> Path``: ``$RCLONE_CONFIG``, else the invoking user's
  ``~/.config/rclone/rclone.conf``; a regular file owned by the caller, mode 0600, else
  ``UploadError`` naming it.
- ``RcloneRunner(remote=..., config=...)`` with ``copyto(local, dst)``, ``cat(path) ->
  bytes | None``, ``moveto(src, dst)``, ``purge(path)``; paths are relative to the remote
  (``releases/<date>/<name>``). A failure is ``UploadError`` naming the step, the path and
  rclone's last stderr line, with credentials redacted by ``redact(text)``. Under ``sudo``
  rclone runs as the invoking user through ``runuser``.

The constructor's keywords and the remote-relative paths are this test's reading of the
design; ``runner_for`` is the one place to adapt them. rclone is ``FakeRclone``
(tests/_fake_rclone.py).
"""

import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._fake_rclone import (
    FAKE_RUNUSER_BODY,
    FakeRclone,
    as_sudo,
    install_fake_rclone,
    write_script,
)
from tests._pending import try_import
from tests.test_release_upload import DAY, INDEX

UploadError: Any = try_import("shidashi.release_upload", "UploadError")
rclone_config: Any = try_import("shidashi.release_upload", "rclone_config")
RcloneRunner: Any = try_import("shidashi.release_upload", "RcloneRunner")
redact: Any = try_import("shidashi.release_upload", "redact")


@pytest.fixture
def rclone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeRclone:
    return install_fake_rclone(tmp_path, monkeypatch)


def runner_for(fake: FakeRclone) -> Any:
    return RcloneRunner(remote=fake.remote, config=fake.config)


def read(path: Path) -> bytes | None:
    """The file's bytes, ``None`` when it is not there."""
    return path.read_bytes() if path.is_file() else None


def one_call(fake: FakeRclone) -> dict[str, Any]:
    calls = fake.calls()
    assert len(calls) == 1, calls
    return calls[0]


# --- 3.1 the rclone config (R2.2, R2.4) ---------------------------------------------------------


def config_file(path: Path, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[r2]\ntype = s3\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_config_from_rclone_config_when_0600_and_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.2: ``$RCLONE_CONFIG`` names it."""
    path = config_file(tmp_path / "conf" / "rclone.conf")
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert Path(rclone_config()) == path


def test_config_defaults_to_the_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.2: no ``$RCLONE_CONFIG``: ``~/.config/rclone/rclone.conf``."""
    home = tmp_path / "home"
    path = config_file(home / ".config" / "rclone" / "rclone.conf")
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert Path(rclone_config()) == path


def test_config_under_sudo_is_the_invoking_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.4: under sudo ``~`` is root's; the config is the invoking user's."""
    user_home, roots_home = tmp_path / "maintainer", tmp_path / "root"
    path = config_file(user_home / ".config" / "rclone" / "rclone.conf")
    config_file(roots_home / ".config" / "rclone" / "rclone.conf")
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(roots_home))
    as_sudo(monkeypatch, user_home)
    assert Path(rclone_config()) == path


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660])
def test_config_refuses_a_mode_other_than_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """R2.2: readable by others: refused naming it."""
    path = config_file(tmp_path / "rclone.conf", mode)
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(UploadError) as refused:
        rclone_config()
    assert str(path) in str(refused.value)


def test_config_refuses_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R2.2."""
    path = tmp_path / "nowhere" / "rclone.conf"
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(UploadError) as refused:
        rclone_config()
    assert str(path) in str(refused.value)


def test_config_refuses_a_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R2.2: not a regular file."""
    path = tmp_path / "rclone.conf"
    path.mkdir(mode=0o700)
    path.chmod(0o600)
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    monkeypatch.delenv("SUDO_USER", raising=False)
    try:
        with pytest.raises(UploadError) as refused:
            rclone_config()
    finally:
        path.chmod(0o700)
    assert str(path) in str(refused.value)


def test_config_refuses_a_symlink_to_a_0600_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.2, hostile half: the link is not the regular file it points at."""
    target = config_file(tmp_path / "real.conf")
    link = tmp_path / "rclone.conf"
    link.symlink_to(target)
    monkeypatch.setenv("RCLONE_CONFIG", str(link))
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(UploadError) as refused:
        rclone_config()
    assert str(link) in str(refused.value)


def test_config_refuses_a_file_another_user_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.2: the caller is someone else than the file's owner."""
    path = config_file(tmp_path / "rclone.conf")
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    monkeypatch.delenv("SUDO_USER", raising=False)
    someone_else = os.getuid() + 4242
    monkeypatch.setattr(os, "getuid", lambda: someone_else)
    monkeypatch.setattr(os, "geteuid", lambda: someone_else)
    with pytest.raises(UploadError) as refused:
        rclone_config()
    assert str(path) in str(refused.value)


# --- 3.2 the runner (R3.4, R3.5, R2.4) ------------------------------------------------------------


def test_rclone_copyto_puts_the_file_at_the_remote_path(rclone: FakeRclone, tmp_path: Path) -> None:
    """R3.1: one ``copyto`` per file, to the remote path given, bucket check off."""
    src = tmp_path / "x.iso"
    src.write_bytes(b"iso bytes")
    runner_for(rclone).copyto(src, f"releases/{DAY}/x.iso")
    assert read(rclone.obj(f"releases/{DAY}/x.iso")) == b"iso bytes"
    call = one_call(rclone)
    assert call["cmd"] == "copyto"
    assert "--s3-no-check-bucket" in call["argv"], call["argv"]


def test_rclone_runs_an_argv_list_never_a_shell(rclone: FakeRclone, tmp_path: Path) -> None:
    """Constraints: a name a shell would expand reaches rclone unchanged."""
    name = "a b $HOME;$(touch pwned).iso"
    src = tmp_path / name
    src.write_bytes(b"x")
    runner_for(rclone).copyto(src, f"releases/{DAY}/{name}")
    assert read(rclone.obj(f"releases/{DAY}/{name}")) == b"x"
    assert not list(Path.cwd().glob("pwned")) and not (tmp_path / "pwned").exists()


def test_rclone_passes_the_checked_file_and_no_credential_env(
    rclone: FakeRclone, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.2, R3.5: rclone reads the checked config; credentials in the parent's env (which
    rclone would prefer over the file) do not reach it, nor appear in argv."""
    monkeypatch.setenv("RCLONE_CONFIG_R2_SECRET_ACCESS_KEY", secrets.token_hex(32))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secrets.token_hex(32))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", secrets.token_hex(16))
    src = tmp_path / "x.txt"
    src.write_text("x")
    runner_for(rclone).copyto(src, f"releases/{DAY}/x.txt")
    call = one_call(rclone)
    assert call["config"] == str(rclone.config), call
    leaked = [k for k in call["env_keys"] if k.startswith(("RCLONE_CONFIG_", "AWS_"))]
    assert leaked == [], leaked
    argv = " ".join(call["argv"])
    assert rclone.secret_access_key not in argv and rclone.access_key_id not in argv


def test_rclone_cat_returns_the_object_or_none_when_missing(rclone: FakeRclone) -> None:
    """R4.1: a missing index is no error (rclone: ``directory not found``, exit 3)."""
    runner = runner_for(rclone)
    assert runner.cat(INDEX) is None
    rclone.obj(INDEX).parent.mkdir(parents=True)
    rclone.obj(INDEX).write_bytes(b'{"schema": 1}')
    assert runner.cat(INDEX) == b'{"schema": 1}'


def test_rclone_cat_failure_is_an_upload_error_naming_the_path(rclone: FakeRclone) -> None:
    """R4.4: a remote that cannot be read is not an empty index."""
    rclone.fail("cat", "index.json", "ERROR : Failed to create file system: 403 Forbidden\n")
    with pytest.raises(UploadError) as failed:
        runner_for(rclone).cat(INDEX)
    assert INDEX in str(failed.value)
    assert "403 Forbidden" in str(failed.value)


def test_rclone_moveto_and_purge(rclone: FakeRclone) -> None:
    """R4.5, R5.1."""
    rclone.obj("releases/.index.json.tmp").parent.mkdir(parents=True)
    rclone.obj("releases/.index.json.tmp").write_bytes(b"new")
    old = rclone.obj("releases/2026.09.20/SHA256SUMS")
    old.parent.mkdir(parents=True)
    old.write_text("x")
    runner = runner_for(rclone)
    runner.moveto("releases/.index.json.tmp", INDEX)
    runner.purge("releases/2026.09.20")
    assert read(rclone.obj(INDEX)) == b"new"
    assert not rclone.obj("releases/.index.json.tmp").exists()
    assert not rclone.obj("releases/2026.09.20").exists()
    assert rclone.commands() == ["moveto", "purge"]


def test_rclone_copy_failure_names_the_step_path_and_rclones_last_line(
    rclone: FakeRclone, tmp_path: Path
) -> None:
    """R3.4: the error says what failed and rclone's reason."""
    src = tmp_path / "big.iso"
    src.write_bytes(b"x")
    rclone.fail("copyto", "big.iso", "NOTICE: retrying\nERROR : big.iso: multipart upload failed\n")
    with pytest.raises(UploadError) as failed:
        runner_for(rclone).copyto(src, f"releases/{DAY}/big.iso")
    message = str(failed.value)
    assert "copyto" in message and f"releases/{DAY}/big.iso" in message, message
    assert "multipart upload failed" in message, message


def test_rclone_failure_redacts_every_credential_from_the_error(
    rclone: FakeRclone, tmp_path: Path
) -> None:
    """R3.5, Q4: rclone's stderr carries the key; the error never does."""
    src = tmp_path / "SHA256SUMS"
    src.write_text("x")
    rclone.fail("copyto", "SHA256SUMS", rclone.leaky_stderr())
    with pytest.raises(UploadError) as failed:
        runner_for(rclone).copyto(src, f"releases/{DAY}/SHA256SUMS")
    shown = f"{failed.value!s} {failed.value!r} {failed.value.args!r}"
    assert rclone.access_key_id not in shown, shown
    assert rclone.secret_access_key not in shown, shown


def test_rclone_under_sudo_runs_as_the_invoking_user(
    rclone: FakeRclone, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.4: under sudo, rclone runs through ``runuser`` as the maintainer, with their home
    and their config."""
    write_script(rclone.bin_dir / "runuser", FAKE_RUNUSER_BODY, rclone.state)
    user = as_sudo(monkeypatch, tmp_path / "maintainer")
    src = tmp_path / "x.txt"
    src.write_text("x")
    runner_for(rclone).copyto(src, f"releases/{DAY}/x.txt")
    wrapped = rclone.runuser_calls()
    assert len(wrapped) == 1 and wrapped[0][:2] == ["-u", user.name], wrapped
    call = one_call(rclone)
    assert call["home"] == str(user.home), call
    assert call["config"] == str(rclone.config), call


# --- redact (R3.5, Q4) --------------------------------------------------------------------------


@pytest.fixture
def key_material() -> Iterator[tuple[str, str, str]]:
    yield secrets.token_hex(16), secrets.token_hex(32), secrets.token_hex(32)


def test_redact_removes_the_access_key_in_every_shape(key_material: tuple[str, str, str]) -> None:
    """R3.5: labelled ``=``/``:``/JSON, any case, the Authorization header and a presigned
    URL's credential."""
    kid, secret, signature = key_material
    lines = [
        f"access_key_id = {kid}",
        f"secret_access_key={secret}",
        f"ACCESS_KEY_ID: {kid}",
        f"AWS_SECRET_ACCESS_KEY={secret}",
        f'{{"access_key_id": "{kid}", "secret_access_key": "{secret}"}}',
        f"Authorization: AWS4-HMAC-SHA256 Credential={kid}/20261024/auto/s3/aws4_request, "
        f"SignedHeaders=host, Signature={signature}",
        f"GET /bentoo?X-Amz-Credential={kid}%2F20261024%2Fauto%2Fs3%2Faws4_request HTTP/1.1",
    ]
    for line in lines:
        cleaned = redact(line)
        assert kid not in cleaned and secret not in cleaned, (line, cleaned)
        assert "***" in cleaned, (line, cleaned)
    header = redact(lines[5])
    assert signature not in header, header


def test_redact_redacts_every_line_of_a_multi_line_text(key_material: tuple[str, str, str]) -> None:
    """R3.5: rclone's stderr is many lines; the key may sit on any."""
    kid, secret, _ = key_material
    text = f"NOTICE: a\naccess_key_id = {kid}\nNOTICE: b\nsecret_access_key = {secret}\n"
    cleaned = redact(text)
    assert kid not in cleaned and secret not in cleaned, cleaned
    assert "NOTICE: a" in cleaned and "NOTICE: b" in cleaned, cleaned


def test_redact_keeps_digests_names_and_paths(key_material: tuple[str, str, str]) -> None:
    """R3.4, hostile half: an R2 secret is 64 hex digits, like a SHA-256; the digest and the
    file an error names must survive, or the error names nothing."""
    _kid, secret, _ = key_material
    digest = secrets.token_hex(32)
    iso = f"bentoo-{DAY}-minimal-systemd-v3.iso"
    text = (
        f"ERROR : releases/{DAY}/{iso}: corrupted on transfer: sha256 {digest} differ\n"
        f"secret_access_key = {secret}\n"
    )
    cleaned = redact(text)
    assert secret not in cleaned
    assert digest in cleaned, cleaned
    assert f"releases/{DAY}/{iso}" in cleaned, cleaned
    assert "corrupted on transfer" in cleaned, cleaned


def test_redact_leaves_a_text_without_key_material_unchanged() -> None:
    """R3.4: nothing to hide, nothing changed."""
    text = "ERROR : releases/2026.10.25/SHA256SUMS.asc: 403 Forbidden\n"
    assert redact(text) == text
