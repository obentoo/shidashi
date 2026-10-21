"""The signed publish pool (story 029, task 5.1; R5.1-R5.8, R5.11-R5.13, R3.11).

Contract from design.md "shidashi/publish_pool.py": ``plan_publish(refs, record)`` -- the
recorded finals and nothing else; ``publish(build_pool, dest, *, key, toolbox_for)``
builds its own toolbox through ``toolbox_for(rw_mounts, file_binds)`` -- the agent's extra
socket bound as ONE FILE at ``<GNUPGHOME inside>/S.gpg-agent``, never its directory --
copies each planned gpkg to ``dest/<same PATH>`` (a copy: signing rewrites the file), signs
each copy with ``gpkg-sign`` in the toolbox (``BINPKG_GPG_SIGNING_KEY`` and
``BINPKG_GPG_SIGNING_GPG_HOME`` in its environment, GOTCHA gpkg-sign), runs ``emaint
binhost --fix`` over the new pool, builds everything in a sibling temporary directory and
renames it into place. Refusals, all before any copy: no build-pool ``Packages``, nothing
recorded final, no extra socket, an empty ``gpg --export <key>``. Under sudo, ``gpgconf``
and ``gpg --export`` run as ``SUDO_USER`` with their home. A failure (a copy's
``OSError`` included) leaves the previous pool untouched; the build pool is never written.

The toolbox is tests/test_binhost_cli_gc.py's fake (its ``gpkg-sign`` appends the key to
the file IN PLACE, so a hardlinked copy would show in the build pool). ``gpgconf`` and
``gpg`` on the host are fakes too; the extra socket is a real Unix socket. Real signing
with a real ``gpkg-sign`` needs root and a toolbox tarball: not exercised here (see
tests/test_publish_verify.py for real ``gpg`` signatures).
"""

import contextlib
import getpass
import json
import os
import pwd
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import
from tests.test_binhost_cli_gc import FakeTools, install_fake_toolbox, path_of
from tests.test_binhost_cli_show import FFMPEG, Lab, make_lab
from tests.test_binhost_refs import entry

BinhostError: Any = try_import("shidashi.binhost", "BinhostError")
PoolRecord: Any = try_import("shidashi.binhost", "PoolRecord")
parse_refs: Any = try_import("shidashi.binhost", "parse_refs")
plan_publish: Any = try_import("shidashi.publish_pool", "plan_publish")
publish: Any = try_import("shidashi.publish_pool", "publish")

FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
CURL = "net-misc/curl-8.16.0"

FINAL_FFMPEG = entry(FFMPEG, 3, use="sdl x264", build_time=300)  # minimal's final, kde's cut
CUT_FFMPEG = entry(FFMPEG, 2, use="x264", build_time=200)  # kde's transient only
FINAL_CURL = entry(CURL, 1, build_time=100)
UNRECORDED = entry("app-misc/plain-1-r1", 1, build_time=50)
OLD = entry("dev-libs/old-1.0", 1, build_time=10)
ENTRIES = (FINAL_FFMPEG, CUT_FFMPEG, FINAL_CURL, UNRECORDED, OLD)
PUBLISHED = {path_of(FINAL_FFMPEG), path_of(FINAL_CURL)}

_FAKE_GPG = r"""
import json, os, sys
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
if name == "gpgconf":
    if "--list-dirs" in args:
        if "agent-extra-socket" in args:
            print(os.environ.get("FAKE_EXTRA_SOCKET", ""))
        elif "agent-socket" in args:
            print(os.environ.get("FAKE_EXTRA_SOCKET", "") + ".std")
        else:
            where = os.environ.get("FAKE_EXTRA_SOCKET", "").replace(":", "%3a")
            print("agent-extra-socket:" + where)
    sys.exit(0)
if "--export" in args or "--export-options" in args:
    if os.environ.get("FAKE_GPG_EXPORT_EMPTY"):
        sys.exit(0)  # gpg exports nothing, exit 0, for a key it does not have
    key = (
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nfake public key\n"
        "-----END PGP PUBLIC KEY BLOCK-----\n"
    )
    if "--output" in args:
        Path(args[args.index("--output") + 1]).write_text(key)
    elif "-o" in args:
        Path(args[args.index("-o") + 1]).write_text(key)
    else:
        sys.stdout.write(key)
    sys.exit(0)
sys.exit(0)
"""


