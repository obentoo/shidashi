"""``release_report.render`` -- ``release.json`` for a human or a job summary (story 032,
task 4.1; R3.1, R3.2, R3.3, R3.5).

Contract (design.md "shidashi release report"): pure ``render(manifest, *, markdown) -> str``
raising ``ReleaseReportError`` for a manifest of the wrong shape, naming the key. One table
row per variant -- arch, init, status, ISOs, generation (R3.1); a ``toolchain-refused``
variant adds its successor PKGDIR (R3.2); ``"signed": false`` says the release is unsigned
and is signed on the host (R3.3); a manifest with no ``signed`` key is reported as signed
(R3.5) -- it renders exactly like ``"signed": true``.

The manifests are in the shape story 030's ``release.manifest`` writes today (status
``ok``; a build's refusal under ``toolchain_refusal``, its versions under ``toolchain``);
tests/test_release_json_signed_contract.py checks the renderer against the real producer.
"""

import re
from typing import Any

import pytest

from tests._pending import try_import

render: Any = try_import("shidashi.release_report", "render")
ReleaseReportError: Any = try_import("shidashi.release_report", "ReleaseReportError")

REPORT_DATE = "2026-11-01"
GEN_V3 = "20260823T153057Z"
GEN_ZN = "20260905T101500Z"
SUCCESSOR_V3 = f"/var/cache/shidashi/binpkgs/v3/{GEN_V3}-gcc16"
SUCCESSOR_ZN = f"/var/cache/shidashi/binpkgs/znver5/{GEN_ZN}-gcc16"
REPORT_TOOLCHAIN = {"gcc": "15.2.1", "glibc": "2.42", "binutils": "2.45"}


def report_iso(flavor: str, init: str, arch: str) -> str:
    return f"bentoo-2026.11.01-{flavor}-{init}-{arch}.iso"


def built_entry(
    arch: str, init: str, generation: str, *flavors: str, publish: str = "skipped: unsigned"
) -> dict[str, Any]:
    return {
        "arch": arch,
        "init": init,
        "status": "ok",
        "isos": [report_iso(f, init, arch) for f in flavors],
        "generation": generation,
        "toolchain": dict(REPORT_TOOLCHAIN),
        "publish": publish,
    }


def refused_entry(arch: str, init: str, generation: str, successor: str) -> dict[str, Any]:
    pkgdir = successor.removesuffix("-gcc16")
    return {
        "arch": arch,
        "init": init,
        "status": "toolchain-refused",
        "isos": [],
        "generation": None,
        "toolchain": None,
        "publish": "not run: build failed",
        "toolchain_refusal": {
            "differences": ["gcc 15.2.1 -> 16.1.0"],
            "pkgdir": pkgdir,
            "successor": successor,
        },
        "exit_code": 1,
        "message": f"{pkgdir} belongs to another generation ({generation})",
    }


def failed(arch: str, init: str) -> dict[str, Any]:
    return {
        "arch": arch,
        "init": init,
        "status": "error",
        "isos": [],
        "generation": None,
        "toolchain": None,
        "publish": "not run: build failed",
        "exit_code": 137,
        "message": "build exited 137 without a readable outcome",
    }


def manifest_of(*variants: dict[str, Any], **top: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "date": REPORT_DATE,
        "started": "2026-11-01T01:00:03Z",
        "finished": "2026-11-01T07:12:40Z",
        "variants": list(variants),
    }
    body.update(top)
    return body


