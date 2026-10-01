"""The pinned ::gentoo tree -- a dated, signed snapshot instead of the host's (D26).

Builds used to bind the host's ``/var/db/repos/gentoo`` into the container: a
tree that moves with every ``emerge --sync``, so two builds of one recipe could
resolve different versions, and a version published an hour earlier could land
in a release. The pin fixes both:

- **Reproducible input.** ``seeds/gentoo.toml`` names one daily snapshot of the
  mirror (``gentoo-YYYYMMDD.tar.xz``) and its SHA-512. Every factory, assemble
  and pretend run binds that tree, so the binpkgs and the ISO come from the
  same ebuilds.
- **Cooldown.** The pinned snapshot must be at least :data:`COOLDOWN_DAYS` old
  on the day of the build. A new version reaches a build only after a week in
  the tree, the time in which breakage and bad releases tend to be reverted.
  Portage has no such mechanism; a dated tree is one.

The mirror keeps about eight days of snapshots, so the verified tarball is cached
locally (like the stage3) and extracted once under ``cache/repos/``.
"""

import datetime
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from shidashi.seed import SeedError

#: A snapshot younger than this is refused (D26).
COOLDOWN_DAYS = 7

#: The Gentoo release keys, as installed by sec-keys/openpgp-keys-gentoo-release
#: -- the same file emerge-webrsync verifies snapshots with. Imported into a
#: throwaway keyring, so nothing depends on the caller's own gpg setup.
GENTOO_KEYRING = Path("/usr/share/openpgp-keys/gentoo-release.asc")

_STRICT = ConfigDict(frozen=True, extra="forbid")


class TreeError(SeedError):
    """The pinned tree is missing, unverifiable or too young to build from."""


class TreePin(BaseModel):
    """``seeds/gentoo.toml``: which daily snapshot of ::gentoo builds use."""

    model_config = _STRICT
    date: str
    base_url: str
    sha512: str

    @property
    def filename(self) -> str:
        return f"gentoo-{self.date}.tar.xz"

    @property
    def day(self) -> datetime.date:
        return datetime.datetime.strptime(self.date, "%Y%m%d").date()


def load_tree_pin(seeds_dir: Path) -> TreePin:
    """Read ``seeds_dir/gentoo.toml``; :class:`TreeError` when absent or malformed."""
    path = seeds_dir / "gentoo.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        pin = TreePin(**data)
        _ = pin.day
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as err:
        raise TreeError(f"cannot read the ::gentoo pin {path}: {err}") from err
    return pin


def check_cooldown(pin: TreePin, *, today: datetime.date, days: int = COOLDOWN_DAYS) -> None:
    """Refuse a snapshot younger than ``days`` on ``today``. Pure."""
    age = (today - pin.day).days
    if age < days:
        raise TreeError(
            f"::gentoo snapshot {pin.date} is {age} day(s) old; builds need one at "
            f"least {days} days old (D26 cooldown). Pin gentoo-"
            f"{(today - datetime.timedelta(days=days)):%Y%m%d} or older in seeds/gentoo.toml"
        )


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def signature_ok(status: str, returncode: int) -> bool:
    """Whether ``gpg --status-fd`` output proves a good, valid signature. Pure.

    Both lines are required: GOODSIG alone is emitted for a key that is expired
    or revoked too, and only VALIDSIG says the signature checks out.
    """
    lines = status.splitlines()
    return (
        returncode == 0
        and any(line.startswith("[GNUPG:] GOODSIG ") for line in lines)
        and any(line.startswith("[GNUPG:] VALIDSIG ") for line in lines)
    )