@contextlib.contextmanager
def extra_socket() -> Iterator[Path]:
    """A real Unix socket standing for the agent's extra socket (a short path: AF_UNIX)."""
    home = Path(tempfile.mkdtemp(prefix="gx-", dir="/tmp"))
    path = home / "S.gpg-agent.extra"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    try:
        yield path
    finally:
        sock.close()
        shutil.rmtree(home, ignore_errors=True)


def publish_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: Path, *entries: str) -> Lab:
    """The lab, a build pool of ``entries``, the fake toolbox, and fake host ``gpg`` and
    ``gpgconf`` answering ``agent`` as the extra socket. The record is the caller's."""
    lab = make_lab(tmp_path, monkeypatch)
    lab.pool(*entries)
    install_fake_toolbox(tmp_path, monkeypatch)
    FakeTools.env["FAKE_SOURCE_INDEX"] = str(lab.pkgdir() / "Packages")
    gpgbin = tmp_path / "host-gpg-bin"
    gpgbin.mkdir()
    tool = gpgbin / "fake-gpg"
    tool.write_text(f"#!{sys.executable}\n{_FAKE_GPG}", encoding="utf-8")
    tool.chmod(0o755)
    for name in ("gpg", "gpg2", "gpgconf", "sudo", "runuser"):
        (gpgbin / name).symlink_to(tool)
    monkeypatch.setenv("PATH", f"{gpgbin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GPG_LOG", str(tmp_path / "gpg-calls.jsonl"))
    monkeypatch.setenv("FAKE_EXTRA_SOCKET", str(agent))
    return lab


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lab]:
    with extra_socket() as agent:
        lab = publish_lab(tmp_path, monkeypatch, agent, *ENTRIES)
        lab.record(
            finals={"minimal": [(FFMPEG, 3)], "kde": [(CURL, 1)]},
            transients={"kde": [(FFMPEG, 3), (FFMPEG, 2)]},
        )
        yield lab


def _dest(lab: Lab) -> Path:
    return lab.root / "publish" / "v3" / "20260823T153057Z"


def toolbox_for(lab: Lab) -> Any:
    """What the CLI hands ``publish``: builds the (fake) toolbox with the mounts and file
    binds ``publish`` asks for; each one is in ``FakeTools.made``."""

    def build(rw: Any, file_binds: Any) -> FakeTools:
        return FakeTools(lab.root / "toolbox", rw=rw, file_binds=file_binds)

    return build


def _publish(lab: Lab) -> Any:
    dest = _dest(lab)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return publish(lab.pkgdir(), dest, key=FPR, toolbox_for=toolbox_for(lab))


def gpg_calls(lab: Lab) -> list[dict[str, Any]]:
    log = lab.root / "gpg-calls.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (dotfiles included), by relative path, with its bytes."""
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- plan_publish (pure) ---------------------------------------------------------------------


def test_the_plan_is_the_recorded_finals_and_nothing_else() -> None:
    """R5.1. Hostile halves: build 3 is also kde's transient (still published); build 2
    shares its CPV (not published); an unrecorded instance is never published (fail
    closed)."""
    refs = parse_refs("\n".join(["ARCH: amd64\n", *ENTRIES]))
    record = PoolRecord(
        finals={"minimal": frozenset({(FFMPEG, 3)}), "kde": frozenset({(CURL, 1)})},
        transients={"kde": frozenset({(FFMPEG, 3), (FFMPEG, 2)})},
    )
    assert {r.path for r in plan_publish(refs, record)} == PUBLISHED


def test_with_no_record_nothing_is_planned() -> None:
    refs = parse_refs("\n".join(["ARCH: amd64\n", *ENTRIES]))
    assert len(refs) == len(ENTRIES)
    assert plan_publish(refs, PoolRecord(finals={}, transients={})) == []
    only_cuts = PoolRecord(
        finals={"kde": frozenset()}, transients={"kde": frozenset({(FFMPEG, 2)})}
    )
    assert plan_publish(refs, only_cuts) == []
    record = PoolRecord(finals={"kde": frozenset({(CURL, 1)})}, transients={})
    assert {r.path for r in plan_publish(refs, record)} == {path_of(FINAL_CURL)}


