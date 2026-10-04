"""Unit tests of shidashi.tree -- the pinned, cooled-down ::gentoo tree (D26)."""

import datetime
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from shidashi import config, tree
from shidashi.tree import (
    TreeError,
    TreePin,
    check_cooldown,
    ensure_tree,
    fetch_snapshot,
    load_tree_pin,
    signature_ok,
    verify_detached,
)

_LAB_SNAPSHOT = Path("/var/tmp/bentoo-lab/dl/gentoo-20260926.tar.xz")


def _snapshot_bytes(date: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        data = b"MANIFEST"
        info = tarfile.TarInfo(f"gentoo-{date}/Manifest")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _pin(date: str = "20260919", payload: bytes = b"") -> TreePin:
    return TreePin(
        date=date,
        base_url="https://mirror.test/snapshots",
        sha512=hashlib.sha512(payload).hexdigest(),
    )


def test_the_repository_pin_loads_and_names_its_file() -> None:
    pin = load_tree_pin(config.seeds_dir())
    assert pin.filename == f"gentoo-{pin.date}.tar.xz"
    assert len(pin.sha512) == 128


def test_a_missing_pin_is_a_tree_error(tmp_path: Path) -> None:
    with pytest.raises(TreeError, match="cannot read"):
        load_tree_pin(tmp_path)


def test_cooldown_accepts_a_week_old_snapshot_and_refuses_a_younger_one() -> None:
    pin = _pin("20260919")
    check_cooldown(pin, today=datetime.date(2026, 9, 26))  # exactly 7 days
    with pytest.raises(TreeError, match="6 day\\(s\\) old.*gentoo-20260918"):
        check_cooldown(pin, today=datetime.date(2026, 9, 25))


def test_signature_ok_needs_goodsig_validsig_and_exit_zero() -> None:
    good = "[GNUPG:] GOODSIG EC59 Gentoo\n[GNUPG:] VALIDSIG E1D6 2026-09-20\n"
    assert signature_ok(good, 0)
    assert not signature_ok(good, 1)
    assert not signature_ok("[GNUPG:] GOODSIG EC59 Gentoo\n", 0)  # e.g. an expired key
    assert not signature_ok("[GNUPG:] BADSIG EC59 Gentoo\n", 1)


def _mirror(monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes]) -> list[str]:
    fetched: list[str] = []

    def _download(url: str, dest: Path) -> None:
        fetched.append(url)
        dest.write_bytes(files[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(tree, "_download", _download)
    return fetched


def test_fetch_and_extract_verify_then_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _snapshot_bytes("20260919")
    pin = _pin(payload=payload)
    fetched = _mirror(monkeypatch, {pin.filename: payload, f"{pin.filename}.gpgsig": b"sig"})
    verified: list[tuple[str, str]] = []

    def _verify(data: Path, sig: Path) -> None:
        verified.append((data.name, sig.name))

    dest = ensure_tree(pin, cache_dir=tmp_path, verify=_verify)

    assert dest == tmp_path / "repos" / "gentoo-20260919"
    assert (dest / "Manifest").read_bytes() == b"MANIFEST"
    assert verified == [(pin.filename, f"{pin.filename}.gpgsig")]
    assert len(fetched) == 2
    # second call: the extracted tree is reused, nothing is fetched
    assert ensure_tree(pin, cache_dir=tmp_path, download=False, verify=_verify) == dest
    assert len(fetched) == 2
    # and the cached tarball satisfies a fetch without the network
    assert fetch_snapshot(pin, cache_dir=tmp_path, download=False, verify=_verify).is_file()


def test_fetch_refuses_a_tarball_that_does_not_match_the_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pin = _pin(payload=b"the pinned bytes")
    _mirror(monkeypatch, {pin.filename: b"other bytes", f"{pin.filename}.gpgsig": b"sig"})
    with pytest.raises(TreeError, match="sha512 mismatch"):
        fetch_snapshot(pin, cache_dir=tmp_path, verify=lambda d, s: None)
    assert not (tmp_path / "trees" / pin.filename).exists()


def test_no_download_without_a_cached_snapshot_is_a_tree_error(tmp_path: Path) -> None:
    with pytest.raises(TreeError, match="--no-download"):
        fetch_snapshot(_pin(), cache_dir=tmp_path, download=False)


@pytest.mark.skipif(
    not _LAB_SNAPSHOT.is_file() or not Path(f"{_LAB_SNAPSHOT}.gpgsig").is_file(),
    reason="the lab's downloaded snapshot is not on this machine",
)
def test_the_pinned_snapshot_verifies_against_the_gentoo_key() -> None:
    """The real thing: the tarball the pin names, its signature, the system key."""
    pin = load_tree_pin(config.seeds_dir())
    assert _LAB_SNAPSHOT.name == pin.filename
    verify_detached(_LAB_SNAPSHOT, Path(f"{_LAB_SNAPSHOT}.gpgsig"))
    assert hashlib.sha512(_LAB_SNAPSHOT.read_bytes()).hexdigest() == pin.sha512


# --- the pinned ::bentoo overlay (a git commit, fetched by hash) -----------------

import subprocess  # noqa: E402

from shidashi.tree import OverlayPin, ensure_overlay, load_overlay_pins  # noqa: E402


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
        },
    ).stdout.strip()


