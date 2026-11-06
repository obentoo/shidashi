"""``ci_guest.fetch``: the pinned base image on the work disk (story 034, task 2.1; R2.1-R2.4,
R9.1, Q11).

Contract (design.md, "shidashi/ci_guest.py" -> ``fetch``):
``fetch(pins, *, root, opener=urllib.request.urlopen) -> Path`` downloads the pinned image
to ``<root>/images/<sha256>.img`` through a ``.part`` file, verifies its sha256 and only
then gives it its final name. Present and verified -> no download (R2.3). A digest that
differs -> the partial file deleted and ``CiGuestError`` naming both digests (R2.2). An
interrupted download -> nothing under the final name (R2.4). One ``ci_fetch`` JSON event
per line on stderr (R9.1).

Driven against a local HTTP server: ``opener`` sends the pinned ``https://`` URL there.
The pins are loaded with ``ci_guest_seed.load_pins`` from files written per test.
"""

import hashlib
import http.server
import json
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import
from tests.test_ci_guest_seed import write_pins

fetch: Any = try_import("shidashi.ci_guest", "fetch")
CiGuestError: Any = try_import("shidashi.ci_guest", "CiGuestError")
load_pins: Any = try_import("shidashi.ci_guest_seed", "load_pins")

PAYLOAD = b"QFI\xfb ubuntu 26.04 cloud image stand-in " * 40_000  # ~1.5 MiB: several chunks
PINNED_URL = "https://cloud-images.ubuntu.com/releases/26.04/release-20261020/ubuntu.img"


@dataclass
class ImageServer:
    """Serves one image; ``mode`` is ``ok``, ``missing`` (404) or ``cut`` (half, then closed)."""

    body: bytes = PAYLOAD
    mode: str = "ok"
    hits: list[str] = field(default_factory=list)
    port: int = 0
    _server: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        image = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                image.hits.append(self.path)
                if image.mode == "missing":
                    self.send_error(404, "Not Found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(image.body)))
                self.end_headers()
                data = image.body if image.mode == "ok" else image.body[: len(image.body) // 2]
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except BrokenPipeError, ConnectionResetError:
                    pass
                if image.mode == "cut":
                    self.close_connection = True

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@dataclass
class OpenerToLocal:
    """``urlopen`` for the pinned URL, answered by :class:`ImageServer`; records each call."""

    server: ImageServer
    asked: list[str] = field(default_factory=list)

    def __call__(self, url: Any, *args: Any, **kwargs: Any) -> Any:
        full = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        self.asked.append(full)
        assert full == PINNED_URL, f"fetch asked for {full}, not the pinned URL"
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.server.port}/ubuntu.img", *args, **kwargs
        )


@pytest.fixture
def server() -> Iterator[ImageServer]:
    s = ImageServer()
    s.start()
    yield s
    s.stop()


def pins_for(tmp_path: Path, digest: str) -> Any:
    return load_pins(*write_pins(tmp_path / "pins", url=PINNED_URL, sha256=digest))


def good_digest() -> str:
    return hashlib.sha256(PAYLOAD).hexdigest()


def leftovers(root: Path) -> list[str]:
    images = root / "images"
    return sorted(p.name for p in images.iterdir()) if images.is_dir() else []


def events(text: str, name: str) -> list[dict[str, Any]]:
    found = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == name:
            found.append(obj)
    return found


def test_fetch_downloads_and_keeps_the_image_named_by_its_digest(
    tmp_path: Path, server: ImageServer
) -> None:
    """R2.1: downloaded on the worker, kept only once its sha256 is the pinned one."""
    digest = good_digest()
    root = tmp_path / "ci"
    path = fetch(pins_for(tmp_path, digest), root=root, opener=OpenerToLocal(server))
    assert path == root / "images" / f"{digest}.img"
    assert path.read_bytes() == PAYLOAD
    assert leftovers(root) == [f"{digest}.img"], "a .part file was left beside the image"


