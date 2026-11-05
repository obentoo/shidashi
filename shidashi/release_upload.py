"""``shidashi release upload``: what is copied to the bucket, the public index, and which
releases the bucket keeps (story 033 R3-R5). The planners here are pure; the rclone calls
that act on their plans live beside them."""

import contextlib
import datetime
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shidashi import audit
from shidashi.invoker import InvokingUser, invoking_user
from shidashi.release_sign import RELEASE_KEY_FILE, SUMS_FILE

#: The run bundle carries internal paths and build logs: it never leaves the host (R3.3).
_BUNDLE_SUFFIX = ".build.tar.zst"
#: Copied last: its presence in the bucket means the whole release is there (R3.2).
SUMS_SIGNATURE_FILE = f"{SUMS_FILE}.asc"
#: The public index, relative to the bucket root: R2 does not list objects to the public,
#: so this is the only way a reader finds a release.
INDEX_OBJECT = "releases/index.json"
#: The format version of index.json and latest.json (Q12): any other is refused.
INDEX_SCHEMA = 1
#: The bucket key of a release is its directory name. ``[0-9]``, not ``\d``, which also
#: matches non-ASCII digits: the date ends up in a ``purge`` path.
_RELEASE_DATE_RE = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}")
#: The entry fields that name an object of the entry's own release.
_ENTRY_PATH_FIELDS = ("sha256sums", "signature", "pubkey")


class UploadError(Exception):
    """An upload step refused or failed; the message names the file, object or step,
    never a credential (R3.5)."""


@dataclass(frozen=True)
class UploadPlan:
    """``data`` is copied first, ``seal`` after it: a reader who finds ``SHA256SUMS.asc``
    (the last object) finds every file it vouches for (R3.2)."""

    data: tuple[str, ...]
    seal: tuple[str, ...]


@dataclass(frozen=True)
class RetentionPlan:
    """The index entries ``kept``, to ``drop`` (purged from the bucket after ``index`` is
    written), and ``refused`` (beyond ``--keep`` but kept: no host copy and no ``--yes``);
    the three never overlap. ``index`` is the index without the ``drop`` entries."""

    kept: tuple[dict[str, Any], ...]
    drop: tuple[dict[str, Any], ...]
    refused: tuple[dict[str, Any], ...]
    index: dict[str, Any]


def _is_public_name(name: str) -> bool:
    # A name with "/" would leave releases/<date>/ in the bucket; a hidden one is
    # bookkeeping (locks, the signing stage), never a published file (R3.3).
    return "/" not in name and not name.startswith(".")


def upload_set(sums: Mapping[str, str], present: Collection[str]) -> UploadPlan:
    """The objects to copy for one release: ``sums`` is ``SHA256SUMS`` as name -> digest,
    ``present`` the names in the release directory. Raises :class:`UploadError` naming a
    listed name that is unsafe or missing, or a seal file that is missing. Pure."""
    on_disk = set(present)
    for name in sums:
        if not _is_public_name(name):
            raise UploadError(f"SHA256SUMS lists {name!r}: a hidden name or one with '/'")
        if name not in on_disk:
            # An advertised file the bucket lacks would fail every reader's check (R3.1).
            raise UploadError(f"SHA256SUMS lists {name!r}, which is not in the release")
    for name in (RELEASE_KEY_FILE, SUMS_FILE, SUMS_SIGNATURE_FILE):
        if name not in on_disk:
            raise UploadError(f"{name!r} is not in the release: it is not signed")
    data = sorted(
        (n for n in sums if not n.endswith((_BUNDLE_SUFFIX, ".asc")) and n != SUMS_FILE),
        # ISOs are the largest objects: last among the data, so a slow or failed
        # transfer is noticed after the small files are already up.
        key=lambda n: (n.endswith(".iso"), n),
    )
    signatures = sorted(
        n
        for n in on_disk
        if n.endswith(".asc")
        and _is_public_name(n)
        and n not in (RELEASE_KEY_FILE, SUMS_SIGNATURE_FILE)
    )
    # The key is listed in SHA256SUMS yet ends in .asc: it travels with the seal, so a
    # reader who finds a signature also finds the key that checks it.
    seal = (RELEASE_KEY_FILE, SUMS_FILE, *signatures, SUMS_SIGNATURE_FILE)
    return UploadPlan(data=tuple(data), seal=seal)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _refuse_index(reason: str) -> UploadError:
    return UploadError(f"{INDEX_OBJECT}: {reason}; left unchanged")