def _overlay_remote(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits; the pin names the OLDER one, as a real pin does."""
    repo = tmp_path / "remote"
    (repo / "metadata").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=repo)  # as GitHub
    (repo / "metadata" / "layout.conf").write_text("masters = gentoo\n", encoding="utf-8")
    (repo / "pkg").write_text("old\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "old", cwd=repo)
    old = _git("rev-parse", "HEAD", cwd=repo)
    (repo / "pkg").write_text("new\n", encoding="utf-8")
    _git("commit", "-qam", "new", cwd=repo)
    return repo, old, _git("rev-parse", "HEAD", cwd=repo)


def test_the_repository_pins_bentoo_by_full_commit() -> None:
    pins = {p.name: p for p in load_overlay_pins(config.seeds_dir())}
    assert len(pins["bentoo"].commit) == 40


def test_ensure_overlay_extracts_exactly_the_pinned_commit(tmp_path: Path) -> None:
    repo, old, _new = _overlay_remote(tmp_path)
    pin = OverlayPin(name="bentoo", url=f"file://{repo}", commit=old)

    dest = ensure_overlay(pin, cache_dir=tmp_path / "cache")

    assert dest == tmp_path / "cache" / "repos" / f"bentoo-{old[:12]}"
    assert (dest / "pkg").read_text(encoding="utf-8") == "old\n"  # not the branch tip
    assert (dest / "metadata" / "layout.conf").is_file()
    assert not (dest / ".git").exists()
    # cached: no fetch needed any more
    assert ensure_overlay(pin, cache_dir=tmp_path / "cache", download=False) == dest


def test_ensure_overlay_without_the_commit_and_no_download_is_a_tree_error(
    tmp_path: Path,
) -> None:
    pin = OverlayPin(name="bentoo", url="file:///nonexistent", commit="a" * 40)
    with pytest.raises(TreeError, match="--no-download"):
        ensure_overlay(pin, cache_dir=tmp_path, download=False)


def test_an_overlay_pin_must_be_a_full_commit_hash() -> None:
    with pytest.raises(ValueError):
        OverlayPin(name="bentoo", url="https://x", commit="199e434501")


def test_the_overlay_directory_is_readable_by_the_portage_user(tmp_path: Path) -> None:
    """Regression (2026-09-27): the overlay was the mkdtemp directory itself,
    mode 0700, and emerge -- reading repositories as the portage user --
    failed with PermissionError on profiles/thirdpartymirrors. A tree already
    cached with the bad mode is repaired on reuse."""
    repo, old, _new = _overlay_remote(tmp_path)
    pin = OverlayPin(name="bentoo", url=f"file://{repo}", commit=old)
    dest = ensure_overlay(pin, cache_dir=tmp_path / "cache")
    assert oct(dest.stat().st_mode & 0o777) == "0o755"
    dest.chmod(0o700)  # as the first real run left it
    assert (
        oct(
            ensure_overlay(pin, cache_dir=tmp_path / "cache", download=False).stat().st_mode & 0o777
        )
        == "0o755"
    )
