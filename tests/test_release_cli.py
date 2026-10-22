"""``shidashi release`` end to end (story 030, task 5.1; R1.3-R1.8, R2.1-R2.4, R3.7,
R4.1-R4.4, R5.1, R5.2, R6.1-R6.3, R6.5, R6.6, R7.1-R7.6, R8.1-R8.5, R9.1, R9.2).

Contract from design.md "shidashi/release_cli.py" and "Data Models": ``release [ARCH]
[INIT] [--all] --key FPR [--output-root DIR] [pass-through build options]``. Preflight
before any build: usage (exit 2), the axes (exit 1), ``gpgsign.check_key`` (exit 1), a
release directory that already holds ``SHA256SUMS.asc`` (exit 1). Then one audited run:
the upstream check per init, one child build per variant (``release.run_build``, one at a
time, each writing ``<dir>/.outcome/<arch>-<init>.json``), one publish per host-owned arch
that built (``publish_pool.publish``), ``release.json`` -> ``SHA256SUMS``, the key, the
signatures, their check, the summary and the exit code.

Driven through the real CLI (typer ``CliRunner``). The seams faked are the ones the design
names: the child build (``FakeChildBuilds`` -- it writes ISOs, DIGESTS, SHA256SUMS lines and
the outcome file the way a build does), ``publish_pool.publish`` (``FakePublisher``), the
cache owner and the generation PKGDIR of ``cli``, the maintainer's gpg
(``FakeMaintainerGpg`` of tests/test_release_core.py) and the network (``urlopen`` refuses;
``FakeUpstream`` where a test wants a newer stage3). The recipe axes and the pins are the
real ``variants/`` and ``seeds/``.
"""

import datetime
import importlib
import inspect
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from shidashi import audit, cli, config, publish, publish_pool, seed
from shidashi.binhost import BinhostError
from shidashi.generation import FINGERPRINT_FILE, GenerationFingerprint
from shidashi.tree import load_overlay_pins, load_tree_pin
from tests._pending import try_import
from tests.test_release_core import (
    FPR,
    KEY_FILE,
    LATEST,
    STRANGER,
    FakeMaintainerGpg,
    fake_signature,
    listed,
)

UpstreamStage3: Any = try_import("shidashi.upstream", "UpstreamStage3")

runner = CliRunner()

TOOLCHAIN = {"gcc": "15.2.1", "glibc": "2.42", "binutils": "2.45"}
WORKER = "bentoo-lab"


def pin() -> seed.Stage3Pointer:
    return seed.load_pointer("systemd", seeds_dir=config.seeds_dir())


def all_pairs() -> list[tuple[str, str]]:
    return [(a, i) for a in config.available_names("arch") for i in config.available_names("init")]


def utc_day() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y.%m.%d")


# --- fakes -------------------------------------------------------------------------------------


@dataclass
class ChildCall:
    arch: str
    init: str
    argv: list[str]
    out: Path
    outcome: Path