# --- publish ---------------------------------------------------------------------------------


def test_publish_copies_signs_and_indexes_exactly_the_finals(lab: Lab) -> None:
    """R5.1-R5.3: same relative PATH, each copy signed with FPR, one index over the new
    pool; nothing else in it -- no key material, no cut build, no unrecorded instance."""
    result = _publish(lab)
    dest = _dest(lab)
    files = _tree(dest)
    assert set(files) == PUBLISHED | {"Packages"}, sorted(files)
    for rel in PUBLISHED:
        original = (lab.pkgdir() / rel).read_bytes()
        assert files[rel].startswith(original) and files[rel] != original, rel  # signed copy
        assert FPR.encode() in files[rel]
    signs = FakeTools.calls("gpkg-sign")
    assert len(signs) == len(PUBLISHED), signs
    assert {s["env"].get("BINPKG_GPG_SIGNING_KEY") for s in signs} == {FPR}
    signed = {Path(a).name for s in signs for a in s["argv"] if a.endswith(".gpkg.tar")}
    assert signed == {Path(p).name for p in PUBLISHED}
    indexes = FakeTools.calls("emaint")
    assert len(indexes) == 1, indexes
    assert Path(indexes[0]["pkgdir"]).resolve() != lab.pkgdir().resolve()
    assert set(indexes[0]["files"]) == PUBLISHED  # indexed after every copy was there
    log = FakeTools.calls()
    assert log.index(indexes[0]) > max(log.index(s) for s in signs)  # after every signature
    index_text = (dest / "Packages").read_text(encoding="utf-8")
    assert all(rel in index_text for rel in PUBLISHED)
    assert path_of(CUT_FFMPEG) not in index_text and path_of(UNRECORDED) not in index_text
    assert (result.dest, result.published, result.skipped_unrecorded) == (dest, 2, 2)


def test_the_index_is_made_without_portage_verifying_the_signatures_it_just_made(
    lab: Lab,
) -> None:
    """R5.5, found by the real run on bentoo-lab (task 5.4): over signed gpkgs a bare
    ``emaint binhost --fix`` verifies each signature, and in the read-only toolbox gpg cannot
    even create its random_seed, so every binpkg is rejected and the index lists none
    (``PACKAGES: 0``). The fake toolbox cannot reproduce that, so pin the cause: the index
    step must run with ``binpkg-ignore-signature`` (``verify`` is what checks the signatures)."""
    _publish(lab)
    indexes = FakeTools.calls("emaint")
    assert len(indexes) == 1, indexes
    assert "binpkg-ignore-signature" in (indexes[0].get("features") or "").split(), indexes[0]
    assert FakeTools.calls("gpkg-sign"), "nothing was signed"
    # signing itself must still verify nothing and ignore nothing
    assert all(
        "binpkg-ignore-signature" not in (s.get("features") or "")
        for s in FakeTools.calls("gpkg-sign")
    )


def test_publish_never_writes_the_build_pool(lab: Lab) -> None:
    """R5.6. The fake gpkg-sign rewrites its file in place: signing in the build pool, or
    a hardlink instead of a copy, would change these bytes."""
    before = _tree(lab.pkgdir())
    _publish(lab)
    assert FakeTools.calls("gpkg-sign"), "nothing was signed"
    assert _tree(lab.pkgdir()) == before


