"""The upstream stage3 check of a release (story 030 R7): report, never re-pin.

Gentoo publishes ``latest-stage3-amd64-nomultilib-<init>.txt`` next to its autobuilds. Since
2024 it is a PGP clear-signed message, so it is verified against the Gentoo release keyring
(:func:`shidashi.seed.gpg_verify`) before a single line of it is parsed, and only the signed
body is read: text after the signature block is not covered by the signature. It is read with
a 30 s timeout (``urlopen`` has none by default) and a 64 KiB cap, and the snapshot it names
is only reported -- the release builds from the pin whatever the pointer says (R7.3).
"""

import http.client
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shidashi import config, seed

_FETCH_TIMEOUT_S = 30
#: The real pointer is ~300 bytes; anything this large is not a pointer (R7.4).
_MAX_POINTER_BYTES = 64 * 1024
#: Gentoo's autobuild directory name, e.g. ``20261018T124555Z``; it sorts as text.
_SNAPSHOT = re.compile(r"\d{8}T\d{6}Z")
_SIGNED_HEADER = "-----BEGIN PGP SIGNED MESSAGE-----"
_SIGNATURE_HEADER = "-----BEGIN PGP SIGNATURE-----"


class UpstreamError(Exception):
    """The upstream pointer could not be read: oversize, or it names no stage3."""


@dataclass(frozen=True)
class UpstreamStage3:
    """What the upstream pointer said for ``init``, next to the pin.

    ``latest`` is ``None`` when it could not be read, and ``note`` says why.
    """

    init: str
    pinned: str
    latest: str | None
    note: str

    @property
    def newer(self) -> bool:
        """Whether upstream has a newer snapshot than the pin (R7.2)."""
        return self.latest is not None and self.latest > self.pinned


def pointer_url(pointer: seed.Stage3Pointer, init: str) -> str:
    """The init's ``latest-*.txt`` under the pinned ``base_url`` (R7.1). Pure."""
    return f"{pointer.base_url.rstrip('/')}/latest-stage3-amd64-nomultilib-{init}.txt"


def _signed_body(text: str) -> list[str]:
    """The lines of a clear-signed message between its armor headers and its signature."""
    lines = text.splitlines()
    try:
        start = [line.rstrip() for line in lines].index(_SIGNED_HEADER)
    except ValueError as err:
        raise UpstreamError("pointer is not a clear-signed message") from err
    body: list[str] = []
    in_headers = True
    for line in lines[start + 1 :]:
        if in_headers:
            # the armor headers ("Hash: SHA256") end at the first blank line
            in_headers = line.strip() != ""
            continue
        if line.rstrip() == _SIGNATURE_HEADER:
            return body
        body.append(line)
    raise UpstreamError("pointer has no signature block")


def parse_latest(text: str) -> tuple[str, str]:
    """The ``(snapshot, filename)`` of the first entry of the signed body. Pure.

    ``#`` and blank lines are skipped; the first other line must be
    ``<snapshot>/<filename> <size>``, or :class:`UpstreamError` is raised.
    """
    for line in _signed_body(text):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        snapshot, slash, filename = fields[0].partition("/")
        if slash and _SNAPSHOT.fullmatch(snapshot) and filename and "/" not in filename:
            return snapshot, filename
        break
    raise UpstreamError("pointer names no stage3")


def _fetch(url: str) -> bytes:
    """GET ``url`` with a 30 s timeout, refusing more than 64 KiB (R7.4)."""
    # The URL is built from the pinned base_url of this repository, and the bytes are
    # GPG-verified before they are parsed.
    # nosemgrep: dynamic-urllib-use-detected
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_S) as resp:
        data: bytes = resp.read(_MAX_POINTER_BYTES + 1)
    if len(data) > _MAX_POINTER_BYTES:
        raise UpstreamError("pointer exceeds 64 KiB")
    return data


def _verified(raw: bytes, name: str, verify: Callable[..., None]) -> None:
    """Run ``verify`` over ``raw`` written to a temp file that is always removed."""
    with tempfile.TemporaryDirectory(prefix="shidashi-upstream-") as where:
        path = Path(where) / name
        path.write_bytes(raw)
        verify(path)


def check(
    init: str,
    *,
    download: bool,
    fetch: Callable[[str], bytes] = _fetch,
    verify: Callable[..., None] = seed.gpg_verify,
) -> UpstreamStage3:
    """Compare Gentoo's latest stage3 for ``init`` with the pin; report only (R7.1-R7.6).

    Never raises for a network, size, signature or parse failure: the report carries
    ``latest=None`` and the reason (R7.4). ``download=False`` fetches nothing (R7.5, R7.6).
    """
    pointer = seed.load_pointer(init, seeds_dir=config.seeds_dir())

    def unknown(note: str) -> UpstreamStage3:
        return UpstreamStage3(init=init, pinned=pointer.snapshot, latest=None, note=note)

    if not download:
        return unknown("--no-download")
    url = pointer_url(pointer, init)
    try:
        raw = fetch(url)
    except (
        OSError,
        urllib.error.URLError,
        TimeoutError,
        http.client.HTTPException,
        UpstreamError,
    ) as err:
        return unknown(str(err) or type(err).__name__)
    try:
        _verified(raw, url.rsplit("/", 1)[-1], verify)
    except seed.SeedError as err:
        return unknown(f"signature invalid: {err}")
    except OSError as err:  # the temp file: the upstream check never fails a release
        return unknown(f"signature not checked: {err}")
    try:
        latest, _filename = parse_latest(raw.decode("utf-8"))
    except UnicodeDecodeError as err:
        return unknown(f"pointer is not UTF-8: {err}")
    except UpstreamError as err:
        return unknown(str(err))
    return UpstreamStage3(init=init, pinned=pointer.snapshot, latest=latest, note="")
