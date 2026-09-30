"""Unit tests of shidashi.bootstrap -- the toolchain bootstrap (BOOTSTRAP-PROCESS §1).

A fake container records every argv and plays the stage3: merging binutils or
gcc drops a new slot into /etc/env.d, as the real ebuilds do, so the tests
prove the NEWEST one is selected by name after the merge.
"""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from shidashi import config
from shidashi.bootstrap import (
    BootstrapError,
    emerge_argv,
    newest_profile,
    run_bootstrap,
)
from shidashi.container import CommandResult
from shidashi.flow import load_flow

# The bootstrap's steps come from variants/flow.yaml. The expectations below are
# LITERAL on purpose: they are the command sequence the lab ran and the Python
# implementation reproduced before the steps moved into the YAML.
_FEATURES = "FEATURES=-buildpkg -ccache"


def _emerge(*atoms: str) -> list[str]:
    return ["env", _FEATURES, "emerge", "--oneshot", *atoms]


# ccache is built with buildpkg on: no later stage rebuilds it, and the ISO
# installs @devel from binpkgs only (2026-09-29).
_CCACHE = ["env", "FEATURES=-ccache", "emerge", "--oneshot", "dev-util/ccache"]


_CHOST = "x86_64-pc-linux-gnu"


class FakeContainer:
    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs
        self.calls: list[list[str]] = []
        self.locales = ["C", "C.utf8", "POSIX", "en_US.utf8", "pt_BR.utf8"]
        self.fail_on: str | None = None
        self.on_emerge: dict[str, Callable[[], None]] = {
            "sys-devel/binutils": lambda: self._slot("binutils", f"{_CHOST}-2.45"),
            "sys-devel/gcc": lambda: self._slot("gcc", f"{_CHOST}-15"),
        }

    def check_generation(self) -> None:
        self.calls.append(["<check-generation>"])

    def _slot(self, kind: str, name: str) -> None:
        (self.rootfs / "etc/env.d" / kind / name).write_text("", encoding="utf-8")

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if self.fail_on is not None and self.fail_on in argv:
            raise subprocess.CalledProcessError(1, argv, output="boom", stderr="")
        for atom, effect in self.on_emerge.items():
            if "emerge" in argv and atom in argv:
                effect()
        if argv == ["locale", "-a"]:
            return CommandResult(0, "\n".join(self.locales) + "\n", "")
        return CommandResult(0, "", "")


