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

Step kinds of every stage of the chain (base → minimal → desktop → flavor), run
by :mod:`shidashi.phases` in the declared order:

- ``apply-config``: the layers in force up to this stage (portage/ + quirks);
- ``write-cuts``: the cycle cuts pending at this stage (``use_break``);
- ``emerge-stage``: the stage's emerge -- ``--emptytree`` for the stage whose
  ``update`` is ``emptytree`` (the base), the ``update`` options otherwise, with
  ``@world`` and the stage's sets; its options are in ``stages.emerge``;
- ``settle``: on a stage that ships, rebuild the cut packages with their final
  USE; its options are in ``stages.settle``;
- ``snapshot``: the stage's fork point.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class _StageStep(BaseModel):
    model_config = _STRICT
    name: str
    do: Literal["apply-config", "write-cuts", "emerge-stage", "settle", "snapshot"]


class StageEmerge(BaseModel):
    """The options of a stage's emerge."""

    model_config = _STRICT
    #: Always passed (``--verbose``, ``--usepkg``...).
    options: tuple[str, ...]
    #: The stage that rebuilds everything (``update: emptytree``, the base).
    rebuild: tuple[str, ...]
    #: Every later stage.
    update: tuple[str, ...]
    #: Put ``@world`` before the stage's sets.
    world: bool = True


class StageSettle(BaseModel):
    model_config = _STRICT
    options: tuple[str, ...]


class StagesFlow(BaseModel):
    """What each stage of the chain does, in order, and with which options."""

    model_config = _STRICT
    emerge: StageEmerge
    settle: StageSettle
    steps: tuple[_StageStep, ...]

    @model_validator(mode="after")
    def _coherent(self) -> StagesFlow:
        kinds = [s.do for s in self.steps]
        for kind in ("apply-config", "write-cuts", "emerge-stage", "settle", "snapshot"):
            if kinds.count(kind) != 1:
                raise ValueError(
                    f"stages.steps needs `{kind}` exactly once, has {kinds.count(kind)}"
                )
        at = kinds.index("emerge-stage")
        if kinds.index("apply-config") > at or kinds.index("write-cuts") > at:
            raise ValueError("apply-config and write-cuts must come before emerge-stage")
        return self

    def split(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The step kinds before the emerge (included) and after it."""
        kinds = tuple(s.do for s in self.steps)
        at = kinds.index("emerge-stage")
        return kinds[: at + 1], kinds[at + 1 :]


class Flow(BaseModel):
    model_config = _STRICT
    bootstrap: BootstrapFlow
    stages: StagesFlow


#: The repository's own flow, next to the package: the default for a variants
#: tree that does not carry one (the flow belongs to the engine, not to a recipe).
_DEFAULT_VARIANTS = Path(__file__).resolve().parent.parent / "variants"


def active_flow(variants_dir: Path) -> Flow:
    """The flow of ``variants_dir``, or the repository's when that tree has none."""
    if (variants_dir / FLOW_FILE).is_file():
        return load_flow(variants_dir)
    return load_flow(_DEFAULT_VARIANTS)


def load_flow(variants_dir: Path) -> Flow:
    """Read and validate ``variants_dir/flow.yaml``."""
    path = variants_dir / FLOW_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Flow.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as err:
        raise FlowError(f"{path}: {err}") from err