def _check_entry(item: object, position: int) -> None:
    if not isinstance(item, dict):
        raise _refuse_index(f"release {position} is not an object")
    date = item.get("date")
    if not isinstance(date, str) or not _RELEASE_DATE_RE.fullmatch(date):
        raise _refuse_index(f"release {position} has no YYYY.MM.DD date")
    isos = item.get("isos")
    if not isinstance(isos, list) or not all(isinstance(i, str) for i in isos):
        raise _refuse_index(f"release {date}: isos is not a list of names")
    for field in _ENTRY_PATH_FIELDS:
        path = item.get(field)
        prefix, _, name = path.rpartition("/") if isinstance(path, str) else ("", "", "")
        # One plain name under its own date: an entry must not point at another release.
        if prefix != f"releases/{date}" or name in ("", ".", ".."):
            raise _refuse_index(f"release {date}: {field} is not under releases/{date}/")


def parse_index(raw: bytes | None) -> dict[str, Any]:
    """The bucket's ``index.json`` as ``rclone cat`` read it; ``None`` (no index yet) is an
    empty one. Raises :class:`UploadError` naming it unless it is UTF-8 JSON of schema 1
    whose entries each carry a release date and paths under that date (R4.4). Pure."""
    if raw is None:
        return {"schema": INDEX_SCHEMA, "releases": []}
    try:
        # Decoded here: json.loads(bytes) would also accept UTF-16 and UTF-32.
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as err:  # RecursionError: nesting too deep
        raise _refuse_index(f"not UTF-8 JSON ({err})") from err
    if not isinstance(doc, dict):
        raise _refuse_index("not a JSON object")
    # type(), not ==: True == 1 in Python, yet it is not the integer the format names.
    if type(doc.get("schema")) is not int or doc["schema"] != INDEX_SCHEMA:
        raise _refuse_index(f"schema is {doc.get('schema')!r}, not {INDEX_SCHEMA}")
    releases = doc.get("releases")
    if not isinstance(releases, list):
        raise _refuse_index("releases is not a list")
    for position, item in enumerate(releases, start=1):
        _check_entry(item, position)
    return doc


def _entry_date(entry: Mapping[str, Any]) -> str:
    return str(entry["date"])


def merge_index(
    current: Mapping[str, Any],
    entry: Mapping[str, Any],
    clock: Callable[[], datetime.datetime] = _utc_now,
) -> dict[str, Any]:
    """``current`` with ``entry`` added, replacing every entry of its date (R4.2), all
    newest date first (R4.1), ``updated`` stamped from ``clock``. ``current`` is left
    unchanged. Pure but for the default clock."""
    others = [dict(e) for e in current.get("releases", []) if e["date"] != entry["date"]]
    releases = sorted([*others, dict(entry)], key=_entry_date, reverse=True)
    stamp = clock().astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"schema": INDEX_SCHEMA, "updated": stamp, "releases": releases}


def latest(index: Mapping[str, Any]) -> dict[str, Any]:
    """``latest.json``: the entry with the newest date, whatever the entry order (R4.3).
    Raises :class:`UploadError` for an index with no release. Pure."""
    releases = index.get("releases", [])
    if not releases:
        raise UploadError(f"{INDEX_OBJECT} lists no release: no latest.json to write")
    return {"schema": INDEX_SCHEMA, "release": max(releases, key=_entry_date)}


def retention(
    index: Mapping[str, Any],
    keep: int,
    current: str,
    host_dates: Collection[str],
    yes: bool,
) -> RetentionPlan:
    """Which releases the bucket keeps: the ``keep`` newest dates (R5.1) and always
    ``current``, the release just uploaded (R5.2). One beyond them is dropped only when its
    date is in ``host_dates`` or ``yes`` (R5.3): a bucket delete is irreversible. Raises
    ``ValueError`` for ``keep < 1``. Pure."""
    if keep < 1:
        raise ValueError(f"--keep must be at least 1, not {keep}")
    newest_first = sorted(index.get("releases", []), key=_entry_date, reverse=True)
    kept: list[dict[str, Any]] = []
    drop: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for rank, item in enumerate(newest_first):
        date = _entry_date(item)
        # An older release uploaded again stays beside the newest `keep`: the bucket holds
        # keep + 1 until the next upload (decision 2026-10-24).
        if rank < keep or date == current:
            kept.append(item)
        elif date in host_dates or yes:
            drop.append(item)
        else:
            refused.append(item)
    remaining = sorted([*kept, *refused], key=_entry_date, reverse=True)
    return RetentionPlan(
        kept=tuple(kept),
        drop=tuple(drop),
        refused=tuple(refused),
        index={**index, "releases": remaining},
    )


