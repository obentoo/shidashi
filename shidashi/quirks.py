"""The quirks registry -- per-package exceptions to a layer's defaults, in one file.

A layer's ``quirks.yaml`` lists the packages that cannot build with the defaults
of its ``make.conf``, one entry each, with the effect, the reason and the
evidence. It exists so that the exceptions can be read and audited in one place;
before it, each one was a ``package.env`` line, an ``env/`` file and a comment
spread over three directories.

Each entry has an ``atom`` and at least one effect:

- ``features``: FEATURES tokens applied to that atom only (``-ccache``,
  ``-network-sandbox``, ``protect-owned``...), rendered as an ``env/`` file and a
  ``package.env`` line;
- ``mask: true``: the atom goes into ``package.mask``.

:func:`render_quirks` turns the entries into the Portage files that
:func:`shidashi.resolve.apply_portage` writes next to the layer's own, marked as
generated. The YAML is the only thing to edit.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_STRICT = ConfigDict(frozen=True, extra="forbid")

#: A FEATURES token, optionally negated.
_FEATURE = re.compile(r"^-?[a-z0-9][a-z0-9-]*$")

#: Where the rendered files go, relative to /etc/portage. The names sort after
#: the hand-written files of a layer and cannot collide with them.
PACKAGE_ENV = "package.env/zz-quirks"
PACKAGE_MASK = "package.mask/zz-quirks"
ENV_PREFIX = "env/quirk-"


class QuirksError(Exception):
    """A quirks.yaml that cannot be read or does not validate."""


class Quirk(BaseModel):
    """One package that departs from the layer's defaults, and why."""

    model_config = _STRICT
    atom: str
    features: tuple[str, ...] = ()
    mask: bool = False
    why: str
    found: str

    @field_validator("atom")
    @classmethod
    def _atom_shape(cls, value: str) -> str:
        if not re.match(r"^[A-Za-z0-9<>=~!*][^\s]*/[^\s]+$", value):
            raise ValueError(f"not a package atom: {value!r}")
        return value

    @field_validator("features")
    @classmethod
    def _feature_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bad = [f for f in value if not _FEATURE.match(f)]
        if bad:
            raise ValueError(f"not FEATURES tokens: {bad}")
        return value

    @field_validator("found", mode="before")
    @classmethod
    def _date_as_text(cls, value: object) -> object:
        # YAML reads an unquoted 2026-09-27 as a date; the field is free text
        return value.isoformat() if hasattr(value, "isoformat") else value

    @field_validator("why", "found")
    @classmethod
    def _said(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must say why, and where it was found")
        return value

    @model_validator(mode="after")
    def _has_an_effect(self) -> Quirk:
        if not self.features and not self.mask:
            raise ValueError(f"{self.atom}: a quirk needs `features` or `mask: true`")
        return self

    @property
    def env_file(self) -> str:
        """The ``env/`` file name for this atom: ``quirk-dev-java_openjdk.conf``."""
        slug = re.sub(r"[^A-Za-z0-9.-]+", "_", self.atom).strip("_")
        return f"quirk-{slug}.conf"


def load_quirks(path: Path) -> tuple[Quirk, ...]:
    """Read a ``quirks.yaml``: a YAML list of entries. ``()`` when the file is absent."""
    if not path.is_file():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            raise TypeError("the top level must be a list of entries")
        quirks = tuple(Quirk(**entry) for entry in data)
    except (OSError, yaml.YAMLError, TypeError, ValueError) as err:
        raise QuirksError(f"{path}: {err}") from err
    atoms = [q.atom for q in quirks]
    dupes = sorted({a for a in atoms if atoms.count(a) > 1})
    if dupes:
        raise QuirksError(f"{path}: one entry per atom; repeated: {dupes}")
    return quirks


def _comment(text: str) -> str:
    return "\n".join(f"# {line}".rstrip() for line in text.strip().splitlines())


def render_quirks(quirks: tuple[Quirk, ...], *, source: str) -> dict[str, str]:
    """The Portage files for ``quirks``, by path relative to ``/etc/portage``. Pure.

    ``source`` names the YAML in the generated headers, so whoever reads
    ``/etc/portage`` in an image knows where to edit.
    """
    header = f"# GENERATED from {source} by shidashi -- edit that file, not this one.\n"
    files: dict[str, str] = {}
    env_lines: list[str] = []
    mask_lines: list[str] = []
    for q in quirks:
        if q.features:
            files[f"{ENV_PREFIX}{q.env_file[len('quirk-') :]}"] = (
                f"{header}# {q.atom} -- found: {q.found}\n{_comment(q.why)}\n"
                f'FEATURES="{" ".join(q.features)}"\n'
            )
            env_lines.append(f"{q.atom}  {q.env_file}")
        if q.mask:
            mask_lines.append(f"# {q.found}\n{_comment(q.why)}\n{q.atom}")
    if env_lines:
        files[PACKAGE_ENV] = header + "\n".join(env_lines) + "\n"
    if mask_lines:
        files[PACKAGE_MASK] = header + "\n\n".join(mask_lines) + "\n"
    return files