class FakeChildBuilds:
    """Stands in for ``release.run_build``: one child ``shidashi build`` per call.

    ``script`` maps ``(arch, init)`` to how that build ends: ``ok`` (two ISOs), ``refused``
    (a toolchain refusal), ``images-failed``, ``killed`` (exit 137, no outcome file),
    ``corrupt`` (half an outcome file), ``extra-key`` (an outcome of the wrong shape),
    ``interrupt`` (Ctrl-C while it runs) or ``sigterm`` (SIGTERM while it runs)."""

    def __init__(self, pkgdir_of: Any) -> None:
        self.script: dict[tuple[str, str], str] = {}
        self.calls: list[ChildCall] = []
        self.running = False
        self.overlapped = False
        self.pkgdir_of = pkgdir_of

    def isos(self, out: Path, arch: str, init: str) -> list[str]:
        return [
            f"bentoo-{out.name}-minimal-{init}-{arch}.iso",
            f"bentoo-{out.name}-kde-{init}-{arch}.iso",
        ]

    def __call__(self, argv: Sequence[Any], *_a: Any, **_k: Any) -> int:
        argv = [str(a) for a in argv]
        at = argv.index("build")
        arch, init = argv[at + 1], argv[at + 2]
        out = Path(argv[argv.index("-o") + 1])
        outcome = Path(argv[argv.index("--outcome-file") + 1])
        self.calls.append(ChildCall(arch, init, argv, out, outcome))
        if self.running:
            self.overlapped = True
        self.running = True
        try:
            return self._build(arch, init, out, outcome, self.script.get((arch, init), "ok"))
        finally:
            self.running = False

    def _build(self, arch: str, init: str, out: Path, outcome: Path, how: str) -> int:
        if how == "interrupt":
            raise KeyboardInterrupt
        if how == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
            return 143
        if how == "killed":
            return 137
        fields: dict[str, Any] = {
            "arch": arch,
            "init": init,
            "status": "ok",
            "exit_code": 0,
            "isos": [],
            "failed_images": [],
            "message": "",
            "toolchain": None,
        }
        made = self.isos(out, arch, init)
        if how == "ok":
            fields["isos"] = made
        elif how == "images-failed":
            made = made[:1]
            fields.update(status="images-failed", exit_code=1, isos=made, failed_images=["kde"])
            fields["message"] = "image(s) failed: kde"
        elif how == "refused":
            made = []
            pkgdir = self.pkgdir_of(arch, init)
            fields.update(status="toolchain-refused", exit_code=1)
            fields["message"] = f"{pkgdir} belongs to another generation"
            fields["toolchain"] = {
                "differences": ["gcc 15.2.1 -> 16.1.0"],
                "pkgdir": str(pkgdir),
                "successor": f"{pkgdir}-gcc16",
            }
        sums: dict[str, str] = {}
        for name in made:
            iso = out / name
            iso.write_bytes(f"iso {name}".encode())
            digests, sha256 = publish.write_digests(iso)
            sums[name] = sha256
            sums[digests.name] = publish.sha256_of(digests)
        if sums:
            publish.update_sha256sums(out, sums)
        if how == "corrupt":
            outcome.write_text('{"arch": "' + arch, encoding="utf-8")
            return 1
        if how == "extra-key":
            outcome.write_text(json.dumps({**fields, "signed_by": "x"}), encoding="utf-8")
            return 0
        outcome.write_text(json.dumps(fields), encoding="utf-8")
        return int(fields["exit_code"])


@dataclass
class FakePublisher:
    """Stands in for ``publish_pool.publish``: records each call; ``fail`` arches raise."""

    fail: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, build_pool: Path, dest: Path, *, key: str, **_kw: Any) -> Any:
        arch = Path(build_pool).parent.name
        self.calls.append(
            {"arch": arch, "build_pool": Path(build_pool), "dest": Path(dest), "key": key}
        )
        if arch in self.fail:
            raise BinhostError("the gpg-agent extra socket is gone")
        return publish_pool.PublishResult(dest=Path(dest), published=2144, skipped_unrecorded=3)

    def arches(self) -> list[str]:
        return [c["arch"] for c in self.calls]


class FakeUpstream:
    """Stands in for ``upstream.check``: a newer stage3 for every init."""

    def __init__(self, latest: str = LATEST) -> None:
        self.latest = latest
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, init: str, *, download: bool, **_kw: Any) -> Any:
        self.calls.append((init, download))
        return UpstreamStage3(init=init, pinned=pin().snapshot, latest=self.latest, note="")


def patch_everywhere(monkeypatch: pytest.MonkeyPatch, module: str, name: str, fake: Any) -> None:
    """``module.name``, and the same name in the release modules if they bound it."""
    monkeypatch.setattr(importlib.import_module(module), name, fake)
    for other in ("shidashi.release", "shidashi.release_cli"):
        try:
            bound = importlib.import_module(other)
        except ImportError:
            continue
        # ``from shidashi import publish`` binds a module of the same name: never that
        current = getattr(bound, name, None)
        if other != module and callable(current) and not inspect.ismodule(current):
            monkeypatch.setattr(bound, name, fake)


@dataclass
class Lab:
    root: Path
    cache: Path
    releases: Path
    builds: FakeChildBuilds
    publisher: FakePublisher
    maintainer: FakeMaintainerGpg
    owners: dict[str, str]
    fetched: list[str]

    def pkgdir(self, arch: str, _init: str = "systemd") -> Path:
        return self.cache / "binpkgs" / arch / pin().snapshot

    def release(self, *args: str, root: bool = True) -> Result:
        extra = ["--output-root", str(self.releases)] if root else []
        return runner.invoke(cli.app, ["release", *args, *extra])

    def the_release(self) -> Path:
        """The one release directory made, named by the UTC date of the run."""
        made = [p for p in self.releases.iterdir() if p.is_dir()] if self.releases.is_dir() else []
        assert len(made) == 1, made
        return made[0]

    def manifest(self) -> dict[str, Any]:
        path = self.the_release() / "release.json"
        assert path.is_file(), "no release.json"
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def variant(self, arch: str, init: str) -> dict[str, Any]:
        found: list[dict[str, Any]] = [
            v for v in self.manifest()["variants"] if (v["arch"], v["init"]) == (arch, init)
        ]
        assert len(found) == 1, self.manifest()["variants"]
        return found[0]


