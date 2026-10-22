"""The upstream stage3 check (story 030, task 3.1; R7.1, R7.2, R7.4-R7.6).

Contract from design.md "shidashi/upstream.py": ``pointer_url(pointer, init)`` ->
``<base_url>/latest-stage3-amd64-nomultilib-<init>.txt``; ``parse_latest(text)`` -> the
``(snapshot, filename)`` of the first ``<snapshot>/<filename> <size>`` line of the
clear-signed body (``#`` and blank lines skipped; none = ``UpstreamError``);
``UpstreamStage3(init, pinned, latest, note)`` with ``latest is None`` when it could not be
read; ``check(init, *, download, fetch=_fetch, verify=seed.gpg_verify)`` never raises for a
network, size, signature or parse failure. ``_fetch`` reads with a 30 s timeout and at most
64 KiB. The pointer is verified (clear-signed, Gentoo keyring) before it is parsed, and it
is only reported: nothing here changes a pin.

``fetch`` and ``verify`` are injected fakes; the signature block can be dummy. The pin comes
from a seeds directory written here, so a re-pin of the real ``seeds/`` changes nothing.
"""

import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shidashi import seed
from shidashi.seed import SeedError, Stage3Pointer
from tests._pending import try_import

UpstreamError: Any = try_import("shidashi.upstream", "UpstreamError")
UpstreamStage3: Any = try_import("shidashi.upstream", "UpstreamStage3")
pointer_url: Any = try_import("shidashi.upstream", "pointer_url")
parse_latest: Any = try_import("shidashi.upstream", "parse_latest")
check: Any = try_import("shidashi.upstream", "check")
fetch_pointer: Any = try_import("shidashi.upstream", "_fetch")

PINNED = "20260823T153057Z"
LATEST = "20261018T124555Z"
BASE_URL = "https://distfiles.example.invalid/releases/amd64/autobuilds"
LATEST_FILE = f"stage3-amd64-nomultilib-systemd-{LATEST}.tar.xz"

#: The body of Gentoo's pointer as fetched on 2026-10-22.
BODY = (
    "# Latest as of Thu, 22 Oct 2026 01:15:01 +0000\n"
    "# ts=1792631701\n"
    f"{LATEST}/{LATEST_FILE} 288509620\n"
)
DUMMY_SIGNATURE = (
    "-----BEGIN PGP SIGNATURE-----\n\n"
    "iHUEARYKAB0WIQRQZm9vYmFyYmF6cXV4ZHVtbXlzaWduYXR1cmU=\n"
    "=AAAA\n"
    "-----END PGP SIGNATURE-----\n"
)


def clearsigned(body: str, *, after: str = "") -> str:
    return f"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n{body}{DUMMY_SIGNATURE}{after}"


POINTER = clearsigned(BODY)