def test_fetch_a_wrong_digest_deletes_the_part_and_names_both_digests(
    tmp_path: Path, server: ImageServer
) -> None:
    """R2.2: the pin and the bytes disagree -> nothing kept, both digests named."""
    pinned = "e5" * 32
    root = tmp_path / "ci"
    with pytest.raises(CiGuestError) as caught:
        fetch(pins_for(tmp_path, pinned), root=root, opener=OpenerToLocal(server))
    message = str(caught.value)
    assert pinned in message and good_digest() in message, message
    assert leftovers(root) == []
    assert server.hits, "nothing was downloaded"


def test_fetch_a_present_verified_image_is_not_downloaded_again(
    tmp_path: Path, server: ImageServer
) -> None:
    """R2.3."""
    digest = good_digest()
    root = tmp_path / "ci"
    (root / "images").mkdir(parents=True)
    (root / "images" / f"{digest}.img").write_bytes(PAYLOAD)
    opener = OpenerToLocal(server)
    path = fetch(pins_for(tmp_path, digest), root=root, opener=opener)
    assert path == root / "images" / f"{digest}.img"
    assert opener.asked == [] and server.hits == []


def test_fetch_a_present_image_of_other_bytes_is_not_trusted(
    tmp_path: Path, server: ImageServer
) -> None:
    """Hostile half of R2.3: the right NAME with the wrong bytes (a disk error, a write cut
    by a power loss) is not "already present": it is downloaded again."""
    digest = good_digest()
    root = tmp_path / "ci"
    (root / "images").mkdir(parents=True)
    (root / "images" / f"{digest}.img").write_bytes(PAYLOAD[:1000])
    path = fetch(pins_for(tmp_path, digest), root=root, opener=OpenerToLocal(server))
    assert path == root / "images" / f"{digest}.img"
    assert path.read_bytes() == PAYLOAD
    assert server.hits, "a corrupt image was kept without a download"


def test_fetch_an_interrupted_download_leaves_no_file_under_the_final_name_nor_a_part(
    tmp_path: Path, server: ImageServer
) -> None:
    """R2.4: the connection drops half-way -> ``CiGuestError``; no final name, no ``.part``."""
    server.mode = "cut"
    digest = good_digest()
    root = tmp_path / "ci"
    with pytest.raises(CiGuestError):
        fetch(pins_for(tmp_path, digest), root=root, opener=OpenerToLocal(server))
    assert server.hits, "nothing was downloaded"
    assert leftovers(root) == []


def test_fetch_resumes_cleanly_after_a_part_left_by_a_crash(
    tmp_path: Path, server: ImageServer
) -> None:
    """A ``.part`` an earlier crash left is never taken for the image: the next fetch
    replaces it and ends with the verified image alone."""
    digest = good_digest()
    root = tmp_path / "ci"
    (root / "images").mkdir(parents=True)
    (root / "images" / f"{digest}.img.part").write_bytes(b"half of something else")
    path = fetch(pins_for(tmp_path, digest), root=root, opener=OpenerToLocal(server))
    assert path == root / "images" / f"{digest}.img"
    assert path.read_bytes() == PAYLOAD
    assert leftovers(root) == [f"{digest}.img"]


def test_fetch_a_missing_image_names_the_url(tmp_path: Path, server: ImageServer) -> None:
    server.mode = "missing"
    root = tmp_path / "ci"
    with pytest.raises(CiGuestError) as caught:
        fetch(pins_for(tmp_path, good_digest()), root=root, opener=OpenerToLocal(server))
    assert PINNED_URL in str(caught.value)
    assert leftovers(root) == []


def test_fetch_emits_one_json_event_per_line(
    tmp_path: Path, server: ImageServer, capfd: pytest.CaptureFixture[str]
) -> None:
    """R9.1, Q9: one ``ci_fetch`` object on one stderr line, with its result and duration."""
    fetch(pins_for(tmp_path, good_digest()), root=tmp_path / "ci", opener=OpenerToLocal(server))
    seen = events(capfd.readouterr().err, "ci_fetch")
    assert len(seen) == 1, seen
    assert seen[0].get("result") and isinstance(seen[0].get("duration_ms"), int)
    assert seen[0].get("url") == PINNED_URL