def line_of(output: str, arch: str, init: str) -> str:
    pattern = re.compile(rf"(?<![\w-]){re.escape(arch)}\W+{re.escape(init)}(?![\w-])")
    mine = [line for line in output.splitlines() if pattern.search(line)]
    assert len(mine) == 1, (arch, init, output)
    return mine[0]


def signatures(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.asc") if p.name != KEY_FILE)


def release_lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    """The release's seams faked, under ``tmp_path`` (shared with the contract tests)."""
    cache = tmp_path / "cache"
    for name in ("CACHE", "SCRATCH"):
        monkeypatch.setenv(f"SHIDASHI_{name}", str(tmp_path / name.lower()))
    monkeypatch.delenv("SHIDASHI_JOBS", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(cli._err_console, "_width", 10_000, raising=False)

    def pkgdir_of(arch: str, _init: str) -> Path:
        where = cache / "binpkgs" / arch / pin().snapshot
        if not (where / FINGERPRINT_FILE).is_file():
            where.mkdir(parents=True, exist_ok=True)
            fingerprint = GenerationFingerprint(
                arch=arch,
                profile="default/linux/amd64/23.0/no-multilib/systemd",
                common_flags="-O2 -pipe",
                chost="x86_64-pc-linux-gnu",
                llvm_slot="21",
                **TOOLCHAIN,
            )
            (where / FINGERPRINT_FILE).write_text(fingerprint.model_dump_json(), encoding="utf-8")
        return where

    owners: dict[str, str] = {}
    monkeypatch.setattr(cli, "_generation_pkgdir", pkgdir_of)
    monkeypatch.setattr(cli, "_cache_owner", lambda arch: owners.get(arch))

    builds = FakeChildBuilds(pkgdir_of)
    patch_everywhere(monkeypatch, "shidashi.release", "run_build", builds)
    publisher = FakePublisher()
    patch_everywhere(monkeypatch, "shidashi.publish_pool", "publish", publisher)
    maintainer = FakeMaintainerGpg().install(monkeypatch)

    fetched: list[str] = []

    def offline(url: Any, *_a: Any, **_k: Any) -> Any:
        fetched.append(str(getattr(url, "full_url", url)))
        raise urllib.error.URLError("the network is off in tests")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    upstream_module = sys.modules.get("shidashi.upstream")
    if upstream_module is not None and hasattr(upstream_module, "urlopen"):
        monkeypatch.setattr(upstream_module, "urlopen", offline)

    return Lab(
        tmp_path, cache, tmp_path / "releases", builds, publisher, maintainer, owners, fetched
    )


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return release_lab(tmp_path, monkeypatch)


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# --- preflight (R1.3-R1.7) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--all", "v3"], id="all-and-arch"),
        pytest.param(["--all", "v3", "systemd"], id="all-and-both"),
        pytest.param(["v3"], id="arch-alone"),
        pytest.param([], id="nothing"),
    ],
)
def test_a_usage_error_exits_2_before_any_build(lab: Lab, args: list[str]) -> None:
    """R1.3."""
    result = lab.release(*args, "--key", FPR)
    assert result.exit_code == 2, result.output
    assert lab.builds.calls == []
    assert not lab.releases.exists() or not any(lab.releases.iterdir())


def test_a_missing_key_exits_2_and_the_same_command_with_it_builds(lab: Lab) -> None:
    """R1.5, paired: the refusal is the missing key's, not every release's."""
    refused = lab.release("v3", "systemd")
    assert refused.exit_code == 2, refused.output
    assert lab.builds.calls == []
    accepted = lab.release("v3", "systemd", "--key", FPR)
    assert [(c.arch, c.init) for c in lab.builds.calls] == [("v3", "systemd")], accepted.output