# --- rclone: the one place that runs it (R2.2, R2.4, R3.4, R3.5) ---------------------------

#: The rclone config holds the R2 secret: readable by its owner only.
_CONFIG_MODE = 0o600
#: ``cat``, ``moveto`` and ``purge`` touch one small object or one prefix.
_SHORT_TIMEOUT_S = 120
#: A bucket-scoped R2 token gets 403 on rclone's implicit bucket check (design.md gotcha).
_NO_BUCKET_CHECK = "--s3-no-check-bucket"
#: ``--timeout`` is rclone's idle timeout, not a wall clock: a multi-GiB ISO may take hours.
_COPY_FLAGS = ("--checksum", _NO_BUCKET_CHECK, "--retries", "3", "--timeout", "30m")
#: ``cat`` of a missing object: on S3/R2 rclone exits 0 with no output, so
#: ``--error-on-no-transfer`` turns "nothing read" into exit 9 (measured with ``rclone serve
#: s3``, 2026-11-04, tech review 3.x); the local backend (and FakeRclone) answers exit 3
#: "not found". A 0-byte index object also exits 9: it is no index. The host proof records R2.
_CAT_NO_TRANSFER_EXIT = 9
_CAT_LOCAL_MISSING_EXIT = 3
#: Never prompt: a prompt for an encrypted config's password goes to the captured stderr
#: and blocks forever (measured, tech review 3.x).
_NO_PROMPT = "--ask-password=false"

#: Credentials as rclone or the S3 SDK print them. A bare 64-hex string is never redacted:
#: an R2 secret looks like a SHA-256, and an error must still name the digest (R3.4).
_REDACTIONS = (
    # access_key_id = X, AWS_SECRET_ACCESS_KEY=X, "secret_access_key": "X", session tokens
    re.compile(
        r"(?i)((?:aws_)?(?:access_key_id|secret_access_key|session_token)\"?\s*[:=]\s*\"?)"
        r"[^\s\",;)]+"
    ),
    # the whole SigV4 header value: Credential=<key id>/..., Signature=<hmac>
    re.compile(r"(?i)(authorization\s*:\s*)[^\r\n]+"),
    # a presigned URL's query parameters
    re.compile(r"(?i)(x-amz-(?:credential|signature|security-token)=)[^&\s:]+"),
)


def redact(text: str) -> str:
    """``text`` with every credential shape in :data:`_REDACTIONS` replaced by ``***``;
    names, paths and digests stay (R3.5). Pure."""
    for pattern in _REDACTIONS:
        text = pattern.sub(r"\1***", text)
    return text


def _maintainer() -> InvokingUser | None:
    try:
        return invoking_user()
    except LookupError as err:
        raise UploadError(str(err)) from err


def rclone_config() -> Path:
    """The rclone config to use: ``$RCLONE_CONFIG``, else the invoking user's (under
    ``sudo``) or ``$HOME``'s ``.config/rclone/rclone.conf``. It must be a regular file,
    not a symlink, owned by that user, mode 0600 (R2.2, R2.4). Raises
    :class:`UploadError` naming the path -- never the content."""
    user = _maintainer()
    named = os.environ.get("RCLONE_CONFIG")
    if named:
        path = Path(named)
    else:
        home = user.home if user is not None else Path(os.environ.get("HOME", "~")).expanduser()
        path = home / ".config" / "rclone" / "rclone.conf"
    owner = user.uid if user is not None else os.getuid()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as err:
        raise UploadError(f"cannot open the rclone config {path}: {err.strerror}") from None
    try:
        info = os.fstat(fd)
    except OSError as err:
        raise UploadError(f"cannot read the rclone config {path}: {err.strerror}") from None
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner
        or stat.S_IMODE(info.st_mode) != _CONFIG_MODE
    ):
        raise UploadError(
            f"the rclone config {path} must be a regular file owned by uid {owner} with "
            f"mode 0600 (found mode {stat.S_IMODE(info.st_mode):04o}, owner uid {info.st_uid})"
        )
    return path


def rclone_binary() -> str:
    """The absolute path of ``rclone`` on PATH; :class:`UploadError` when absent (R2.1)."""
    found = shutil.which("rclone")
    if found is None:
        raise UploadError("rclone is not installed (not found on PATH)")
    return found


