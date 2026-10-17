"""A corrupt SHA256SUMS is refused, never rewritten (story 014, task 6.4).

Contract (story R4.12, Q14; design "SHA256SUMS integrity"):

- ``publish.update_sha256sums(directory, sums)`` reads ``directory/SHA256SUMS`` as bytes,
  decodes it strictly as UTF-8, and requires every non-empty line to match
  ``^[0-9a-f]{64}  [^/\\n]+$``;
- an undecodable file, or the first wrong-shaped line, raises
  ``publish.CorruptSums(path, line_no, reason)`` -- a ``PublishError`` naming the file
  (and, for a wrong-shaped line, its line number); the file is left byte-for-byte
  unchanged and no ``.SHA256SUMS.*.tmp`` is left behind;
- a missing file is created; a valid one keeps the other files' lines; the write is a
  temporary file renamed over the target;
- in ``schedule.execute``, a worker image whose lines meet a corrupt output SHA256SUMS
  fails with that message, and the other outcomes are kept.

Hostile halves first: the wrongly-ACCEPTED shapes (near-valid lines, a leniently
decodable byte, a bad line for the very file being updated) and the wrongly-REFUSED
shapes (a name with spaces, a blank line, no final newline) come before the plain cases.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pytest

from shidashi import publish
from tests import test_schedule_execute_hardening as hardening
from tests.test_schedule_execute import Lab, _iso_name, _outcome

lab = hardening.lab  # the fixture, shared with the executor's tests
_execute = hardening._execute

LINE_RE = re.compile(r"^[0-9a-f]{64}  [^/\n]+$")


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


NEW = {"new.iso": _digest("new")}


def _corrupt_sums() -> type[BaseException] | None:
    return getattr(publish, "CorruptSums", None)


def _refused(directory: Path, sums: dict[str, str]) -> BaseException | None:
    """What ``update_sha256sums`` raised, or ``None`` when it wrote."""
    try:
        publish.update_sha256sums(directory, sums)
    except Exception as err:  # the contract is checked by the caller, not by except
        return err
    return None


def _assert_corrupt_sums(err: BaseException | None, path: Path) -> Any:
    kind = _corrupt_sums()
    assert kind is not None, "publish.CorruptSums does not exist"
    assert issubclass(kind, publish.PublishError), "CorruptSums must be a PublishError"
    assert isinstance(err, kind), f"expected CorruptSums, got {err!r} (None: the file was written)"
    assert str(path) in str(err), f"the message must name {path}: {err}"
    assert Path(err.path) == path  # type: ignore[attr-defined]
    assert isinstance(err.reason, str) and err.reason  # type: ignore[attr-defined]
    return err


def _temps(directory: Path) -> list[str]:
    pattern = re.compile(r"\.SHA256SUMS\..*\.tmp")
    return sorted(p.name for p in directory.iterdir() if pattern.fullmatch(p.name))


def _names_line(message: str, path: Path, line_no: int) -> bool:
    """``line_no`` appears as a number of its own once the path is taken out."""
    rest = message.replace(str(path), "")
    return re.search(rf"(?<![0-9a-f]){line_no}(?![0-9a-f])", rest) is not None


def _valid(*names: str) -> bytes:
    return "".join(f"{_digest(n)}  {n}\n" for n in names).encode()


# --- hostile: shapes that must NOT be accepted ----------------------------------------------

WRONG_SHAPES = {
    "uppercase-hex": f"{_digest('x').upper()}  x.iso",
    "63-hex": f"{_digest('x')[:63]}  x.iso",
    "65-hex": f"{_digest('x')}a  x.iso",
    "single-space": f"{_digest('x')} x.iso",
    "tab-separator": f"{_digest('x')}\tx.iso",
    "binary-marker": f"{_digest('x')} *x.iso",
    "slash-in-name": f"{_digest('x')}  sub/x.iso",
    "no-name": f"{_digest('x')}  ",
    "short-digest": "aaa  other.iso",
    "name-only": "x.iso",
}


@pytest.mark.parametrize("shape", sorted(WRONG_SHAPES))
def test_a_wrong_shaped_line_is_refused_naming_its_line_and_the_file_is_unchanged(
    tmp_path: Path, shape: str
) -> None:
    bad = WRONG_SHAPES[shape]
    assert not LINE_RE.match(bad)  # the fixture really is wrong-shaped
    path = tmp_path / "SHA256SUMS"
    before = _valid("a.iso", "b.iso") + f"{bad}\n".encode() + _valid("c.iso")
    path.write_bytes(before)
    err = _assert_corrupt_sums(_refused(tmp_path, NEW), path)
    assert err.line_no == 3, err.line_no
    assert _names_line(str(err), path, 3), f"the message must name line 3: {err}"
    assert path.read_bytes() == before
    assert _temps(tmp_path) == []


def test_the_first_wrong_line_is_the_one_named(tmp_path: Path) -> None:
    path = tmp_path / "SHA256SUMS"
    before = _valid("a.iso") + b"zzz  b.iso\n" + _valid("c.iso") + b"yyy  d.iso\n"
    path.write_bytes(before)
    err = _assert_corrupt_sums(_refused(tmp_path, NEW), path)
    assert err.line_no == 2, err.line_no
    assert path.read_bytes() == before


def test_a_bad_line_for_the_very_file_being_updated_is_still_refused(tmp_path: Path) -> None:
    """Replacing ``new.iso``'s line must not hide that the file holding it is corrupt."""
    path = tmp_path / "SHA256SUMS"
    before = _valid("a.iso") + b"not-a-digest  new.iso\n"
    path.write_bytes(before)
    err = _assert_corrupt_sums(_refused(tmp_path, NEW), path)
    assert err.line_no == 2, err.line_no
    assert path.read_bytes() == before
    assert _temps(tmp_path) == []


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"\xff\xfe" + _valid("a.iso"), id="leading-garbage"),
        pytest.param(
            _valid("a.iso") + f"{_digest('c')}  caf\xe9.iso\n".encode("latin-1"), id="latin-1-name"
        ),
        pytest.param(_valid("a.iso") + b"\x80\n", id="lone-continuation-byte"),
    ],
)
def test_an_undecodable_file_is_refused_naming_it_and_is_unchanged(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "SHA256SUMS"
    path.write_bytes(raw)
    _assert_corrupt_sums(_refused(tmp_path, NEW), path)
    assert path.read_bytes() == raw
    assert _temps(tmp_path) == []


def test_a_refusal_never_reaches_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renamed: list[tuple[str, str]] = []
    real_replace, real_rename = os.replace, os.rename

    def replace(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        renamed.append((str(src), str(dst)))
        real_replace(src, dst, *a, **k)

    def rename(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        renamed.append((str(src), str(dst)))
        real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "rename", rename)
    path = tmp_path / "SHA256SUMS"
    path.write_bytes(_valid("a.iso") + b"bad line\n")
    _assert_corrupt_sums(_refused(tmp_path, NEW), path)
    assert renamed == []


# --- hostile: shapes that must NOT be refused ------------------------------------------------


def test_a_name_with_spaces_a_blank_line_and_no_final_newline_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "SHA256SUMS"
    spaced = f"{_digest('s')}  my  image .iso"
    assert LINE_RE.match(spaced)
    last = _valid("b.iso").rstrip(b"\n")  # no final newline
    path.write_bytes(_valid("a.iso") + b"\n" + spaced.encode() + b"\n" + last)
    assert _refused(tmp_path, NEW) is None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert spaced in lines
    assert f"{_digest('a.iso')}  a.iso" in lines
    assert f"{_digest('b.iso')}  b.iso" in lines
    assert f"{NEW['new.iso']}  new.iso" in lines


def test_a_utf8_name_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "SHA256SUMS"
    line = f"{_digest('c')}  café-日本.iso"
    path.write_bytes(f"{line}\n".encode())
    assert _refused(tmp_path, NEW) is None
    assert line in path.read_text(encoding="utf-8").splitlines()


# --- benign: the write itself ------------------------------------------------------------------


def test_a_missing_file_is_created_with_the_new_lines(tmp_path: Path) -> None:
    sums = {"b.iso": _digest("b"), "a.iso": _digest("a")}
    assert _refused(tmp_path, sums) is None
    lines = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == sorted(f"{d}  {n}" for n, d in sums.items())
    assert all(LINE_RE.match(line) for line in lines)
    assert _temps(tmp_path) == []


def test_a_valid_file_keeps_the_other_files_lines(tmp_path: Path) -> None:
    path = tmp_path / "SHA256SUMS"
    path.write_bytes(_valid("other.iso") + f"{_digest('old')}  new.iso\n".encode())
    assert _refused(tmp_path, NEW) is None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert f"{_digest('other.iso')}  other.iso" in lines
    assert f"{NEW['new.iso']}  new.iso" in lines
    assert not any(line == f"{_digest('old')}  new.iso" for line in lines)
    assert _temps(tmp_path) == []


def test_the_write_goes_through_a_temp_file_renamed_over_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "SHA256SUMS"
    path.write_bytes(_valid("other.iso"))
    seen: list[tuple[Path, Path, bytes]] = []
    real_replace = os.replace

    def replace(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        seen.append((Path(src), Path(dst), Path(src).read_bytes()))
        real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    assert _refused(tmp_path, NEW) is None
    assert len(seen) == 1, seen
    src, dst, content = seen[0]
    assert dst == path
    assert src.parent == tmp_path and re.fullmatch(r"\.SHA256SUMS\..*\.tmp", src.name), src
    assert content == path.read_bytes()
    assert _temps(tmp_path) == []


# --- the build: a worker image meets a corrupt output SHA256SUMS -------------------------------


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(
            f"{_digest('old')}  old.iso\nnot-a-digest  older.iso\n".encode(), id="wrong-shape"
        ),
        pytest.param(b"\xff\xfe\n", id="undecodable"),
    ],
)
def test_a_worker_image_meeting_a_corrupt_file_fails_and_the_others_are_kept(
    lab: Lab, corrupt: bytes
) -> None:
    path = lab.out / "SHA256SUMS"
    path.write_bytes(corrupt)
    raised: BaseException | None = None
    outcomes: list[Any] = []
    try:
        outcomes = _execute(lab)
    except Exception as err:  # a crash is a failure of the contract, asserted below
        raised = err
    assert raised is None, f"execute must fail the images, not raise: {raised!r}"
    for name in ("gnome", "wm"):  # SPLIT: the worker's share
        got = _outcome(outcomes, name)
        assert not got.ok, f"{name} published its lines into a corrupt SHA256SUMS"
        assert got.error and str(path) in got.error, got.error
    for name in ("minimal", "kde"):  # SPLIT: the host's share
        got = _outcome(outcomes, name)
        assert got.ok, (name, got.error)
    assert path.read_bytes() == corrupt
    assert _temps(lab.out) == []


def test_a_worker_image_names_the_bad_line_number(lab: Lab) -> None:
    path = lab.out / "SHA256SUMS"
    path.write_bytes(_valid("old.iso", "older.iso") + b"zzz  oldest.iso\n")
    outcomes = _execute(lab)
    gnome = _outcome(outcomes, "gnome")
    assert not gnome.ok and str(path) in gnome.error, gnome.error
    assert _names_line(gnome.error, path, 3), gnome.error
    assert f"  {_iso_name('gnome')}" not in path.read_text(encoding="utf-8")