@pytest.mark.parametrize(
    ("arch", "init", "unknown"),
    [
        pytest.param("v5", "systemd", "v5", id="arch"),
        pytest.param("v3", "runit", "runit", id="init"),
    ],
)
def test_an_axis_value_with_no_recipe_exits_1_naming_it_before_any_build(
    lab: Lab, arch: str, init: str, unknown: str
) -> None:
    """R1.4."""
    result = lab.release(arch, init, "--key", FPR)
    assert result.exit_code == 1, result.output
    assert unknown in result.output
    assert lab.builds.calls == []


def test_a_key_the_maintainer_has_no_secret_for_exits_1_naming_it_before_any_build(
    lab: Lab,
) -> None:
    """R1.6 (no agent is gpgsign.check_key's, tests/test_gpgsign.py)."""
    result = lab.release("v3", "systemd", "--key", STRANGER)
    assert result.exit_code == 1, result.output
    assert STRANGER in result.output
    assert lab.maintainer.checked == [STRANGER]
    assert lab.builds.calls == []
    assert signatures(lab.root) == []


def test_an_already_signed_release_directory_is_refused_and_left_byte_for_byte(
    lab: Lab,
) -> None:
    """R1.7: a published release is never silently replaced."""
    day = utc_day()
    signed = lab.releases / day
    signed.mkdir(parents=True)
    (signed / "bentoo-x-kde-systemd-v3.iso").write_bytes(b"last week")
    (signed / "SHA256SUMS").write_text(
        "0" * 64 + "  bentoo-x-kde-systemd-v3.iso\n", encoding="utf-8"
    )
    (signed / "SHA256SUMS.asc").write_text("signed", encoding="utf-8")
    before = snapshot(signed)
    result = lab.release("v3", "systemd", "--key", FPR)
    if utc_day() != day:
        pytest.skip("the UTC date changed during the test")
    assert result.exit_code == 1, result.output
    assert str(signed) in "".join(result.output.split()) or day in result.output
    assert lab.builds.calls == []
    assert snapshot(signed) == before


def test_yesterdays_signed_release_does_not_block_todays(lab: Lab) -> None:
    """Hostile half (collapse): only the directory of this release counts."""
    yesterday = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).strftime(
        "%Y.%m.%d"
    )
    old = lab.releases / yesterday
    old.mkdir(parents=True)
    (old / "SHA256SUMS").write_text("", encoding="utf-8")
    (old / "SHA256SUMS.asc").write_text("signed", encoding="utf-8")
    before = snapshot(old)
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert [(c.arch, c.init) for c in lab.builds.calls] == [("v3", "systemd")]
    assert snapshot(old) == before


@pytest.fixture
def far_timezones() -> Iterator[list[str]]:
    """UTC+14 and UTC-12: at any hour one of them is on another date than UTC."""
    was = os.environ.get("TZ")
    yield ["Etc/GMT-14", "Etc/GMT+12"]
    if was is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = was
    time.tzset()


def test_the_release_directory_is_the_utc_start_date_under_cache_releases(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, far_timezones: list[str]
) -> None:
    """R1.8: ``<cache>/releases/<YYYY.MM.DD>`` of the start in UTC, whatever the local zone."""
    for n, zone in enumerate(far_timezones):
        cache = lab.root / f"cache-{n}"
        monkeypatch.setenv("SHIDASHI_CACHE", str(cache))
        os.environ["TZ"] = zone
        time.tzset()
        before = utc_day()
        result = lab.release("v3", "systemd", "--key", FPR, root=False)
        after = utc_day()
        assert result.exit_code == 0, (zone, result.output)
        made = (
            sorted(p.name for p in (cache / "releases").iterdir())
            if (cache / "releases").is_dir()
            else []
        )
        assert made in ([before], [after]), (zone, made)


# --- one child build per variant (R1.1, R1.2, R2.1-R2.4) --------------------------------------


def test_one_variant_is_one_child_build_of_every_image_into_the_release_directory(
    lab: Lab,
) -> None:
    """R1.1, R2.1."""
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert len(lab.builds.calls) == 1, lab.builds.calls
    (call,) = lab.builds.calls
    directory = lab.the_release()
    assert call.argv[0] == sys.executable
    at = call.argv.index("build")
    assert call.argv[at : at + 5] == ["build", "v3", "systemd", "--images", "all"]
    assert call.out == directory
    assert call.outcome == directory / ".outcome" / "v3-systemd.json"


def test_all_builds_every_arch_and_init_in_sorted_order_one_at_a_time(lab: Lab) -> None:
    """R1.2, R2.3."""
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert [(c.arch, c.init) for c in lab.builds.calls] == sorted(all_pairs())
    assert len(all_pairs()) > 1 and not lab.builds.overlapped


