"""A real publish on the build host (story 029, task 5.4; R5.2, R5.3, R5.13, R6.1).

What the fake toolbox of tests/test_publish_pool.py cannot prove: real ``gpkg-sign`` in a
real toolbox, signing through the agent's extra socket bound as one file, then real
``emaint binhost --fix``, then ``verify`` with real ``gpg``.

- the key and its agent: a throwaway key without a passphrase, generated at run time in a
  temporary ``GNUPGHOME`` (never committed, Q7), its agent launched there;
- the build pool: two real gpkg files made IN the toolbox by ``quickpkg`` from two small
  packages installed in it, with their real ``Packages``, recorded as finals;
- the toolbox: the v3/systemd toolbox fork point of this checkout's pins, or the tarball
  named by ``SHIDASHI_TEST_TOOLBOX_TARBALL``.

HOST-GATED: skipped, with the missing piece named, without root (nspawn), without ``gpg``
or without a toolbox tarball. Run it as ``sudo uv run --frozen pytest
tests/test_publish_host.py -q`` on the build host.
"""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, toolbox, tree
from shidashi.recipe import TOOLBOX_STAGE
from shidashi.seed import load_pointer
from tests._pending import try_import

parse_refs: Any = try_import("shidashi.binhost", "parse_refs")
PoolRecord: Any = try_import("shidashi.binhost", "PoolRecord")
save_record: Any = try_import("shidashi.binhost", "save_record")
publish: Any = try_import("shidashi.publish_pool", "publish")
verify: Any = try_import("shidashi.publish_pool", "verify")

pytestmark = [
    pytest.mark.skipif(os.geteuid() != 0, reason="needs root: the toolbox runs under nspawn"),
    pytest.mark.skipif(
        shutil.which("gpg") is None or shutil.which("gpgconf") is None,
        reason="needs gpg and gpgconf on the host: the key and its agent are real",
    ),
]

TOOLBOX_ENV = "SHIDASHI_TEST_TOOLBOX_TARBALL"


def _toolbox_tarball() -> Path | None:
    """The tarball named by the environment, else this checkout's v3/systemd toolbox."""
    override = os.environ.get(TOOLBOX_ENV)
    if override:
        return Path(override) if Path(override).is_file() else None
    try:
        recipe = config.load_recipe("v3", TOOLBOX_STAGE, "systemd")
        tarball = toolbox.tarball_path(
            recipe,
            snapshot=load_pointer("systemd", seeds_dir=config.seeds_dir()).snapshot,
            pins=tree.load_pin_id(config.seeds_dir()),
            fork_points_dir=config.fork_points_dir(),
        )
    except Exception:  # no pins, no recipe: no toolbox to find
        return None
    return tarball if tarball.is_file() else None


@pytest.fixture(scope="module")
def toolbox_root() -> Iterator[Path]:
    tarball = _toolbox_tarball()
    if tarball is None:
        pytest.skip(
            f"needs a toolbox tarball: build it with `shidashi factory v3 toolbox systemd`, "
            f"or name one in {TOOLBOX_ENV}"
        )
    root = config.scratch_dir() / "toolbox" / "v3-systemd"
    with toolbox.using(tarball, root):
        yield root


@dataclass(frozen=True)
class Key:
    home: Path
    fpr: str
    pubkey: Path


def _gpg(home: Path, *args: str) -> bytes:
    return subprocess.run(
        ["gpg", "--homedir", str(home), "--batch", "--no-tty", "--pinentry-mode", "loopback"]
        + ["--passphrase", "", *args],
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Key]:
    """A signing key without a passphrase and its running agent, in a throwaway home that
    ``publish`` sees as the invoking user's (no SUDO_USER: this very environment)."""
    home = Path(tempfile.mkdtemp(prefix="gpgh-"))
    home.chmod(0o700)
    try:
        _gpg(home, "--quick-gen-key", "Shidashi Host Test <host@example.invalid>", "ed25519")
        listing = _gpg(home, "--with-colons", "--list-secret-keys").decode()
        fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
        pubkey = tmp_path / "signer.asc"
        pubkey.write_bytes(_gpg(home, "--armor", "--export", fpr))
        subprocess.run(["gpgconf", "--homedir", str(home), "--launch", "gpg-agent"], check=True)
        monkeypatch.setenv("GNUPGHOME", str(home))
        monkeypatch.delenv("SUDO_USER", raising=False)
        yield Key(home, fpr, pubkey)
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(home, ignore_errors=True)


def _two_small_packages(root: Path) -> list[str]:
    """``=cat/pf`` of the two smallest packages installed in the toolbox."""
    sized = []
    for size in (root / "var/db/pkg").glob("*/*/SIZE"):
        try:
            sized.append(
                (int(size.read_text().strip()), f"={size.parent.parent.name}/{size.parent.name}")
            )
        except ValueError:
            continue
    assert len(sized) >= 2, "the toolbox has fewer than two installed packages"
    return [atom for _size, atom in sorted(sized)[:2]]


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_a_real_publish_signs_through_the_agent_and_verifies(
    toolbox_root: Path, key: Key, tmp_path: Path
) -> None:
    # the build pool: two real gpkg files, made in the toolbox, recorded as finals
    pool = tmp_path / "binpkgs" / "v3" / "gen"
    pool.mkdir(parents=True)
    maker = toolbox.Toolbox(toolbox_root, rw={"pool": pool})
    maker.run(
        [
            "env",
            "PKGDIR=/work/pool",
            "PORTAGE_TMPDIR=/tmp",
            "BINPKG_FORMAT=gpkg",
            "FEATURES=binpkg-multi-instance -binpkg-signing",
            "quickpkg",
            "--include-config=y",
            *_two_small_packages(toolbox_root),
        ]
    )
    refs = parse_refs((pool / "Packages").read_text(encoding="utf-8"))
    assert len(refs) == 2, refs
    save_record(
        pool,
        PoolRecord(finals={"minimal": frozenset((r.cpv, r.build_id) for r in refs)}, transients={}),
    )
    pool_before = _tree(pool)

    dest = tmp_path / "publish" / "v3" / "gen"
    dest.parent.mkdir(parents=True)

    def toolbox_for(rw: Any, file_binds: Any) -> Any:
        return toolbox.Toolbox(toolbox_root, rw=dict(rw), file_binds=list(file_binds))

    result = publish(pool, dest, key=key.fpr, toolbox_for=toolbox_for)

    assert result.published == 2
    assert verify(dest, pubkey=key.pubkey) == []
    files = _tree(dest)
    assert set(files) == {r.path for r in refs} | {"Packages"}, sorted(files)
    for name, data in files.items():  # no key material in the pool
        assert b"PRIVATE KEY" not in data, name
    assert _tree(pool) == pool_before  # the build pool stays unsigned, byte for byte
    assert [p.name for p in dest.parent.iterdir()] == [dest.name]
