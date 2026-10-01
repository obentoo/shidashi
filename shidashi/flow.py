"""The build flow as data -- ``variants/flow.yaml``.

What a build does, phase by phase and step by step, is written in
``variants/flow.yaml``; this module only knows the step KINDS and validates the
file. Each kind is executed by the phase that owns it (:mod:`shidashi.bootstrap`
runs the bootstrap's steps). Reading the YAML tells what happens and in which
order; editing it changes the process without touching Python.

Step kinds of the bootstrap phase:

- ``locale``: generate the locales of ``/etc/locale.gen``, select one BY NAME;
- ``emerge``: ``emerge --oneshot <atoms>`` with the phase's ``env`` and the
  step's own ``env`` over it; with ``keep_locales: true`` the step fails if
  ``locale -a`` shrinks;
- ``check-generation``: record or verify the generation fingerprint of the
  PKGDIR (D26) as soon as the toolchain is final. A step whose ``FEATURES``
  lacks ``-buildpkg`` writes binpkgs, so it must come after this one;
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
- ``snapshot``: the stage's fork point;
- ``check-binpkgs`` (optional): on a stage that ships, resolve the image the
  way the assembler will (``--usepkgonly --emptytree``) with ``--pretend``, so
  an installed package with no binpkg fails the factory, not the ISO.
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
    #: Over the phase's ``env``, key by key.
    env: dict[str, str] = {}


class SelectToolchainStep(_Step):
    do: Literal["select-toolchain"]
    tool: Literal["binutils", "gcc"]


class AssertWorldEmptyStep(_Step):
    do: Literal["assert-world-empty"]


class CheckGenerationStep(_Step):
    do: Literal["check-generation"]


BootstrapStep = Annotated[
    LocaleStep | EmergeStep | SelectToolchainStep | AssertWorldEmptyStep | CheckGenerationStep,
    Field(discriminator="do"),
]


def writes_binpkgs(env: dict[str, str]) -> bool:
    """Whether an emerge under ``env`` writes binpkgs: the base make.conf turns
    ``buildpkg`` on, and only ``FEATURES=-buildpkg`` turns it off. Pure."""
    return "-buildpkg" not in env.get("FEATURES", "").split()


class BootstrapFlow(BaseModel):
    """The toolchain bootstrap: its environment and its steps, in order."""

    model_config = _STRICT
    #: Put in front of every emerge of the phase (``env K=V emerge ...``):
    #: systemd-nspawn does not pass the host's environment in.
    env: dict[str, str] = {}
    steps: tuple[BootstrapStep, ...] = Field(min_length=1)

    def step_env(self, step: EmergeStep) -> dict[str, str]:
        """The environment of one emerge step: the phase's, then the step's."""
        return {**self.env, **step.env}

    @model_validator(mode="after")
    def _binpkgs_after_generation(self) -> BootstrapFlow:
        # A binpkg written before the fingerprint check could land in a PKGDIR
        # of another generation, which Portage would then reuse unchecked (D26).
        checked = False
        for step in self.steps:
            if isinstance(step, CheckGenerationStep):
                if checked:
                    raise ValueError("bootstrap needs `check-generation` at most once")
                checked = True
            elif isinstance(step, EmergeStep) and writes_binpkgs(self.step_env(step)):
                if not checked:
                    raise ValueError(
                        f"bootstrap step {step.name!r} writes binpkgs (FEATURES lacks "
                        "-buildpkg) before `check-generation`"
                    )
        return self


class _StageStep(BaseModel):
    model_config = _STRICT
    name: str
    do: Literal[
        "apply-config", "write-cuts", "emerge-stage", "module-rebuild", "settle", "snapshot",
        "check-binpkgs",
    ]


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
        if kinds.count("module-rebuild") > 1:
            raise ValueError("stages.steps needs `module-rebuild` at most once")
        if "module-rebuild" in kinds and not (
            at < kinds.index("module-rebuild") < kinds.index("snapshot")
        ):
            raise ValueError(
                "module-rebuild must come after emerge-stage and before snapshot: "
                "the fork point must not keep modules for a kernel that is gone"
            )
        if kinds.count("check-binpkgs") > 1:
            raise ValueError("stages.steps needs `check-binpkgs` at most once")
        if "check-binpkgs" in kinds and kinds.index("check-binpkgs") < kinds.index("settle"):
            raise ValueError("check-binpkgs must come after settle: it checks the settled image")
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