@pytest.mark.usefixtures("qemu_host")
def test_every_pass_through_option_reaches_every_child_unchanged(lab: Lab) -> None:
    """R2.2."""
    work = lab.root / "work"
    options = [
        ["--boot-test"],
        ["--jobs", "8"],
        ["--compression", "both"],
        ["--no-sbom"],
        ["--no-download"],
        ["--local"],
        ["--allow-dirty"],
        ["--work-dir", str(work)],
        ["--verbose"],
    ]
    flat = [token for option in options for token in option]
    lab.release("--all", "--key", FPR, *flat)
    assert len(lab.builds.calls) == len(all_pairs())
    for call in lab.builds.calls:
        tail = call.argv[call.argv.index("--outcome-file") + 2 :]
        for option in options:
            spots = [i for i in range(len(tail)) if tail[i : i + len(option)] == option]
            assert spots, (option, call.argv)
        assert "--key" not in tail and FPR not in tail, "the key reached a child build"


def test_a_failed_variant_does_not_stop_the_next(lab: Lab) -> None:
    """R2.4."""
    first = sorted(all_pairs())[0]
    lab.builds.script[first] = "killed"
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert [(c.arch, c.init) for c in lab.builds.calls] == sorted(all_pairs())


# --- outcome files that cannot be trusted (R3.7) -----------------------------------------------


@pytest.mark.parametrize(
    ("how", "code"),
    [
        pytest.param("killed", 137, id="no-outcome"),
        pytest.param("corrupt", None, id="half-written"),
        pytest.param("extra-key", None, id="wrong-shape"),
    ],
)
def test_a_child_whose_outcome_cannot_be_read_is_an_error_with_its_exit_code(
    lab: Lab, how: str, code: int | None
) -> None:
    lab.builds.script[("v3", "systemd")] = how
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    entry = lab.variant("v3", "systemd")
    assert entry["status"] == "error"
    if code is not None:  # 137 appears nowhere else in the entry
        assert str(code) in json.dumps(entry), entry
    assert "error" in line_of(result.output, "v3", "systemd")


def test_a_stale_outcome_from_an_earlier_run_is_never_read_as_this_childs(lab: Lab) -> None:
    """Hostile (a third file): a rerun into the same unsigned directory finds last run's
    ``ok`` outcome; this child was killed before writing its own."""
    day = lab.releases / utc_day()
    (day / ".outcome").mkdir(parents=True)
    (day / ".outcome" / "v3-systemd.json").write_text(
        json.dumps(
            {
                "arch": "v3",
                "init": "systemd",
                "status": "ok",
                "exit_code": 0,
                "isos": [],
                "failed_images": [],
                "message": "",
                "toolchain": None,
            }
        ),
        encoding="utf-8",
    )
    lab.builds.script[("v3", "systemd")] = "killed"
    result = lab.release("v3", "systemd", "--key", FPR)
    assert lab.builds.calls, result.output
    assert result.exit_code == 1, result.output
    assert lab.variant("v3", "systemd")["status"] == "error"


# --- publish (R4.1-R4.4) ----------------------------------------------------------------------


def test_each_arch_that_built_is_published_once_into_its_generations_pool(lab: Lab) -> None:
    """R4.1, hostile half (split): two ``ok`` variants of one arch share one build pool --
    one publish per arch, never one per variant."""
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 0, result.output
    arches = sorted({a for a, _i in all_pairs()})
    assert sorted(lab.publisher.arches()) == arches
    for call in lab.publisher.calls:
        assert call["key"] == FPR
        assert call["build_pool"] == lab.pkgdir(call["arch"])
        assert call["dest"] == lab.cache / "publish" / call["arch"] / pin().snapshot


def test_an_arch_is_published_when_one_variant_built_and_not_when_none_did(lab: Lab) -> None:
    """R4.3, both halves: a failed sibling does not hold back an arch that built; an arch
    with nothing built is not published."""
    arches = sorted({a for a, _i in all_pairs()})
    inits = sorted({i for _a, i in all_pairs()})
    nothing_built, half_built = arches[0], arches[1]
    for init in inits:
        lab.builds.script[(nothing_built, init)] = "images-failed"
    lab.builds.script[(half_built, inits[0])] = "refused"
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert half_built in lab.publisher.arches()
    assert nothing_built not in lab.publisher.arches()
    assert lab.variant(nothing_built, inits[0])["publish"].startswith("not run")