class RcloneRunner:
    """Every rclone call of ``release upload``: an argv list, never a shell; an env of only
    PATH, HOME and ``RCLONE_CONFIG``, so credentials in the caller's env (which rclone
    prefers over the file) never reach it; under ``sudo``, run as the maintainer through
    ``runuser``. Paths are relative to ``remote`` (``r2:bucket``). A failure is an
    :class:`UploadError` naming the step, the path and rclone's last stderr line, redacted."""

    def __init__(self, *, remote: str, config: Path, trail: audit.Recorder | None = None) -> None:
        self._remote = remote
        self._config = Path(config)
        self._trail = trail
        self._binary = rclone_binary()
        user = _maintainer()
        home = str(user.home) if user is not None else os.environ.get("HOME", "/")
        self._env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": home,
            "RCLONE_CONFIG": str(self._config),
        }
        # The pattern of gpgsign.as_maintainer, built here so its errors are UploadError.
        wrap = ["runuser", "-u", user.name, "--", "env"] if user is not None else []
        self._prefix = [*wrap, f"HOME={home}", f"RCLONE_CONFIG={self._config}"] if wrap else []

    def _at(self, path: str) -> str:
        sep = "" if self._remote.endswith((":", "/")) else "/"
        return f"{self._remote}{sep}{path}"

    def _run(
        self, step: str, path: str, args: list[str], timeout: float | None
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [*self._prefix, self._binary, step, _NO_PROMPT, *args]
        started = time.monotonic()
        try:
            # A session of its own: under sudo the child is runuser, and killing only it
            # would leave rclone finishing a moveto/purge already reported as failed.
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                start_new_session=True,
            )
        except OSError as err:
            self._log(step, path, started, None)
            raise UploadError(f"rclone {step} {path}: cannot run rclone: {err.strerror}") from None
        try:
            out, err_out = child.communicate(timeout=timeout)
        except BaseException as stop:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            self._log(step, path, started, None)
            if isinstance(stop, subprocess.TimeoutExpired):
                raise UploadError(f"rclone {step} {path}: timed out after {timeout} s") from None
            raise
        self._log(step, path, started, child.returncode)
        return subprocess.CompletedProcess(argv, child.returncode, out, err_out)

    def _log(self, step: str, path: str, started: float, code: int | None) -> None:
        if self._trail is not None:
            self._trail.event(
                "rclone",
                step=step,
                path=path,
                seconds=round(time.monotonic() - started, 3),
                exit=code,
            )

    @staticmethod
    def _failure(step: str, path: str, done: subprocess.CompletedProcess[bytes]) -> UploadError:
        said = redact(done.stderr.decode("utf-8", "replace")).strip().splitlines()
        last = said[-1].strip() if said else "(no message)"
        return UploadError(f"rclone {step} {path} failed (exit {done.returncode}): {last}")

    def copyto(self, local: Path, dst: str) -> None:
        """Copy the file ``local`` to ``dst`` in the remote (R3.1, R3.4)."""
        done = self._run("copyto", dst, [str(local.absolute()), self._at(dst), *_COPY_FLAGS], None)
        if done.returncode != 0:
            raise self._failure("copyto", dst, done)

    def cat(self, path: str) -> bytes | None:
        """The bytes of ``path``; ``None`` only when rclone says it is not there (R4.1).
        Any other failure is an error: an unreadable index is not an empty one (R4.4)."""
        args = [self._at(path), "--error-on-no-transfer"]
        done = self._run("cat", path, args, _SHORT_TIMEOUT_S)
        if done.returncode == 0:
            return done.stdout
        if done.returncode == _CAT_NO_TRANSFER_EXIT or (
            done.returncode == _CAT_LOCAL_MISSING_EXIT and b"not found" in done.stderr
        ):
            return None
        raise self._failure("cat", path, done)

    def moveto(self, src: str, dst: str) -> None:
        """Move ``src`` onto ``dst`` inside the remote (R4.5): copy + delete on S3."""
        done = self._run(
            "moveto", dst, [self._at(src), self._at(dst), _NO_BUCKET_CHECK], _SHORT_TIMEOUT_S
        )
        if done.returncode != 0:
            raise self._failure("moveto", dst, done)

    def purge(self, path: str) -> None:
        """Delete ``path`` and everything under it from the remote (R5.1): irreversible."""
        # The last guard before an irreversible delete: "" would purge the whole bucket.
        release = path.removeprefix("releases/")
        if not path.startswith("releases/") or not _RELEASE_DATE_RE.fullmatch(release):
            raise UploadError(f"refusing to purge {path!r}: not releases/YYYY.MM.DD")
        done = self._run("purge", path, [self._at(path), _NO_BUCKET_CHECK], _SHORT_TIMEOUT_S)
        if done.returncode != 0:
            raise self._failure("purge", path, done)
