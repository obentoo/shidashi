"""Soname waivers -- the soname-stale findings no rebuild can clear, in one file.

The soname rule (:func:`shidashi.binpkgs.soname_stale`) calls a binpkg stale when it
needs a library version its image no longer offers, and the answer is a rebuild. A
package that SHIPS a prebuilt binary linked to the old library needs it again after
every rebuild: the finding never clears, and the factory's ``stale-binpkgs`` step, its
``check-binpkgs`` and the assemble refuse the image for ever (the weekly release
2026.11.13: ``gentoo-kernel-bin``'s ``certs/extract-cert`` on ``libcrypto.so.3``).

``variants/soname-waivers.yaml`` lists those findings, one entry per package and
soname, each with its reason, its evidence and its EXIT CONDITION. A waived finding
is not stale: nothing is excluded, moved aside or refused for it. Nothing else is
waived -- the subslot rule, the unresolved sonames and any other soname of the same
package are judged as always.

The index records what a PACKAGE needs (``REQUIRES``), not which of its files needs
it, so a waiver cannot be narrower than a package and a soname.

:func:`load_soname_waivers` reads the file; :func:`apply_soname_waivers` is PURE and is
the only place a finding is matched to a waiver, so the three paths cannot disagree.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from shidashi import binpkgs

#: Where the waivers live, relative to the variants directory.
SONAME_WAIVERS_FILE = "soname-waivers.yaml"

#: ``category/name`` and nothing else: no operator, wildcard, version, slot or repository
#: (the characters PMS 3.1.1 and 3.1.2 allow in a category and in a package name).
_EXACT_PACKAGE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9+_.-]*/[A-Za-z0-9_][A-Za-z0-9+_-]*$")

#: One soname, as a ``REQUIRES`` line spells it: no blank, no wildcard, no category.
_EXACT_SONAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9+_.-]*$")


class SonameWaiverError(Exception):
    """A soname-waivers.yaml that cannot be read or does not validate."""


class SonameWaiver(BaseModel):
    """One package allowed to keep needing one soname, why, and until when."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    package: str
    soname: str
    why: str
    found: str
    until: str

    @field_validator("package")
    @classmethod
    def _exact_package(cls, value: str) -> str:
        # package_of strips a version: a name it changes carried one
        if not _EXACT_PACKAGE.match(value) or binpkgs.package_of(value) != value:
            raise ValueError(f"not an exact category/name (no version, no wildcard): {value!r}")
        return value

    @field_validator("soname")
    @classmethod
    def _exact_soname(cls, value: str) -> str:
        # only a versioned soname can be stale: any other is unresolved, never waived
        if not _EXACT_SONAME.match(value) or binpkgs.library_of(value) is None:
            raise ValueError(f"not one exact versioned soname (libcrypto.so.3): {value!r}")
        return value

    @field_validator("found", "until", mode="before")
    @classmethod
    def _waiver_date_as_text(cls, value: object) -> object:
        # YAML reads an unquoted 2026-11-15 as a date; the fields are free text
        return value.isoformat() if hasattr(value, "isoformat") else value

    @field_validator("why", "found", "until")
    @classmethod
    def _waiver_says(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must say why, where it was found and what ends the waiver")
        return value.strip()


def load_soname_waivers(variants_dir: Path) -> tuple[SonameWaiver, ...]:
    """Read ``variants_dir/soname-waivers.yaml``: a YAML list of entries. ``()`` when
    the file is absent or holds no entry; one entry per package and soname."""
    path = variants_dir / SONAME_WAIVERS_FILE
    if not path.is_file():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, list | None):
            raise TypeError("the top level must be a list of entries")
        waivers = tuple(SonameWaiver.model_validate(entry) for entry in data or ())
    except (OSError, yaml.YAMLError, TypeError, ValueError) as err:
        raise SonameWaiverError(f"{path}: {err}") from err
    keys = [(w.package, w.soname) for w in waivers]
    dupes = sorted({" ".join(key) for key in keys if keys.count(key) > 1})
    if dupes:
        raise SonameWaiverError(f"{path}: one entry per package and soname; repeated: {dupes}")
    return waivers


@dataclass(frozen=True)
class SonameWaiverVerdict:
    """One judgement's soname findings, split by the waivers in force.

    ``stale``: the findings no waiver covers, in the order given -- the only ones the
    caller acts on. ``waived``: each covered finding with its waiver. ``unused``: the
    waivers that covered nothing in THIS judgement (a plan without the package, or a
    package that no longer needs the soname: the exit condition, met).
    """

    stale: tuple[binpkgs.SonameStale, ...]
    waived: tuple[tuple[binpkgs.SonameStale, SonameWaiver], ...]
    unused: tuple[SonameWaiver, ...]

    def audit_fields(self) -> dict[str, object]:
        """What the audit trail keeps of the waivers, beside the soname findings. Pure."""
        return {
            "soname_waived": [
                {
                    "cpv": finding.instance.cpv,
                    "build_id": finding.instance.build_id,
                    "needs": finding.needs.name,
                    "offered": list(finding.offered),
                    "why": waiver.why,
                    "until": waiver.until,
                }
                for finding, waiver in self.waived
            ],
            "soname_waivers_unused": [
                {"package": w.package, "soname": w.soname, "until": w.until} for w in self.unused
            ],
        }


def apply_soname_waivers(
    found: Iterable[binpkgs.SonameStale], waivers: Sequence[SonameWaiver]
) -> SonameWaiverVerdict:
    """Split soname findings into the stale and the waived. Pure.

    A finding is waived when its instance's package (``cat/pkg``, any version, any
    build) AND the soname it needs are an entry's, both exactly. An instance with a
    waived finding and another one keeps the other: it stays stale.
    """
    by_key = {(w.package, w.soname): w for w in waivers}
    stale: list[binpkgs.SonameStale] = []
    waived: list[tuple[binpkgs.SonameStale, SonameWaiver]] = []
    for finding in found:
        waiver = by_key.get((binpkgs.package_of(finding.instance.cpv), finding.needs.name))
        if waiver is None:
            stale.append(finding)
        else:
            waived.append((finding, waiver))
    used = {waiver for _finding, waiver in waived}
    return SonameWaiverVerdict(
        stale=tuple(stale),
        waived=tuple(waived),
        unused=tuple(w for w in waivers if w not in used),
    )