def verify_detached(data: Path, signature: Path, *, keyring: Path = GENTOO_KEYRING) -> None:
    """Verify ``signature`` over ``data`` against ``keyring`` in a throwaway home."""
    if shutil.which("gpg") is None:
        raise TreeError("gpg is not available on the host; cannot verify the ::gentoo snapshot")
    if not keyring.is_file():
        raise TreeError(f"{keyring} is missing (sec-keys/openpgp-keys-gentoo-release)")
    home = tempfile.mkdtemp(prefix="shidashi-gpg-")
    try:
        subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--quiet", "--import", str(keyring)],
            capture_output=True, text=True, check=False,
        )
        result = subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--status-fd", "1",
             "--verify", str(signature), str(data)],
            capture_output=True, text=True, check=False,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if not signature_ok(result.stdout, result.returncode):
        raise TreeError(f"GPG verification failed for {data.name}:\n{result.stderr.strip()}")


def _download(url: str, dest: Path) -> None:
    try:
        with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
    except (urllib.error.URLError, OSError) as err:
        raise TreeError(f"failed to download {url}: {err}") from err


def fetch_snapshot(
    pin: TreePin,
    *,
    cache_dir: Path,
    download: bool = True,
    verify: Callable[[Path, Path], None] = verify_detached,
) -> Path:
    """The verified snapshot tarball, downloaded once into ``cache_dir/trees``.

    A cached tarball is reused when its SHA-512 matches the pin. Otherwise it is
    downloaded with its detached ``.gpgsig`` into a temporary directory, checked
    against the pin's SHA-512 AND the Gentoo key, and only then moved in.
    """
    trees = cache_dir / "trees"
    cached = trees / pin.filename
    if cached.is_file() and _sha512(cached) == pin.sha512:
        return cached
    if not download:
        raise TreeError(
            f"--no-download: ::gentoo snapshot {pin.filename} is not in {trees}"
        )
    trees.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="shidashi-tree-", dir=trees))
    try:
        url = f"{pin.base_url.rstrip('/')}/{pin.filename}"
        tarball, sig = tmp / pin.filename, tmp / f"{pin.filename}.gpgsig"
        _download(url, tarball)
        _download(f"{url}.gpgsig", sig)
        actual = _sha512(tarball)
        if actual != pin.sha512:
            raise TreeError(
                f"sha512 mismatch for {pin.filename}: pinned {pin.sha512}, got {actual}"
            )
        verify(tarball, sig)
        tarball.replace(cached)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cached


def ensure_tree(
    pin: TreePin,
    *,
    cache_dir: Path,
    download: bool = True,
    verify: Callable[[Path, Path], None] = verify_detached,
) -> Path:
    """The extracted tree ``cache_dir/repos/gentoo-<date>``, extracting it once.

    The snapshot's single top directory (``gentoo-<date>/``) is extracted into a
    temporary sibling and renamed into place, so a half-extracted tree is never
    taken for a complete one.
    """
    dest = cache_dir / "repos" / f"gentoo-{pin.date}"
    if dest.is_dir():
        return dest
    tarball = fetch_snapshot(pin, cache_dir=cache_dir, download=download, verify=verify)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=dest.parent))
    try:
        with tarfile.open(tarball, "r:*") as tar:
            tar.extractall(tmp, filter="data")
        top = tmp / f"gentoo-{pin.date}"
        if not top.is_dir():
            raise TreeError(f"{pin.filename} has no gentoo-{pin.date}/ top directory")
        top.replace(dest)
    except (tarfile.TarError, OSError) as err:
        raise TreeError(f"failed to extract {pin.filename}: {err}") from err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def pinned_tree(
    *, seeds_dir: Path, cache_dir: Path, download: bool, today: datetime.date | None = None
) -> Path:
    """Load the pin, enforce the cooldown and return the extracted tree."""
    pin = load_tree_pin(seeds_dir)
    check_cooldown(pin, today=today or datetime.date.today())
    return ensure_tree(pin, cache_dir=cache_dir, download=download)


# --- pinned overlays (::bentoo): a git commit, fetched by hash ------------------


