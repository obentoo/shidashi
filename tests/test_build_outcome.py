"""The build outcome contract: model, atomic write, load (story 030, task 1.2; R3.7, R5.3).

Contract from design.md "shidashi/build_outcome.py" and "Data Models": ``BuildOutcome``
(``extra="forbid"``) with ``arch``, ``init``, ``status`` (``ok``, ``images-failed``,
``toolchain-refused`` or ``error``), ``exit_code``, ``isos``, ``failed_images``,
``message`` and ``toolchain`` (a ``ToolchainRefusal``: ``differences``, ``pkgdir``,
``successor``, or None). ``write(path, outcome)`` goes through a temporary file in the same
directory renamed over ``path`` (R5.3); ``load(path)`` raises ``ReleaseError`` for a
missing, corrupt or wrong-shaped file -- a child killed before or while writing it (R3.7).
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests._pending import try_import

BuildOutcome: Any = try_import("shidashi.build_outcome", "BuildOutcome")
ToolchainRefusal: Any = try_import("shidashi.build_outcome", "ToolchainRefusal")
ReleaseError: Any = try_import("shidashi.build_outcome", "ReleaseError")
write: Any = try_import("shidashi.build_outcome", "write")
load: Any = try_import("shidashi.build_outcome", "load")

ISO = "bentoo-2026.10.25-kde-systemd-v3.iso"


def outcome_fields(status: str) -> dict[str, Any]:
    """A plausible outcome of each status, as JSON fields."""
    base: dict[str, Any] = {
        "arch": "v3",
        "init": "systemd",
        "status": status,
        "exit_code": 0,
        "isos": [],
        "failed_images": [],
        "message": "",
        "toolchain": None,
    }
    if status == "ok":
        base["isos"] = [ISO, "bentoo-2026.10.25-minimal-systemd-v3.iso"]
    elif status == "images-failed":
        base.update(exit_code=1, failed_images=["gnome", "kde"], isos=[ISO])
        base["message"] = "image(s) failed: gnome, kde"
    elif status == "toolchain-refused":
        base.update(exit_code=1, message="belongs to another generation")
        base["toolchain"] = {
            "differences": ["gcc 15.2.1 -> 16.1.0"],
            "pkgdir": "/var/cache/shidashi/binpkgs/v3/20260823T153057Z",
            "successor": "/var/cache/shidashi/binpkgs/v3/20260823T153057Z-gcc16",
        }
    else:
        base.update(exit_code=1, message="sha512 mismatch for stage3-amd64.tar.xz")
    return base


STATUSES = ("ok", "images-failed", "toolchain-refused", "error")


@pytest.mark.parametrize("status", STATUSES)
def test_an_outcome_round_trips_through_write_and_load(tmp_path: Path, status: str) -> None:
    fields = outcome_fields(status)
    path = tmp_path / ".outcome" / "v3-systemd.json"
    path.parent.mkdir()
    write(path, BuildOutcome.model_validate(fields))
    assert path.is_file(), "write left no outcome file"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == fields
    loaded = load(path)
    assert loaded.status == status
    assert loaded.model_dump(mode="json") == fields


def test_load_of_a_missing_outcome_is_a_release_error_naming_the_file(tmp_path: Path) -> None:
    """R3.7: the child died before it wrote anything."""
    path = tmp_path / "v3-systemd.json"
    with pytest.raises(ReleaseError) as err:
        load(path)
    assert str(path) in str(err.value)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param('{"arch": "v3", "init": "syst', id="truncated"),
        pytest.param("not json at all\n", id="not-json"),
        pytest.param("[]", id="a-list"),
        pytest.param("\udcff", id="not-utf8"),
    ],
)
def test_load_of_a_corrupt_outcome_is_a_release_error_naming_the_file(
    tmp_path: Path, text: str
) -> None:
    """R3.7: a child killed while writing, or a file that is not JSON at all."""
    path = tmp_path / "v3-systemd.json"
    path.write_bytes(text.encode("utf-8", "surrogateescape"))
    with pytest.raises(ReleaseError) as err:
        load(path)
    assert str(path) in str(err.value)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"status": "partial"}, id="unknown-status"),
        pytest.param({"exit_code": "one"}, id="exit-code-not-int"),
        pytest.param({"isos": ISO}, id="isos-not-a-list"),
        pytest.param({"signed_by": "someone"}, id="extra-key"),
        pytest.param({"toolchain": {"differences": [], "pkgdir": "/p"}}, id="refusal-no-successor"),
    ],
)
def test_load_of_a_wrong_shaped_outcome_is_a_release_error(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    """R3.7: valid JSON is not enough -- an unknown status, a wrong type or a key the
    contract does not have is refused, never read as a partial success."""
    fields = {**outcome_fields("ok"), **change}
    path = tmp_path / "v3-systemd.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    with pytest.raises(ReleaseError) as err:
        load(path)
    assert str(path) in str(err.value)


def test_load_of_an_outcome_missing_a_field_is_a_release_error(tmp_path: Path) -> None:
    fields = outcome_fields("ok")
    del fields["status"]
    path = tmp_path / "v3-systemd.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    with pytest.raises(ReleaseError):
        load(path)


def test_write_goes_through_a_temporary_file_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.3: a rename over the target, from a sibling file -- never a write in place."""
    path = tmp_path / "v3-systemd.json"
    path.write_text(json.dumps(outcome_fields("error")), encoding="utf-8")
    renames: list[tuple[Path, Path]] = []
    real_replace, real_rename = os.replace, os.rename

    def spy_replace(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst, *a, **k)

    def spy_rename(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "rename", spy_rename)
    write(path, BuildOutcome.model_validate(outcome_fields("ok")))
    assert [dst for _src, dst in renames] == [path]
    ((src, _dst),) = renames
    assert src.parent == path.parent and src != path
    assert load(path).status == "ok"
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_a_write_that_fails_leaves_the_previous_outcome_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.3: the rename fails (a full disk, EXDEV): the reader still finds the whole
    previous file, and no half-written sibling is left behind."""
    path = tmp_path / "v3-systemd.json"
    previous = json.dumps(outcome_fields("error"))
    path.write_text(previous, encoding="utf-8")

    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken)
    monkeypatch.setattr(os, "rename", broken)
    with pytest.raises((ReleaseError, OSError)):
        write(path, BuildOutcome.model_validate(outcome_fields("ok")))
    assert path.read_text(encoding="utf-8") == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_the_model_refuses_an_unknown_status_and_an_extra_field() -> None:
    with pytest.raises(Exception) as err:
        BuildOutcome.model_validate({**outcome_fields("ok"), "status": "partial"})
    assert not isinstance(err.value, AssertionError), "BuildOutcome is not implemented"
    with pytest.raises(Exception) as err:
        BuildOutcome.model_validate({**outcome_fields("ok"), "extra": 1})
    assert not isinstance(err.value, AssertionError), "BuildOutcome is not implemented"
    refusal = ToolchainRefusal.model_validate(outcome_fields("toolchain-refused")["toolchain"])
    assert refusal.successor.endswith("-gcc16")