@pytest.fixture(autouse=True)
def seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "seeds"
    where.mkdir()
    (where / "stage3.toml").write_text(
        f'snapshot = "{PINNED}"\nbase_url = "{BASE_URL}"\n\n'
        f'[systemd]\nfilename = "stage3-amd64-nomultilib-systemd-{PINNED}.tar.xz"\n'
        f'sha512 = "{"a" * 128}"\n\n'
        f'[openrc]\nfilename = "stage3-amd64-nomultilib-openrc-{PINNED}.tar.xz"\n'
        f'sha512 = "{"b" * 128}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SHIDASHI_SEEDS_DIR", str(where))
    return where


class FakeMirror:
    """Stands in for the HTTP fetch: answers ``answer`` (bytes, or an exception to raise)
    and records the URLs asked for."""

    def __init__(self, answer: bytes | BaseException) -> None:
        self.answer = answer
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class FakeGentooKeyring:
    """Stands in for ``seed.gpg_verify``: accepts or refuses, and keeps what it was shown."""

    def __init__(self, *, good: bool = True) -> None:
        self.good = good
        self.seen: list[bytes] = []
        self.keyrings: list[Path] = []

    def __call__(self, *files: Path, keyring: Path = seed.GENTOO_KEYRING) -> None:
        self.seen.append(files[-1].read_bytes())
        self.keyrings.append(keyring)
        if not self.good:
            raise SeedError(f"GPG verification failed for {files[-1].name}: BAD signature")


def never_fetch(url: str) -> bytes:
    raise AssertionError(f"fetched {url} although downloads are off")


# --- pointer_url (R7.1) ----------------------------------------------------------------------


@pytest.mark.parametrize("base", [BASE_URL, BASE_URL + "/"])
@pytest.mark.parametrize("init", ["systemd", "openrc"])
def test_the_pointer_url_is_under_the_pinned_base_url_with_no_doubled_slash(
    base: str, init: str
) -> None:
    pointer = Stage3Pointer(
        init=init, base_url=base, snapshot=PINNED, filename="f.tar.xz", sha512="a" * 128
    )
    assert pointer_url(pointer, init) == f"{BASE_URL}/latest-stage3-amd64-nomultilib-{init}.txt"


# --- parse_latest -------------------------------------------------------------------------------


def test_a_line_outside_the_signed_body_is_never_taken_for_the_stage3() -> None:
    """Hostile: the body names nothing; text after the signature block -- which gpg does
    not cover -- names a newer stage3. Taking it would report what nobody signed."""
    tampered = clearsigned(
        "# Latest as of Thu, 22 Oct 2026\n",
        after=("20991231T000000Z/stage3-amd64-nomultilib-systemd-20991231T000000Z.tar.xz 1\n"),
    )
    with pytest.raises(UpstreamError):
        parse_latest(tampered)


def test_the_real_pointer_gives_its_snapshot_and_filename() -> None:
    assert tuple(parse_latest(POINTER)) == (LATEST, LATEST_FILE)


def test_comments_blank_lines_and_extra_whitespace_are_skipped() -> None:
    body = f"# Latest as of ...\n\n# ts=1\n\n  {LATEST}/{LATEST_FILE} \t 288509620  \n"
    assert tuple(parse_latest(clearsigned(body))) == (LATEST, LATEST_FILE)


def test_only_the_first_entry_counts() -> None:
    body = BODY + f"{PINNED}/stage3-amd64-nomultilib-systemd-{PINNED}.tar.xz 1\n"
    assert tuple(parse_latest(clearsigned(body))) == (LATEST, LATEST_FILE)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("# Latest as of ...\n# ts=1\n", id="comments-only"),
        pytest.param("", id="empty"),
        pytest.param(f"{LATEST_FILE} 288509620\n", id="no-snapshot-dir"),
    ],
)
def test_a_body_that_names_no_stage3_is_an_upstream_error(body: str) -> None:
    with pytest.raises(UpstreamError):
        parse_latest(clearsigned(body))


# --- check (R7.2, R7.4-R7.6) -----------------------------------------------------------------


def test_with_downloads_off_the_pointer_is_never_fetched_and_upstream_is_unknown() -> None:
    """R7.5, R7.6."""
    report = check("systemd", download=False, fetch=never_fetch, verify=FakeGentooKeyring())
    assert report.init == "systemd" and report.pinned == PINNED
    assert report.latest is None
    assert "--no-download" in report.note


def test_a_newer_verified_pointer_reports_both_snapshots_and_leaves_the_pin(
    seeds: Path,
) -> None:
    """R7.1, R7.2: the URL is the init's pointer; the bytes verified are the bytes fetched.
    R7.3 at the source: seeds/stage3.toml stays byte for byte."""
    pin = (seeds / "stage3.toml").read_bytes()
    mirror, keyring = FakeMirror(POINTER.encode()), FakeGentooKeyring()
    report = check("systemd", download=True, fetch=mirror, verify=keyring)
    assert (seeds / "stage3.toml").read_bytes() == pin
    assert mirror.urls == [f"{BASE_URL}/latest-stage3-amd64-nomultilib-systemd.txt"]
    assert keyring.seen == [POINTER.encode()]
    assert keyring.keyrings == [seed.GENTOO_KEYRING]
    assert (report.init, report.pinned, report.latest) == ("systemd", PINNED, LATEST)