def test_a_successful_publish_replaces_the_previous_pool_whole(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.4: built beside it, renamed into place -- the previous pool's files do not
    survive (replaced, not merged) and no temporary directory is left."""
    dest = _dest(lab)
    (dest / "old" / "stale").mkdir(parents=True)
    (dest / "old" / "stale" / "stale-1-1.gpkg.tar").write_bytes(b"from the last publish\n")
    renames: list[tuple[Path, Path]] = []
    for name in ("rename", "replace"):
        real = getattr(os, name)

        def spy(src: Any, dst: Any, *a: Any, _real: Any = real, **kw: Any) -> Any:
            renames.append((Path(src), Path(dst)))
            return _real(src, dst, *a, **kw)

        monkeypatch.setattr(os, name, spy)
    _publish(lab)
    assert set(_tree(dest)) == PUBLISHED | {"Packages"}
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]
    into = [src for src, dst in renames if dst == dest]
    assert into and into[-1].parent == dest.parent and into[-1] != dest, renames


@pytest.mark.parametrize("failing", ["gpkg-sign:curl", "emaint"])
def test_a_failed_step_leaves_the_previous_pool_and_reports_the_output(
    lab: Lab, failing: str
) -> None:
    """R5.5: the previous pool byte-identical, the build pool too, the command's tail in
    the error."""
    dest = _dest(lab)
    (dest / "net-misc" / "curl").mkdir(parents=True)
    (dest / "net-misc" / "curl" / "curl-8.15.0-1.gpkg.tar").write_bytes(b"last week's\n")
    (dest / "Packages").write_text("last week's index\n", encoding="utf-8")
    previous, pool = _tree(dest), _tree(lab.pkgdir())
    FakeTools.env["FAKE_FAIL"] = failing
    with pytest.raises(BinhostError) as err:
        _publish(lab)
    assert "fake" in str(err.value) and "failure" in str(err.value)
    assert _tree(dest) == previous
    assert _tree(lab.pkgdir()) == pool


@pytest.mark.parametrize("record", ["transients-only", "absent"])
def test_with_no_recorded_final_publish_refuses_before_copying(lab: Lab, record: str) -> None:
    """R5.7: says a factory run records the finals; nothing is created."""
    if record == "absent":
        (lab.pkgdir() / ".shidashi-pool.json").unlink()
    else:
        lab.record(finals={}, transients={"kde": [(FFMPEG, 2)]})
    with pytest.raises(BinhostError) as err:
        _publish(lab)
    assert "factory" in str(err.value)
    assert list(_dest(lab).parent.iterdir()) == []
    assert FakeTools.calls("gpkg-sign") == []


@pytest.mark.parametrize("answer", ["empty", "missing"])
def test_without_the_agent_extra_socket_publish_refuses_before_copying(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """R5.8: gpgconf names no extra socket, or one that does not exist."""
    monkeypatch.setenv(
        "FAKE_EXTRA_SOCKET", "" if answer == "empty" else str(lab.root / "no-such-socket")
    )
    with pytest.raises(BinhostError) as err:
        _publish(lab)
    assert "socket" in str(err.value) or "agent" in str(err.value)
    assert list(_dest(lab).parent.iterdir()) == []
    assert FakeTools.calls("gpkg-sign") == []


def test_a_final_whose_index_path_leaves_the_pool_is_never_copied(lab: Lab) -> None:
    """R3.11 at publish: an index is input. A recorded final with a climbing PATH is never
    read from outside the pool nor written outside the publish pool: the publish either
    refuses, naming it, or goes on without it."""
    outside = lab.root / "outside" / "evil-1.gpkg.tar"
    outside.parent.mkdir()
    outside.write_bytes(b"not a binpkg of the pool\n")
    rel = os.path.relpath(outside, lab.pkgdir())
    lab.pool(*ENTRIES, entry("dev-libs/evil-1", 1, path=rel))
    lab.record(
        finals={"minimal": [(FFMPEG, 3), ("dev-libs/evil-1", 1)], "kde": [(CURL, 1)]},
        transients={},
    )
    raised: Exception | None = None
    try:
        _publish(lab)
    except Exception as err:
        raised = err
    assert outside.read_bytes() == b"not a binpkg of the pool\n"
    assert [p for p in lab.root.rglob("evil-1.gpkg.tar") if p != outside] == []
    signed = [a for s in FakeTools.calls("gpkg-sign") for a in s["argv"]]
    assert not any("evil-1" in a for a in signed), signed
    if raised is not None:
        assert isinstance(raised, BinhostError), raised
        assert "evil-1" in str(raised) or rel in str(raised)
    else:
        assert set(_tree(_dest(lab))) == PUBLISHED | {"Packages"}


# --- the agent socket, the key, sudo, the copy (R5.2, R5.11-R5.13) --------------------------


def test_only_the_extra_socket_file_is_bound_at_the_throwaway_gnupghome(lab: Lab) -> None:
    """GOTCHA gpg-agent in a container. Hostile half: the socket's DIRECTORY also holds
    the full-access standard socket -- it must not be mounted, only the one file, at
    ``S.gpg-agent`` of the GNUPGHOME gpkg-sign is told to use, which is in neither pool."""
    socket_path = Path(os.environ["FAKE_EXTRA_SOCKET"])
    _publish(lab)
    signs = FakeTools.calls("gpkg-sign")
    assert signs, "nothing was signed"
    homes = {s["env"].get("BINPKG_GPG_SIGNING_GPG_HOME") for s in signs}
    assert len(homes) == 1 and None not in homes, homes
    (home,) = homes
    used = [m for m in FakeTools.made if m.file_binds]
    assert len(used) >= 1, [m.file_binds for m in FakeTools.made]
    for tools in used:
        assert len(tools.file_binds) == 1, tools.file_binds
        ((src, dst),) = tools.file_binds
        assert src.resolve() == socket_path.resolve()
        assert dst.name == "S.gpg-agent"
        assert Path(tools._host(str(dst.parent))).resolve() == Path(str(home)).resolve()
        for mounted in tools.mounts.values():
            assert socket_path.parent.resolve() not in (mounted, *mounted.parents), mounted
    assert lab.pkgdir().resolve() not in Path(str(home)).resolve().parents
    assert _dest(lab).resolve() not in Path(str(home)).resolve().parents


def test_without_a_build_pool_index_publish_refuses_naming_the_pkgdir(lab: Lab) -> None:
    """R5.12, before any copy."""
    (lab.pkgdir() / "Packages").unlink()
    with pytest.raises(BinhostError) as err:
        _publish(lab)
    flat = "".join(str(err.value).split())
    assert "".join(str(lab.pkgdir()).split()) in flat and "shidashifactory" in flat
    assert list(_dest(lab).parent.iterdir()) == []
    assert FakeTools.calls("gpkg-sign") == []


def test_a_key_the_keyring_does_not_have_is_refused_before_copying(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.11: ``gpg --export`` of an unknown key prints nothing and exits 0."""
    monkeypatch.setenv("FAKE_GPG_EXPORT_EMPTY", "1")
    with pytest.raises(BinhostError) as err:
        _publish(lab)
    assert FPR in str(err.value)
    assert list(_dest(lab).parent.iterdir()) == []
    assert FakeTools.calls("gpkg-sign") == []


def test_under_sudo_the_agent_and_the_keyring_are_the_invoking_users(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.13: euid 0 with SUDO_USER: ``gpgconf`` and ``gpg --export`` run as that user,
    with their home -- never root's (here HOME points elsewhere, as root's would)."""
    user = getpass.getuser()
    user_home = pwd.getpwnam(user).pw_dir
    monkeypatch.setenv("SUDO_USER", user)
    monkeypatch.setenv("HOME", str(lab.root / "root-home"))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    _publish(lab)
    calls = [c for c in gpg_calls(lab) if c["tool"] not in ("sudo", "runuser")]
    agent = [c for c in calls if c["tool"] == "gpgconf" and "agent-extra-socket" in c["argv"]]
    export = [c for c in calls if c["tool"] in ("gpg", "gpg2") and "--export" in c["argv"]]
    assert agent and export, calls
    assert {c["home"] for c in agent + export} == {user_home}, agent + export


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_copy_that_fails_is_an_error_naming_the_file_and_leaves_the_previous_pool(
    lab: Lab,
) -> None:
    """R5.5: an ``OSError`` of the copy (here EACCES; ENOSPC alike) is a ``BinhostError``
    naming the file, never a traceback, and the previous pool stays."""
    dest = _dest(lab)
    (dest / "net-misc").mkdir(parents=True)
    (dest / "Packages").write_text("last week's index\n", encoding="utf-8")
    previous = _tree(dest)
    unreadable = lab.pkgdir() / path_of(FINAL_CURL)
    unreadable.chmod(0o000)
    try:
        with pytest.raises(BinhostError) as err:
            _publish(lab)
    finally:
        unreadable.chmod(0o644)
    assert Path(path_of(FINAL_CURL)).name in str(err.value)
    assert _tree(dest) == previous