def test_a_worker_owned_arch_builds_skips_its_publish_naming_the_owner_and_exits_0(
    lab: Lab,
) -> None:
    """R4.2, R8.3."""
    lab.owners["znver5"] = WORKER
    result = lab.release("znver5", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert [(c.arch, c.init) for c in lab.builds.calls] == [("znver5", "systemd")]
    assert lab.publisher.calls == []
    assert WORKER in line_of(result.output, "znver5", "systemd")
    entry = lab.variant("znver5", "systemd")
    assert entry["publish"].startswith("skipped") and WORKER in entry["publish"]
    assert (lab.the_release() / "SHA256SUMS.asc").is_file()


def test_a_publish_that_fails_is_recorded_the_release_still_signed_and_exit_1(lab: Lab) -> None:
    """R4.4, R8.4."""
    lab.publisher.fail.add("v3")
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 1, result.output
    entry = lab.variant("v3", "systemd")
    assert entry["publish"].startswith("failed") and "extra socket is gone" in entry["publish"]
    assert "extra socket is gone" in line_of(result.output, "v3", "systemd")
    assert (lab.the_release() / "SHA256SUMS.asc").is_file()


# --- release.json (R5.1, R5.2) -----------------------------------------------------------------


def test_release_json_records_the_inputs_and_every_variant(lab: Lab) -> None:
    """R5.1: dates, the commit, the pins, the upstream check, and per variant its status,
    ISOs, generation, toolchain, publish result and any toolchain refusal."""
    lab.builds.script[("v3", "openrc")] = "refused"
    started = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    result = lab.release("--all", "--key", FPR)
    finished = datetime.datetime.now(datetime.UTC)
    assert result.exit_code == 1, result.output
    body = lab.manifest()
    directory = lab.the_release()
    assert body["date"] == directory.name.replace(".", "-")

    def when(text: str) -> datetime.datetime:
        assert text.endswith("Z"), text
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))

    assert started <= when(body["started"]) <= when(body["finished"]) <= finished
    repo = audit.repo_state()
    assert body["shidashi"]["commit"] == repo["commit"]
    assert body["shidashi"]["dirty"] == repo["dirty"]

    seeds = config.seeds_dir()
    stage3 = body["pins"]["stage3"]
    assert stage3["snapshot"] == pin().snapshot
    for init in sorted({i for _a, i in all_pairs()}):
        pointer = seed.load_pointer(init, seeds_dir=seeds)
        assert stage3[init]["filename"] == pointer.filename
        assert stage3[init]["sha512"] == pointer.sha512
    tree = load_tree_pin(seeds)
    assert tree.sha512 in json.dumps(body["pins"]["gentoo"])
    assert tree.date in json.dumps(body["pins"]["gentoo"])
    assert body["pins"]["overlays"] == {o.name: o.commit for o in load_overlay_pins(seeds)}
    assert set(body["upstream_stage3"]) == {i for _a, i in all_pairs()}

    assert len(body["variants"]) == len(all_pairs())
    ok = lab.variant("v3", "systemd")
    assert ok["status"] == "ok"
    assert sorted(ok["isos"]) == sorted(lab.builds.isos(directory, "v3", "systemd"))
    assert ok["generation"] == pin().snapshot
    assert ok["toolchain"] == TOOLCHAIN
    assert ok["publish"].startswith("published")
    refused = lab.variant("v3", "openrc")
    assert refused["status"] == "toolchain-refused"
    assert refused["toolchain_refusal"]["successor"] == f"{lab.pkgdir('v3')}-gcc16"
    assert refused["toolchain_refusal"]["differences"] == ["gcc 15.2.1 -> 16.1.0"]
    assert refused["toolchain_refusal"]["pkgdir"] == str(lab.pkgdir("v3"))


# --- signatures (R6.1-R6.3, R6.5, R6.6) --------------------------------------------------------