def test_each_init_reads_its_own_pointer() -> None:
    mirror = FakeMirror(clearsigned(BODY.replace("systemd", "openrc")).encode())
    report = check("openrc", download=True, fetch=mirror, verify=FakeGentooKeyring())
    assert mirror.urls == [f"{BASE_URL}/latest-stage3-amd64-nomultilib-openrc.txt"]
    assert report.latest == LATEST and report.pinned == PINNED


def test_a_pointer_whose_signature_fails_is_unknown_even_when_it_names_a_newer_stage3() -> None:
    """R7.4, hostile: the bytes name a newer stage3, but nothing proves Gentoo wrote them."""
    report = check(
        "systemd",
        download=True,
        fetch=FakeMirror(POINTER.encode()),
        verify=FakeGentooKeyring(good=False),
    )
    assert report.latest is None
    assert "signature" in report.note.lower()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(urllib.error.URLError("Name or service not known"), id="offline"),
        pytest.param(TimeoutError("timed out"), id="timeout"),
        pytest.param(OSError(104, "Connection reset by peer"), id="reset"),
    ],
)
def test_a_pointer_that_cannot_be_fetched_is_unknown_with_the_reason(
    failure: BaseException,
) -> None:
    """R7.4: the release goes on -- ``check`` never raises for the network."""
    report = check("systemd", download=True, fetch=FakeMirror(failure), verify=FakeGentooKeyring())
    assert report.latest is None and report.pinned == PINNED
    assert report.note, "no reason given"


def test_a_verified_pointer_that_names_no_stage3_is_unknown() -> None:
    report = check(
        "systemd",
        download=True,
        fetch=FakeMirror(clearsigned("# nothing today\n").encode()),
        verify=FakeGentooKeyring(),
    )
    assert report.latest is None and report.note


# --- _fetch: 30 s and 64 KiB (R7.4) ----------------------------------------------------------


class FakeResponse:
    """Stands in for ``urlopen``'s response: ``read(n)`` honours ``n``."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.asked: list[int | None] = []

    def read(self, amount: int | None = None) -> bytes:
        self.asked.append(amount)
        return self.body if amount is None or amount < 0 else self.body[:amount]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def fake_urlopen(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> tuple[list[dict[str, Any]], Callable[..., FakeResponse]]:
    opened: list[dict[str, Any]] = []

    def urlopen(url: Any, data: Any = None, timeout: Any = None, **kw: Any) -> FakeResponse:
        opened.append({"url": getattr(url, "full_url", url), "timeout": timeout})
        return FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    module = sys.modules.get("shidashi.upstream")
    if module is not None:  # a ``from urllib.request import urlopen`` binds its own name
        monkeypatch.setattr(module, "urlopen", urlopen, raising=False)
    return opened, urlopen


def padded(size: int) -> bytes:
    """A valid pointer padded with comment lines to exactly ``size`` bytes."""
    head = clearsigned("").encode()
    filler = size - len(head) - len(BODY.encode())
    pad = ("#" * (filler - 1) + "\n").encode() if filler > 0 else b""
    text = clearsigned(pad.decode() + BODY).encode()
    assert len(text) == size
    return text


def test_a_pointer_one_byte_over_64_kib_is_refused_with_a_30_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hostile half first: 64 KiB + 1 is refused (reported unknown), and the request
    carries a 30 s timeout -- urlopen has none by default."""
    opened, _ = fake_urlopen(monkeypatch, padded(64 * 1024 + 1))
    report = check("systemd", download=True, fetch=fetch_pointer, verify=FakeGentooKeyring())
    assert [o["timeout"] for o in opened] == [30]
    assert report.latest is None
    assert "64" in report.note


def test_a_pointer_of_exactly_64_kib_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The converse at the boundary: the cap is 64 KiB, not 64 KiB minus one."""
    fake_urlopen(monkeypatch, padded(64 * 1024))
    report = check("systemd", download=True, fetch=fetch_pointer, verify=FakeGentooKeyring())
    assert report.latest == LATEST
