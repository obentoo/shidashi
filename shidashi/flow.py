"""The build flow as data -- ``variants/flow.yaml``.

What a build does, phase by phase and step by step, is written in
``variants/flow.yaml``; this module only knows the step KINDS and validates the
file. Each kind is executed by the phase that owns it (:mod:`shidashi.bootstrap`
runs the bootstrap's steps). Reading the YAML tells what happens and in which
order; editing it changes the process without touching Python.

Step kinds of the bootstrap phase:

- ``locale``: generate the locales of ``/etc/locale.gen``, select one BY NAME;
- ``emerge``: ``emerge --oneshot <atoms>`` with the phase's ``env``; with
  ``keep_locales: true`` the step fails if ``locale -a`` shrinks;
- ``select-toolchain``: switch ``binutils`` or ``gcc`` to its newest installed
  slot, by name (F8, F9);
- ``assert-world-empty``: fail if anything entered the world file.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_STRICT = ConfigDict(frozen=True, extra="forbid")

#: Where the flow lives, relative to the variants directory.
FLOW_FILE = "flow.yaml"


class FlowError(Exception):
    """A flow.yaml that cannot be read or does not validate."""


class _Step(BaseModel):
    model_config = _STRICT
    #: Shown in logs and errors (``bootstrap:<name>``); several steps may share it.
    name: str


class LocaleStep(_Step):
    do: Literal["locale"]
    locale: str


class EmergeStep(_Step):
    do: Literal["emerge"]
    atoms: tuple[str, ...] = Field(min_length=1)
    keep_locales: bool = False


class SelectToolchainStep(_Step):
    do: Literal["select-toolchain"]
    tool: Literal["binutils", "gcc"]


class AssertWorldEmptyStep(_Step):
    do: Literal["assert-world-empty"]


BootstrapStep = Annotated[
    LocaleStep | EmergeStep | SelectToolchainStep | AssertWorldEmptyStep,
    Field(discriminator="do"),
]


class BootstrapFlow(BaseModel):
    """The toolchain bootstrap: its environment and its steps, in order."""

    model_config = _STRICT
    #: Put in front of every emerge of the phase (``env K=V emerge ...``):
    #: systemd-nspawn does not pass the host's environment in.
    env: dict[str, str] = {}
    steps: tuple[BootstrapStep, ...] = Field(min_length=1)


class Flow(BaseModel):
    model_config = _STRICT
    bootstrap: BootstrapFlow


def load_flow(variants_dir: Path) -> Flow:
    """Read and validate ``variants_dir/flow.yaml``."""
    path = variants_dir / FLOW_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Flow.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as err:
        raise FlowError(f"{path}: {err}") from err