def test_a_full_success_is_signed_verifiable_with_the_exported_key_and_exits_0(lab: Lab) -> None:
    """R5.2, R6.1-R6.3, R8.3: every line of SHA256SUMS checks; release.json and the key
    are listed, no signature is; every DIGESTS and SHA256SUMS has a signature that checks
    against the exported key."""
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    directory = lab.the_release()
    names = listed(directory)
    assert "release.json" in names and KEY_FILE in names
    assert not [n for n in names if n.endswith(".asc") and n != KEY_FILE]
    for name, digest in names.items():
        assert publish.sha256_of(directory / name) == digest, name
    assert (
        (directory / KEY_FILE)
        .read_text(encoding="utf-8")
        .startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    )
    digests = sorted(p.name for p in directory.glob("*.iso.DIGESTS"))
    assert len(digests) == 2
    assert sorted(p.name for p in signatures(directory)) == sorted(
        [*(f"{d}.asc" for d in digests), "SHA256SUMS.asc"]
    )
    for asc in signatures(directory):
        data = asc.with_name(asc.name.removesuffix(".asc"))
        assert asc.read_text(encoding="utf-8") == fake_signature(FPR, data.read_bytes()), asc
    assert lab.maintainer.signed[-1] == "SHA256SUMS"
    assert "ok" in line_of(result.output, "v3", "systemd")


def test_a_signature_that_cannot_be_made_exits_1_and_leaves_no_sha256sums_asc(lab: Lab) -> None:
    """R6.6."""
    lab.maintainer.fail_on.add("SHA256SUMS")
    result = lab.release("v3", "systemd", "--key", FPR)
    assert lab.builds.calls, result.output
    assert result.exit_code == 1, result.output
    assert not (lab.the_release() / "SHA256SUMS.asc").exists()


def test_a_signature_that_fails_its_check_exits_1_names_it_and_leaves_no_sha256sums_asc(
    lab: Lab,
) -> None:
    """R6.5, R6.6."""
    bad = f"bentoo-{utc_day()}-kde-systemd-v3.iso.DIGESTS"
    lab.maintainer.reject.add(bad)
    result = lab.release("v3", "systemd", "--key", FPR)
    assert lab.builds.calls, result.output
    assert result.exit_code == 1, result.output
    assert bad in result.output
    assert not (lab.the_release() / "SHA256SUMS.asc").exists()


# --- upstream stage3 (R7.1-R7.6) ---------------------------------------------------------------