def row_of(text: str, arch: str, init: str) -> str:
    """The one line of ``text`` that is about ``arch`` x ``init``."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(arch)}\W+{re.escape(init)}(?![\w-])")
    mine = [line for line in text.splitlines() if pattern.search(line)]
    assert len(mine) == 1, (arch, init, text)
    return mine[0]


THREE = manifest_of(
    built_entry("v3", "systemd", GEN_V3, "minimal", "kde"),
    # Hostile halves of "one row per variant": a variant sharing the arch, and one sharing
    # the init -- neither may borrow the other's row, ISOs or generation.
    built_entry("v3", "openrc", GEN_ZN, "gnome"),
    failed("znver5", "systemd"),
    signed=False,
)


@pytest.mark.parametrize("markdown", [True, False], ids=["markdown", "plain"])
def test_report_has_one_row_per_variant_with_its_status_isos_and_generation(
    markdown: bool,
) -> None:
    text = render(THREE, markdown=markdown)
    first = row_of(text, "v3", "systemd")
    for name in (report_iso("minimal", "systemd", "v3"), report_iso("kde", "systemd", "v3")):
        assert name in first
    assert "ok" in first and GEN_V3 in first
    assert report_iso("gnome", "openrc", "v3") not in first and GEN_ZN not in first

    second = row_of(text, "v3", "openrc")
    assert report_iso("gnome", "openrc", "v3") in second and GEN_ZN in second
    assert report_iso("kde", "systemd", "v3") not in second and GEN_V3 not in second

    third = row_of(text, "znver5", "systemd")
    assert "error" in third
    assert "bentoo-" not in third


def test_markdown_report_rows_are_table_rows() -> None:
    text = render(THREE, markdown=True)
    for arch, init in (("v3", "systemd"), ("v3", "openrc"), ("znver5", "systemd")):
        assert row_of(text, arch, init).lstrip().startswith("|")


def test_report_names_the_release_date() -> None:
    assert REPORT_DATE in render(THREE, markdown=True) or "2026.11.01" in render(
        THREE, markdown=True
    )


def test_report_names_the_successor_pkgdir_of_each_refused_variant_only() -> None:
    body = manifest_of(
        refused_entry("v3", "systemd", GEN_V3, SUCCESSOR_V3),
        built_entry("v3", "openrc", GEN_V3, "minimal"),
        refused_entry("znver5", "systemd", GEN_ZN, SUCCESSOR_ZN),
        signed=False,
    )
    text = render(body, markdown=True)
    assert text.count(SUCCESSOR_V3) == 1, text
    assert text.count(SUCCESSOR_ZN) == 1, text
    assert "toolchain-refused" in row_of(text, "v3", "systemd")
    assert "successor" not in row_of(text, "v3", "openrc").lower()


def test_report_of_an_unsigned_release_says_it_is_signed_on_the_host() -> None:
    text = render(
        manifest_of(built_entry("v3", "systemd", GEN_V3, "kde"), signed=False), markdown=True
    )
    assert "unsigned" in text.lower()
    assert "host" in text.lower()


def test_report_reads_a_missing_signed_key_as_signed() -> None:
    # a signed release's publish line: an unsigned one's would say "unsigned" in any row
    variant = built_entry(
        "v3", "systemd", GEN_V3, "kde", publish=f"published /srv/v3/{GEN_V3} (2 finals)"
    )
    missing = render(manifest_of(variant), markdown=True)
    signed = render(manifest_of(variant, signed=True), markdown=True)
    unsigned = render(manifest_of(variant, signed=False), markdown=True)
    # Converse halves: a missing key and `true` are the same release; `false` is not.
    assert missing == signed
    assert unsigned != signed
    assert "unsigned" not in signed.lower()


@pytest.mark.parametrize(
    "value",
    [
        # Hostile halves: a truthy non-boolean must not be read as signed, nor a falsy one
        # as unsigned -- the key is a JSON boolean or it is malformed.
        pytest.param("false", id="the-string-false"),
        pytest.param(0, id="zero"),
        pytest.param(None, id="null"),
    ],
)
def test_report_refuses_a_signed_value_that_is_not_a_boolean(value: object) -> None:
    body = manifest_of(built_entry("v3", "systemd", GEN_V3, "kde"), signed=value)
    with pytest.raises(ReleaseReportError, match="signed"):
        render(body, markdown=True)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        pytest.param({"date": REPORT_DATE, "signed": False}, "variants", id="no-variants"),
        pytest.param(
            {"date": REPORT_DATE, "variants": {"v3": "ok"}}, "variants", id="variants-not-list"
        ),
        pytest.param(
            manifest_of({"arch": "v3", "init": "systemd", "isos": []}),
            "status",
            id="variant-without-status",
        ),
    ],
)
def test_report_refuses_a_manifest_of_the_wrong_shape_naming_the_key(
    body: dict[str, Any], key: str
) -> None:
    with pytest.raises(ReleaseReportError, match=key):
        render(body, markdown=True)