@pytest.fixture
def stage3(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    for kind, names in {
        "binutils": [f"{_CHOST}-2.44", f"config-{_CHOST}"],
        "gcc": [f"{_CHOST}-9", f"{_CHOST}-14", f"config-{_CHOST}"],
    }.items():
        d = root / "etc/env.d" / kind
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_text("", encoding="utf-8")
    (root / "etc/locale.gen").write_text(
        "# comment\nen_US.UTF-8 UTF-8\npt_BR.UTF-8 UTF-8\n", encoding="utf-8"
    )
    (root / "var/lib/portage").mkdir(parents=True)
    (root / "var/lib/portage/world").write_text("", encoding="utf-8")
    return root


def test_newest_profile_sorts_versions_naturally_and_skips_config(stage3: Path) -> None:
    # "-9" < "-14": a plain string sort would pick gcc 9
    assert newest_profile(stage3 / "etc/env.d/gcc") == f"{_CHOST}-14"


def test_emerge_argv_is_oneshot_with_the_flows_environment() -> None:
    env = load_flow(config.variants_dir()).bootstrap.env
    assert env == {"FEATURES": "-buildpkg -ccache"}  # D22, F65
    assert emerge_argv("sys-devel/gcc", env=env) == _emerge("sys-devel/gcc")


def test_run_bootstrap_runs_the_lab_sequence_and_selects_the_new_slots(stage3: Path) -> None:
    c = FakeContainer(stage3)
    result = run_bootstrap(c, on_generation=c.check_generation)

    assert result.binutils == f"{_CHOST}-2.45"
    assert result.gcc == f"{_CHOST}-15"
    # the switch happens after the merge, by name
    assert ["binutils-config", f"{_CHOST}-2.45"] in c.calls
    assert ["gcc-config", f"{_CHOST}-15"] in c.calls
    assert c.calls.index(_emerge("sys-devel/gcc")) < c.calls.index(
        ["gcc-config", f"{_CHOST}-15"]
    )
    emerged = [call[4:] for call in c.calls if call[:4] == _emerge()]
    assert emerged == [
        ["sys-kernel/linux-headers", "sys-devel/binutils"],
        ["sys-devel/gcc"],
        ["dev-build/libtool"],
        ["sys-libs/glibc"],
        ["@preserved-rebuild"],
    ]
    assert c.calls[:2] == [["locale-gen"], ["eselect", "locale", "set", "en_US.UTF-8"]]
    assert result.steps == (
        "locale", "binutils", "gcc", "libtool", "glibc", "preserved", "generation", "ccache",
        "world",
    )


def test_run_bootstrap_refuses_a_locale_that_locale_gen_would_not_build(stage3: Path) -> None:
    (stage3 / "etc/locale.gen").write_text("# en_US.UTF-8 UTF-8\n", encoding="utf-8")
    c = FakeContainer(stage3)
    with pytest.raises(BootstrapError, match="not active") as err:
        run_bootstrap(c)
    assert err.value.phase == "bootstrap:locale"
    assert c.calls == []


def test_run_bootstrap_fails_when_glibc_loses_locales(stage3: Path) -> None:
    c = FakeContainer(stage3)
    c.on_emerge["sys-libs/glibc"] = lambda: c.locales.pop()
    with pytest.raises(BootstrapError, match="glibc lost locales: locale -a went 5 -> 4") as err:
        run_bootstrap(c)
    assert err.value.phase == "bootstrap:glibc"


def test_run_bootstrap_fails_when_the_world_file_is_not_empty(stage3: Path) -> None:
    c = FakeContainer(stage3)
    c.on_emerge["dev-util/ccache"] = lambda: (stage3 / "var/lib/portage/world").write_text(
        "dev-util/ccache\n", encoding="utf-8"
    )
    with pytest.raises(BootstrapError, match="1 world entries: dev-util/ccache"):
        run_bootstrap(c, on_generation=c.check_generation)


def test_run_bootstrap_wraps_a_failed_command_with_step_and_transcript(stage3: Path) -> None:
    c = FakeContainer(stage3)
    c.fail_on = "sys-devel/gcc"
    with pytest.raises(BootstrapError) as err:
        run_bootstrap(c)
    assert err.value.phase == "bootstrap:gcc"
    assert "boom" in err.value.output
    assert "[binutils] binutils-config" in err.value.output  # the transcript so far


def test_the_flow_reproduces_the_whole_command_sequence(stage3: Path) -> None:
    """Every command, in order: what bootstrap.py ran before its steps moved
    into variants/flow.yaml (and what the lab's step0-5 + P scripts ran)."""
    c = FakeContainer(stage3)
    run_bootstrap(c, on_generation=c.check_generation)
    assert c.calls == [
        ["locale-gen"],
        ["eselect", "locale", "set", "en_US.UTF-8"],
        ["env-update"],
        _emerge("sys-kernel/linux-headers", "sys-devel/binutils"),
        ["binutils-config", f"{_CHOST}-2.45"],
        ["env-update"],
        _emerge("sys-devel/gcc"),
        ["gcc-config", f"{_CHOST}-15"],
        ["env-update"],
        _emerge("dev-build/libtool"),
        ["locale", "-a"],
        _emerge("sys-libs/glibc"),
        ["locale", "-a"],
        _emerge("@preserved-rebuild"),
        # the fingerprint is checked once the toolchain is final, and before the
        # first step that writes a binpkg
        ["<check-generation>"],
        _CCACHE,
    ]


def test_check_generation_without_a_check_to_run_fails_before_ccache(stage3: Path) -> None:
    c = FakeContainer(stage3)
    with pytest.raises(BootstrapError, match="no fingerprint check") as err:
        run_bootstrap(c)
    assert err.value.phase == "bootstrap:generation"
    assert _CCACHE not in c.calls


def test_a_failed_generation_check_stops_the_bootstrap_before_any_binpkg(stage3: Path) -> None:
    c = FakeContainer(stage3)

    def mismatch() -> None:
        raise RuntimeError("PKGDIR belongs to another generation")

    with pytest.raises(RuntimeError, match="another generation"):
        run_bootstrap(c, on_generation=mismatch)
    assert _CCACHE not in c.calls


def test_a_steps_env_goes_over_the_phases_key_by_key(tmp_path: Path) -> None:
    (tmp_path / "flow.yaml").write_text(
        """
bootstrap:
  env: {FEATURES: "-buildpkg -ccache", USE: "-doc"}
  steps:
    - {name: g, do: check-generation}
    - {name: c, do: emerge, atoms: [dev-util/ccache], env: {FEATURES: "-ccache"}}
stages:
  emerge: {options: [], rebuild: [], update: []}
  settle: {options: []}
  steps:
    - {name: a, do: apply-config}
    - {name: w, do: write-cuts}
    - {name: e, do: emerge-stage}
    - {name: s, do: settle}
    - {name: f, do: snapshot}
""",
        encoding="utf-8",
    )
    flow = load_flow(tmp_path).bootstrap
    step = flow.steps[1]
    assert flow.step_env(step) == {"FEATURES": "-ccache", "USE": "-doc"}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "steps",
    [
        # buildpkg on before the fingerprint is known: the binpkg could land in
        # another generation's PKGDIR (D26)
        "    - {name: c, do: emerge, atoms: [x/y], env: {FEATURES: \"-ccache\"}}\n"
        "    - {name: g, do: check-generation}\n",
        "    - {name: g, do: check-generation}\n    - {name: h, do: check-generation}\n",
    ],
)
def test_the_flow_refuses_binpkgs_before_the_generation_check(
    tmp_path: Path, steps: str
) -> None:
    from shidashi.flow import FlowError

    (tmp_path / "flow.yaml").write_text(
        "bootstrap:\n  env: {FEATURES: \"-buildpkg\"}\n  steps:\n" + steps, encoding="utf-8"
    )
    with pytest.raises(FlowError, match="check-generation"):
        load_flow(tmp_path)


def test_the_flow_refuses_an_unknown_step_kind(tmp_path: Path) -> None:
    from shidashi.flow import FlowError

    (tmp_path / "flow.yaml").write_text(
        "bootstrap:\n  steps:\n    - {name: x, do: reboot}\n", encoding="utf-8"
    )
    with pytest.raises(FlowError, match="reboot"):
        load_flow(tmp_path)