def test_upstream_is_read_once_per_released_init_and_a_newer_one_is_reported(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7.1, R7.2."""
    fake = FakeUpstream()
    patch_everywhere(monkeypatch, "shidashi.upstream", "check", fake)
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert sorted(fake.calls) == sorted((i, True) for i in {i for _a, i in all_pairs()})
    assert [
        line for line in result.output.splitlines() if LATEST in line and pin().snapshot in line
    ]
    assert lab.manifest()["upstream_stage3"]["systemd"]["latest"] == LATEST


def test_one_variant_reads_only_its_inits_pointer(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeUpstream()
    patch_everywhere(monkeypatch, "shidashi.upstream", "check", fake)
    lab.release("v3", "openrc", "--key", FPR)
    assert fake.calls == [("openrc", True)]


def test_a_newer_upstream_never_changes_what_is_built(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7.3: the child builds the pin; the pin is not rewritten."""
    fake = FakeUpstream()
    patch_everywhere(monkeypatch, "shidashi.upstream", "check", fake)
    stage3 = config.seeds_dir() / "stage3.toml"
    before = stage3.read_bytes()
    lab.release("v3", "systemd", "--key", FPR)
    assert fake.calls and lab.builds.calls
    for call in lab.builds.calls:
        assert not [a for a in call.argv if LATEST in a], call.argv
    assert stage3.read_bytes() == before


def test_no_download_fetches_nothing_and_reports_the_upstream_unknown(lab: Lab) -> None:
    """R7.5, R7.6."""
    result = lab.release("v3", "systemd", "--key", FPR, "--no-download")
    assert result.exit_code == 0, result.output
    assert lab.fetched == []
    upstream = lab.manifest()["upstream_stage3"]["systemd"]
    assert upstream["latest"] is None and "--no-download" in upstream["note"]
    assert [line for line in result.output.splitlines() if "unknown" in line.lower()], result.output


def test_an_unreachable_upstream_is_reported_unknown_and_never_fails_the_release(lab: Lab) -> None:
    """R7.4: the network is off; the release still builds, signs and exits 0."""
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert lab.fetched, "the pointer was never asked for"
    assert lab.manifest()["upstream_stage3"]["systemd"]["latest"] is None
    assert [line for line in result.output.splitlines() if "unknown" in line.lower()], result.output


# --- summary and exit code (R8.1-R8.5) ---------------------------------------------------------


def test_a_refusal_of_the_second_variant_publishes_and_signs_the_first_and_exits_1(
    lab: Lab,
) -> None:
    """Success Metric 2, R8.2, R8.4: the summary names the ABI difference and the
    successor PKGDIR on the refused variant's line."""
    first, second = sorted(all_pairs())[:2]
    assert first[0] == second[0], "the first two variants share an arch"
    lab.builds.script[second] = "refused"
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 1, result.output
    assert first[0] in lab.publisher.arches()
    assert (lab.the_release() / "SHA256SUMS.asc").is_file()
    line = line_of(result.output, *second)
    assert f"{lab.pkgdir(second[0])}-gcc16" in line
    assert "gcc" in line
    assert "ok" in line_of(result.output, *first)


def test_the_release_is_one_audited_run_with_a_step_per_variant_publish_manifest_and_signing(
    lab: Lab,
) -> None:
    """R8.5."""
    result = lab.release("--all", "--key", FPR)
    assert result.exit_code == 0, result.output
    runs = Path(os.environ["SHIDASHI_RUNS"])
    manifests = [json.loads(p.read_text(encoding="utf-8")) for p in runs.glob("*/manifest.json")]
    releases = [m for m in manifests if m.get("command") == "release"]
    assert len(releases) == 1, [m.get("command") for m in manifests]
    steps = [str(s["step"]) for s in releases[0]["steps"]]
    for arch, init in all_pairs():
        assert [s for s in steps if arch in s and init in s], (arch, init, steps)
    for arch in {a for a, _i in all_pairs()}:
        assert [s for s in steps if "publish" in s and arch in s], (arch, steps)
    assert [s for s in steps if "manifest" in s or "release.json" in s], steps
    assert [s for s in steps if "sign" in s], steps


# --- interruption and rerun (R9.1, R9.2) -----------------------------------------------------


def test_ctrl_c_during_a_child_build_signs_nothing_and_exits_non_zero(lab: Lab) -> None:
    """R9.1."""
    first = sorted(all_pairs())[0]
    lab.builds.script[first] = "interrupt"
    result = lab.release("--all", "--key", FPR)
    assert [(c.arch, c.init) for c in lab.builds.calls] == [first]
    assert result.exit_code != 0
    assert signatures(lab.root) == []
    assert lab.maintainer.signed == []


class NoReleaseHandler(Exception):
    """Raised by the test's own SIGTERM handler: the release installed none."""


def test_sigterm_during_a_child_build_signs_nothing_and_exits_non_zero(lab: Lab) -> None:
    """R9.1. The test's own handler stands guard, so a release without one cannot kill the
    test run; it raises ``NoReleaseHandler`` instead."""

    def guard(_signum: int, _frame: Any) -> None:
        raise NoReleaseHandler("the release left SIGTERM to the default handler")

    previous = signal.signal(signal.SIGTERM, guard)
    try:
        first = sorted(all_pairs())[0]
        lab.builds.script[first] = "sigterm"
        result = lab.release("--all", "--key", FPR)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert [(c.arch, c.init) for c in lab.builds.calls] == [first]
    assert not isinstance(result.exception, NoReleaseHandler), result.exception
    assert result.exit_code != 0
    assert signatures(lab.root) == []


def test_a_rerun_into_an_unsigned_directory_keeps_its_files_and_their_lines(lab: Lab) -> None:
    """R9.2: an earlier, unsigned run left another variant's ISO; it stays as it was, keeps
    its SHA256SUMS line, and its DIGESTS is signed with the rest (R6.2)."""
    day = lab.releases / utc_day()
    day.mkdir(parents=True)
    earlier = day / f"bentoo-{day.name}-minimal-openrc-znver5.iso"
    earlier.write_bytes(b"built yesterday evening")
    digests, sha256 = publish.write_digests(earlier)
    publish.update_sha256sums(day, {earlier.name: sha256, digests.name: publish.sha256_of(digests)})
    kept = {
        name: data
        for name, data in snapshot(day).items()
        if name != "SHA256SUMS" and not name.startswith(".")  # the SHA256SUMS lock file
    }
    old_lines = listed(day)
    result = lab.release("v3", "systemd", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert {name: snapshot(day)[name] for name in kept} == kept
    now = listed(day)
    assert {name: now.get(name) for name in old_lines} == old_lines
    assert f"bentoo-{day.name}-kde-systemd-v3.iso" in now
    assert (day / f"{digests.name}.asc").is_file()
    assert (day / "SHA256SUMS.asc").read_text(encoding="utf-8") == fake_signature(
        FPR, (day / "SHA256SUMS").read_bytes()
    )
