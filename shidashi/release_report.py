"""``release.json`` as a table for a human or a job summary (story 032, R3).

Pure: :func:`render` reads a parsed manifest and returns text. It prints tables only, never
logs -- GitHub caps a step summary at 1 MiB. ``release.json`` is read as story 030's
``release.manifest`` writes it (status ``ok``; a refused build's successor under
``toolchain_refusal``) plus story 032's top-level ``signed``.

The headings and the signing state are in the language shown; the cells are what
``release.json`` holds.
"""

from collections.abc import Mapping, Sequence

from rich.cells import cell_len

from shidashi import i18n

#: A failure message longer than this is cut: a summary row is a pointer, the run log has the rest.
_MESSAGE_LIMIT = 200
_HEADERS = (
    i18n.mark("Variant"),
    i18n.mark("Status"),
    i18n.mark("ISOs"),
    i18n.mark("Generation"),
    i18n.mark("Notes"),
)


class ReleaseReportError(Exception):
    """``release.json`` is not the shape :func:`render` reads; the message names the key."""


def _text(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ReleaseReportError(f"release.json: {key} must be a string, got {value!r}")
    return value


def _variant_row(entry: object, position: int) -> tuple[str, ...]:
    """The five cells of one variant, or :class:`ReleaseReportError` naming the missing key."""
    where = f"variants[{position}]"
    if not isinstance(entry, Mapping):
        raise ReleaseReportError(f"release.json: {where} must be an object")
    for required in ("arch", "init", "status", "isos"):
        if required not in entry:
            raise ReleaseReportError(f"release.json: {where} has no {required}")
    isos = entry["isos"]
    if not isinstance(isos, list) or not all(isinstance(i, str) for i in isos):
        raise ReleaseReportError(f"release.json: {where}.isos must be a list of file names")
    status = _text(entry["status"], f"{where}.status")
    note = ""
    refusal = entry.get("toolchain_refusal")
    if isinstance(refusal, Mapping) and isinstance(refusal.get("successor"), str):
        note = f"successor PKGDIR: {refusal['successor']}"
    elif status != "ok" and isinstance(entry.get("message"), str):
        note = " ".join(str(entry["message"]).split())[:_MESSAGE_LIMIT]
    generation = entry.get("generation")
    return (
        f"{_text(entry['arch'], f'{where}.arch')}/{_text(entry['init'], f'{where}.init')}",
        status,
        ", ".join(isos),
        generation if isinstance(generation, str) else "-",
        note,
    )


def _signed(manifest: Mapping[str, object]) -> bool:
    """``signed``; a manifest from before story 032 has none and was signed (R3.5)."""
    if "signed" not in manifest:
        return True
    value = manifest["signed"]
    if not isinstance(value, bool):
        raise ReleaseReportError(f"release.json: signed must be true or false, got {value!r}")
    return value


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _plain_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> list[str]:
    table = [headers, *rows]
    # terminal cells, not characters: a CJK heading takes two per character
    widths = [max(cell_len(row[i]) for row in table) for i in range(len(headers))]
    return [
        "  ".join(
            cell + " " * (w - cell_len(cell)) for cell, w in zip(row, widths, strict=True)
        ).rstrip()
        for row in table
    ]


def render(manifest: Mapping[str, object], *, markdown: bool) -> str:
    """The release report: its date, whether it is signed, one row per variant (R3.1-R3.3).

    Raises :class:`ReleaseReportError` naming the key of a manifest that is not the shape
    above -- ``variants`` a list, each variant with ``arch``, ``init``, ``status`` and
    ``isos``, ``signed`` a boolean when present.
    """
    variants = manifest.get("variants")
    if not isinstance(variants, list):
        raise ReleaseReportError("release.json: variants must be a list")
    signed = _signed(manifest)
    rows = [_variant_row(entry, i) for i, entry in enumerate(variants)]
    date = manifest.get("date")
    title = (
        i18n.t("Release {date}").format(date=date) if isinstance(date, str) else i18n.t("Release")
    )
    signing = (
        i18n.t("Signing: signed")
        if signed
        else i18n.t("Signing: unsigned — sign on the host (story 033)")
    )
    headers = tuple(i18n.t(header) for header in _HEADERS)
    if not markdown:
        return "\n".join([title, signing, "", *_plain_table(headers, rows)]) + "\n"
    lines = [f"## {title}", "", signing, ""]
    lines.append("| " + " | ".join(_cell(header) for header in headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return "\n".join(lines) + "\n"