class OverlayPin(BaseModel):
    """One ``[name]`` table of ``seeds/overlays.toml``: a repo URL and a FULL commit."""

    model_config = _STRICT
    name: str
    url: str
    commit: str

    @field_validator("commit")
    @classmethod
    def _full_hash(cls, value: str) -> str:
        # git verifies every fetched object against the full hash; an abbreviated
        # one is ambiguous and would not be an integrity check at all
        if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"commit must be a full 40-hex hash, got {value!r}")
        return value


def load_overlay_pins(seeds_dir: Path) -> tuple[OverlayPin, ...]:
    """Read ``seeds_dir/overlays.toml`` (one table per overlay); ``()`` when absent."""
    path = seeds_dir / "overlays.toml"
    if not path.is_file():
        return ()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return tuple(OverlayPin(name=name, **table) for name, table in data.items())
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as err:
        raise TreeError(f"cannot read the overlay pins {path}: {err}") from err


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def ensure_overlay(pin: OverlayPin, *, cache_dir: Path, download: bool = True) -> Path:
    """The overlay at ``pin.commit``, extracted once to ``cache_dir/repos/<name>-<hash12>``.

    The commit is fetched by hash into a bare repository under ``cache_dir/git``
    (the host's clone is shallow and at its own tip) and exported with
    ``git archive``: no ``.git`` in the tree the container sees. Fetching by
    hash needs the server to allow it, which GitHub does.
    """
    dest = cache_dir / "repos" / f"{pin.name}-{pin.commit[:12]}"
    if dest.is_dir():
        # repairs a tree cached before the fix below -- only when needed: the
        # cache is root's, and an unprivileged reader (kits check) cannot chmod
        if dest.stat().st_mode & 0o777 != 0o755:
            dest.chmod(0o755)
        return dest
    gitdir = cache_dir / "git" / f"{pin.name}.git"
    if not gitdir.is_dir():
        gitdir.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--quiet", "--bare", str(gitdir)])
    have = _git(["--git-dir", str(gitdir), "cat-file", "-e", f"{pin.commit}^{{commit}}"])
    if have.returncode != 0:
        if not download:
            raise TreeError(
                f"--no-download: {pin.name} commit {pin.commit} is not in {gitdir}"
            )
        fetched = _git(["--git-dir", str(gitdir), "fetch", "--quiet", "--depth", "1",
                        pin.url, pin.commit])
        if fetched.returncode != 0:
            raise TreeError(
                f"cannot fetch {pin.name} {pin.commit} from {pin.url}: {fetched.stderr.strip()}"
            )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=dest.parent))
    try:
        archive = subprocess.run(
            ["git", "--git-dir", str(gitdir), "archive", "--format=tar", pin.commit],
            capture_output=True, check=False,
        )
        if archive.returncode != 0:
            raise TreeError(f"git archive {pin.commit} failed: {archive.stderr.decode().strip()}")
        untar = subprocess.run(
            ["tar", "--extract", "--directory", str(tmp)],
            input=archive.stdout, capture_output=True, check=False,
        )
        if untar.returncode != 0:
            raise TreeError(f"extracting {pin.name} failed: {untar.stderr.decode().strip()}")
        # mkdtemp made tmp 0700 and tmp BECOMES the tree: emerge reads
        # repositories as the portage user and was refused (2026-09-27)
        tmp.chmod(0o755)
        tmp.replace(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def pinned_repos(
    *, seeds_dir: Path, cache_dir: Path, download: bool, today: datetime.date | None = None
) -> dict[str, Path]:
    """Every pinned repository, by name: ``gentoo`` (cooldown enforced) and the overlays.

    Bound in place of the host's clones by factory, assemble and pretend, so
    the binpkgs and the ISO come from the same ebuilds on both repositories.
    """
    repos = {"gentoo": pinned_tree(
        seeds_dir=seeds_dir, cache_dir=cache_dir, download=download, today=today
    )}
    for pin in load_overlay_pins(seeds_dir):
        repos[pin.name] = ensure_overlay(pin, cache_dir=cache_dir, download=download)
    return repos

