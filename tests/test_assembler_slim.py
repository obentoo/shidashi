"""The assembler's ``slim`` step and the squashfs budget (story 021, task 1.3).

An image whose ``system.yaml`` declares ``slim:`` is reduced, inside its build
container, after ``system`` and before ``live``: its world becomes the keep list (and
its selected sets are emptied, or they would keep everything), depclean removes the
rest, the dropped kernel-module subtrees go and the module index is rebuilt, the
firmware no remaining module asks for goes, the paths are pruned, and every command
the image must run is proved present. After each ``squashfs:<compression>`` the
squashfs is held to the image's budget, before that compression's ISO is made.
Every other image runs exactly the steps it ran before, and the ``packages``
checkpoint stays shared: the slimmed tree is never what a checkpoint holds.

The orchestration runs whole, off-host, in the idiom of tests/test_assembler.py: the
container is a fake that PLAYS the commands on a real directory tree (depclean drops
what the world and the sets no longer reach, ``rm`` removes, ``portageq`` reads the
vdb, ``modinfo`` answers from a table), so the test reads the image's state at each
point instead of trusting a recorded argv.

Requirements exercised: R1.1, R1.2, R1.4, R1.6, R1.7, R2.2, R2.3, R3.2, R3.3, R3.4.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import shidashi.assembler as asm
from shidashi import audit, checkpoint, image, publish, toolbox, world
from shidashi.assembler import Assembler, AssemblerError, iso_emerge_argv
from shidashi.container import CommandResult
from shidashi.recipe import ResolvedRecipe

KVER = "7.2.6-gentoo-dist"
_MODULES = f"usr/lib/modules/{KVER}/kernel"

#: module file (under kernel/) -> the firmware ``modinfo -F firmware`` prints for it
_MODULE_FIRMWARE: dict[str, tuple[str, ...]] = {
    "drivers/gpu/drm/amd/amdgpu/amdgpu.ko.zst": ("amdgpu/navi10_gpu_info.bin",),
    "drivers/net/ethernet/mellanox/mlx5/core/mlx5_core.ko.zst": ("mellanox/mlx5_core.bin",),
    "drivers/net/ethernet/realtek/r8169.ko.xz": ("rtl_nic/rtl8168h-2.fw",),
    "drivers/net/wireless/intel/iwlwifi/iwlwifi.ko.zst": ("iwlwifi-cc-a0-77.ucode",),
    "drivers/nvme/host/nvme.ko": (),
}
_BY_BASENAME = {Path(rel).name: fw for rel, fw in _MODULE_FIRMWARE.items()}

#: vdb entries: category/PF -> SLOT
_VDB = {
    "sys-apps/systemd-261.3": "0",
    "dev-lang/python-3.14.2-r1": "3.14",
    "dev-lang/python-exec-2.4.10": "2",
    "app-misc/unwanted-1.0": "0",
    "sys-libs/glibc-2.42-r2": "2.2",
}
#: what depclean keeps whatever the world says (the profile's system set) ...
_SYSTEM_SET = ("sys-libs/glibc",)
#: ... and the runtime dependencies of what it keeps
_DEPENDS = {"dev-lang/python": ("dev-lang/python-exec",)}
#: the files each package owns, removed with it
_OWNS = {"app-misc/unwanted": ("usr/bin/unwanted", "usr/share/unwanted/data.bin")}

#: The worker's slim section, as the tests declare it.
SLIM: dict[str, Any] = {
    "keep": ["sys-apps/systemd", "dev-lang/python:3.14"],
    "commands": ["/usr/bin/systemd-run", "/usr/bin/python3"],
    # drivers/accel is not in this kernel: skipped, not an error
    "drop_modules": ["drivers/gpu", "drivers/net/ethernet/mellanox", "drivers/accel"],
    "keep_firmware": ["intel-ucode/*", "regulatory.db*"],
    "prune": ["usr/share/doc/**"],
}

_OLD_STEPS = [
    "seed",
    "configure",
    "install",
    "settle",
    "depclean",
    "preserved-rebuild",
    "rootfs",
    "system",
    "live",
    "initramfs",
    "finalize",
    "verify-config",
    "toolbox",
    "sbom",
    "squashfs:zstd",
    "iso:zstd",
    "publish:zstd",
    "cleanup",
]

_PF = re.compile(r"^(?P<pn>.+?)-(?P<pv>\d[^-]*(?:-r\d+)?)$")


def _recipe(flavor: str) -> ResolvedRecipe:
    return ResolvedRecipe(
        arch="znver5",
        flavor=flavor,
        init="systemd",
        profile="default/linux/amd64/23.0/no-multilib/systemd",
        common_flags="-O2",
        goamd64="v4",
        rustflags="",
        cpu_flags_x86=("avx2",),
        tier=1,
        runnable_on_build_host=True,
        sets=(),
        phases=(),
        portage_layers=("base", "arch/znver5", "flavor/kde", "init/systemd"),
    )


def _pointer() -> object:
    from shidashi.seed import Stage3Pointer

    return Stage3Pointer(
        init="systemd",
        base_url="https://distfiles.gentoo.org/x",
        snapshot="20260524T170105Z",
        filename="stage3-amd64-nomultilib-systemd-20260524T170105Z.tar.xz",
        sha512="0" * 128,
    )


def _file(path: Path, data: bytes = b"x", *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _image(rootfs: Path) -> None:
    """A merged-usr image as the install leaves it: a kernel with graphics, Mellanox,
    wired and Wi-Fi modules, compressed firmware, a vdb with a package nothing keeps,
    a world and selected sets, documentation."""
    blob = b"\0" * 65536
    (rootfs / "usr/lib").mkdir(parents=True, exist_ok=True)
    (rootfs / "lib").symlink_to("usr/lib")
    for rel in _MODULE_FIRMWARE:
        _file(rootfs / _MODULES / rel, blob)
    _file(
        rootfs / f"usr/lib/modules/{KVER}/modules.dep",
        "".join(f"kernel/{rel}:\n" for rel in _MODULE_FIRMWARE).encode(),
    )
    fw = rootfs / "usr/lib/firmware"
    for rel in (
        "amdgpu/navi10_gpu_info.bin.zst",
        "mellanox/mlx5_core.bin.zst",
        "rtl_nic/rtl8168h-2.fw",
        "iwlwifi-cc-a0-77.ucode.zst",
        "intel-ucode/06-9e-0a",
        "regulatory.db",
    ):
        _file(fw / rel, blob)
    _file(rootfs / f"boot/vmlinuz-{KVER}", b"K")
    (rootfs / "etc/portage").mkdir(parents=True)
    _file(rootfs / "var/lib/portage/world", b"app-misc/unwanted\nsys-apps/systemd\n")
    _file(rootfs / "var/lib/portage/world_sets", b"@base\n@extra-system\n@worker\n")
    for pf, slot in _VDB.items():
        _file(rootfs / "var/db/pkg" / pf / "SLOT", f"{slot}\n".encode())
    _file(rootfs / "usr/bin/systemd-run", blob, mode=0o755)
    _file(rootfs / "usr/bin/python3.14", blob, mode=0o755)
    (rootfs / "usr/bin/python3").symlink_to("python3.14")
    for owned in _OWNS["app-misc/unwanted"]:
        _file(rootfs / owned, blob, mode=0o755)
    _file(rootfs / "usr/share/doc/unwanted-1.0/README", blob)
    _file(rootfs / "usr/lib/locale/C.utf8/LC_CTYPE", blob)


def _installed(rootfs: Path) -> dict[str, str]:
    """``cat/pn`` of every vdb entry -> its PF (category/PF)."""
    out: dict[str, str] = {}
    vdb = rootfs / "var/db/pkg"
    for entry in sorted(vdb.glob("*/*")) if vdb.is_dir() else ():
        match = _PF.match(entry.name)
        if match:
            out[f"{entry.parent.name}/{match['pn']}"] = f"{entry.parent.name}/{entry.name}"
    return out


def _vdb_match(rootfs: Path, atom: str) -> list[str]:
    """What ``portageq match / <atom>`` prints: the installed CPVs ``atom`` names,
    by exact name and, when given, slot."""
    cp, _, slot = atom.partition(":")
    pf = _installed(rootfs).get(cp)
    if pf is None:
        return []
    if slot:
        have = (rootfs / "var/db/pkg" / pf / "SLOT").read_text().strip().split("/")[0]
        if have != slot:
            return []
    return [pf]


def _lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if ln.strip()] if path.is_file() else []


def _walk_keys(data: object) -> Iterator[str]:
    if isinstance(data, Mapping):
        for key, value in data.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(data, list | tuple):
        for item in data:
            yield from _walk_keys(item)


class _Slimmed:
    """An assemble wired for the slim tests: every container command PLAYED on the
    rootfs, the image's state read at the moments that matter."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        slim: Mapping[str, Any] | None = None,
        checkpoints: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        #: flavor -> its slim section (only those flavors declare one)
        self.slim_by_flavor: dict[str, Mapping[str, Any]] = {"worker": slim} if slim else {}
        self.events: list[tuple[str, list[str]]] = []
        self.fail: str | None = None
        self.extracted = 0
        #: squashfs bytes per compression profile
        self.squashfs_size: dict[str, int] = {"zstd": 4096, "xz": 4096}
        self.isos_built: list[Path] = []
        #: the image's state at the slim depclean, at depmod, at live, at dracut, at squashfs
        self.at: dict[str, dict[str, Any]] = {}
        self.modinfo_asked: list[str] = []
        self.steps: list[dict[str, Any]] = []
        self.store: checkpoint.Store | None = None
        wired = self

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setenv("SHIDASHI_SCRATCH", str(tmp_path / "scratch"))
        monkeypatch.setenv("SHIDASHI_CACHE", str(tmp_path / "cache"))
        monkeypatch.setattr(asm, "load_pointer", lambda init, *, seeds_dir: _pointer())
        monkeypatch.setattr(
            asm, "fetch_stage3", lambda pointer, *, cache_dir, download: tmp_path / "s.tar"
        )

        def extract(_tarball: Path, rootfs: Path) -> None:
            wired.extracted += 1
            rootfs.mkdir(parents=True, exist_ok=True)
            _image(rootfs)

        monkeypatch.setattr(asm, "extract_stage3", extract)
        monkeypatch.setattr(asm, "apply_portage", lambda rootfs, recipe, **_k: None)
        monkeypatch.setattr(asm, "apply_rootfs", lambda *_a, **_k: ())
        monkeypatch.setattr(asm, "bind_repos", lambda d, **_k: [])
        tree = tmp_path / "pinned-gentoo"
        for pn, pf in (("a", "a-1"), ("b", "b-2")):
            _file(tree / "x" / pn / f"{pf}.ebuild")
        monkeypatch.setattr(asm, "pinned_repos", lambda **_k: {"gentoo": tree})
        monkeypatch.setattr(world, "current_atoms", lambda recipe, variants_dir: ("app-misc/a",))

        # the toolbox: present, never run
        tar = tmp_path / "fork-points" / "toolbox.tar"
        _file(tar, b"T")
        monkeypatch.setattr(toolbox, "tarball_path", lambda recipe, **_k: tar)
        monkeypatch.setattr(toolbox, "ensure_rootfs", lambda tarball, rootfs: False)

        class _Tools:
            def __init__(self, rootfs: Path, **_k: object) -> None:
                self.rootfs = rootfs

            def path(self, host: Path) -> Path:
                return host

            def run(self, argv: list[str]) -> str:
                return ""

            def versions(self) -> dict[str, str]:
                return {"squashfs-tools": "mksquashfs version 4.6.1"}

        monkeypatch.setattr(toolbox, "Toolbox", _Tools)

        # system.yaml: the real one, with the slim section of the flavors that declare it
        real_load = asm.load_system_config

        def load_system_config(recipe: ResolvedRecipe, *, variants_dir: Path) -> Any:
            cfg = real_load(recipe, variants_dir=variants_dir)
            slim_section = wired.slim_by_flavor.get(recipe.flavor)
            if slim_section is None:
                return cfg
            data = cfg.model_dump()
            data["slim"] = dict(slim_section)
            return type(cfg).model_validate(data)

        monkeypatch.setattr(asm, "load_system_config", load_system_config)

        def apply_system(
            _c: object, _cfg: object, *, init: str, build: object = None
        ) -> dict[str, object]:
            wired.events.append(("system", []))
            return {}

        def apply_live(container: Any, _cfg: object, *, init: str) -> dict[str, object]:
            wired.events.append(("live", []))
            wired.at["live"] = wired.state(container.rootfs)
            return {}

        monkeypatch.setattr(asm, "apply_system", apply_system)
        monkeypatch.setattr(asm, "apply_live", apply_live)
        monkeypatch.setattr(asm, "verify", lambda _r, _cfg, *, init, live: [])

        install_argv = iso_emerge_argv(_recipe("kde"))  # the same for every test flavor
        self.install_output = "[binary   N    ] x/a-1-1::gentoo  0 KiB\n"

        class _Container:
            def __init__(self, rootfs: Path, **_k: object) -> None:
                self.rootfs = rootfs

            def __enter__(self) -> _Container:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def run(
                self,
                argv: Sequence[str],
                *,
                env: Mapping[str, str] | None = None,
                check: bool = True,
            ) -> CommandResult:
                argv = list(argv)
                wired.events.append(("run", argv))
                return wired.play(self.rootfs, argv, check=check, install_argv=install_argv)

        monkeypatch.setattr(asm, "Container", _Container)

        def make_squashfs(rootfs: Path, output: Path, **kw: object) -> Path:
            profile = str(kw.get("compression"))
            wired.at[f"squashfs:{profile}"] = wired.state(rootfs)
            with output.open("wb") as out:
                out.truncate(wired.squashfs_size[profile])
            return output

        def build_iso(_squashfs: Path, output: Path, **_kw: object) -> Path:
            wired.isos_built.append(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"ISO")
            return output

        def write_contents(_squashfs: Path, dest: Path, **_kw: object) -> Path:
            dest.write_bytes(b"C")
            return dest

        monkeypatch.setattr(image, "make_squashfs", make_squashfs)
        monkeypatch.setattr(image, "build_iso", build_iso)
        monkeypatch.setattr(publish, "write_contents", write_contents)
        monkeypatch.setattr(publish, "write_sbom", lambda rootfs, dest: None)

        if checkpoints:
            from tests._fake_btrfs import FakeBackend

            self.binhost = tmp_path / "binhost"
            _file(
                self.binhost / "Packages",
                b"PACKAGES: 1\n\nBUILD_ID: 1\nCPV: x/a-1\nSHA1: a1\n",
            )
            store = checkpoint.Store(tmp_path / "ckpt", FakeBackend())
            self.store = store
            monkeypatch.setattr(checkpoint, "open_store", lambda root, **_k: store)
        else:
            self.binhost = tmp_path / "binhost"

    # --- the container, played ------------------------------------------------------

    def slim_phase(self) -> bool:
        """Whether the image is past its ``system`` step (the slim step's place)."""
        return any(kind == "system" for kind, _ in self.events)

    def state(self, rootfs: Path) -> dict[str, Any]:
        fw = rootfs / "usr/lib/firmware"
        return {
            "world": sorted(_lines(rootfs / "var/lib/portage/world")),
            "world_sets": _lines(rootfs / "var/lib/portage/world_sets"),
            "installed": sorted(_installed(rootfs)),
            "gpu": (rootfs / _MODULES / "drivers/gpu").exists(),
            "mellanox": (rootfs / _MODULES / "drivers/net/ethernet/mellanox").exists(),
            "realtek": (rootfs / _MODULES / "drivers/net/ethernet/realtek/r8169.ko.xz").exists(),
            "firmware": sorted(
                p.relative_to(fw).as_posix() for p in fw.rglob("*") if not p.is_dir()
            )
            if fw.is_dir()
            else [],
            "doc": (rootfs / "usr/share/doc/unwanted-1.0/README").exists(),
            "locale": (rootfs / "usr/lib/locale/C.utf8/LC_CTYPE").exists(),
            "unwanted_bin": (rootfs / "usr/bin/unwanted").exists(),
        }

    def _failed(self, argv: list[str], output: str, check: bool) -> CommandResult:
        if check:
            raise subprocess.CalledProcessError(1, argv, output=output, stderr="")
        return CommandResult(1, output, "")

    def play(
        self, rootfs: Path, argv: list[str], *, check: bool, install_argv: list[str]
    ) -> CommandResult:
        name = Path(argv[0]).name
        if argv == [*install_argv, "--pretend"] or argv == install_argv:
            return CommandResult(0, self.install_output, "")
        if name == "portageq" and len(argv) >= 4:
            matches = _vdb_match(rootfs, argv[-1])
            if argv[1] == "has_version":
                if not matches:
                    return self._failed(argv, "", check)
                return CommandResult(0, "", "")
            return CommandResult(0, "".join(f"{m}\n" for m in matches), "")
        if name == "emerge" and "--depclean" in argv:
            if self.slim_phase():
                self.at["depclean"] = self.state(rootfs)
                if self.fail == "depclean":
                    return self._failed(
                        argv,
                        ">>> Calculating dependencies\n!!! slim-test: depclean refused\n",
                        check,
                    )
            return self.depclean(rootfs)
        if name == "depmod":
            self.at["depmod"] = {**self.state(rootfs), "argv": argv}
            if self.fail == "depmod":
                return self._failed(argv, f"depmod: ERROR: could not open {KVER}\n", check)
            return CommandResult(0, "", "")
        if name == "modinfo":
            if self.fail == "modinfo":
                return self._failed(argv, "modinfo: ERROR: bad module\n", check)
            out: list[str] = []
            for arg in argv[1:]:
                base = Path(arg).name
                stem = re.sub(r"\.ko(\.zst|\.xz)?$", "", base)
                for known, firmware in _BY_BASENAME.items():
                    if base == known or stem == re.sub(r"\.ko(\.zst|\.xz)?$", "", known):
                        self.modinfo_asked.append(known)
                        out.extend(firmware)
            return CommandResult(0, "".join(f"{f}\n" for f in out), "")
        if name == "rm":
            for arg in argv[1:]:
                if arg.startswith("-"):
                    continue
                target = rootfs / arg.lstrip("/")
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
            return CommandResult(0, "", "")
        if name == "dracut":
            self.at["dracut"] = self.state(rootfs)
        return CommandResult(0, "", "")

    def depclean(self, rootfs: Path) -> CommandResult:
        """Remove what neither the world, the selected sets nor the system set reach."""
        installed = _installed(rootfs)
        if _lines(rootfs / "var/lib/portage/world_sets"):
            return CommandResult(0, "Number removed:       0\n", "")  # the sets reach it all
        wanted: set[str] = set(_SYSTEM_SET)
        for atom in _lines(rootfs / "var/lib/portage/world"):
            wanted.add(atom.partition(":")[0])
        for cp in list(wanted):
            wanted.update(_DEPENDS.get(cp, ()))
        removed = [cp for cp in installed if cp not in wanted]
        for cp in removed:
            shutil.rmtree(rootfs / "var/db/pkg" / installed[cp])
            for owned in _OWNS.get(cp, ()):
                (rootfs / owned).unlink(missing_ok=True)
        return CommandResult(0, f"Number removed:       {len(removed)}\n", "")

    # --- one run --------------------------------------------------------------------

    def assemble(
        self, flavor: str = "worker", *, compressions: Sequence[str] = ("zstd",)
    ) -> list[dict[str, Any]]:
        self.events, self.at, self.modinfo_asked, self.isos_built = [], {}, [], []
        with audit.run(self.tmp_path / "runs", command="assemble", argv=[]) as trail:
            try:
                Assembler(_recipe(flavor), self.binhost).assemble(
                    self.tmp_path / "dist",
                    compressions=compressions,
                    now=datetime.datetime(2026, 10, 11, 1, 0, tzinfo=datetime.UTC),
                )
            finally:
                manifest = audit.build_manifest(audit.read_events(trail.path / "events.jsonl"))
                self.steps = cast(list[dict[str, Any]], manifest["steps"])
        return self.steps

    def step(self, name: str) -> dict[str, Any]:
        return next(s for s in self.steps if s["step"] == name)

    def rootfs(self, flavor: str = "worker") -> Path:
        return self.tmp_path / "scratch" / "assemble" / f"znver5-{flavor}-systemd"

    def index(self, predicate: Callable[[str, list[str]], bool], start: int = 0) -> int:
        """The first event at or after ``start`` that ``predicate`` accepts."""
        return next(
            i for i, (kind, argv) in enumerate(self.events) if i >= start and predicate(kind, argv)
        )

    def slim_commands(self) -> list[list[str]]:
        """The container commands run after ``system``."""
        start = self.index(lambda kind, _a: kind == "system")
        return [argv for kind, argv in self.events[start:] if kind == "run"]


def _slim_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **slim: Any) -> _Slimmed:
    return _Slimmed(tmp_path, monkeypatch, slim={**SLIM, **slim})


# --- the step, in its place and in its order (R1.1, R2.2, R2.3) ----------------------


def test_the_slim_step_runs_after_system_and_before_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _slim_wired(tmp_path, monkeypatch)
    steps = wired.assemble()
    i = _OLD_STEPS.index("system") + 1
    assert [s["step"] for s in steps] == [*_OLD_STEPS[:i], "slim", *_OLD_STEPS[i:]]
    assert all(s["status"] == "ok" for s in steps)


def test_the_slim_step_cleans_the_world_down_to_the_keep_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The world is the keep list AND the selected sets are emptied before the
    depclean: a set left selected would keep everything it reaches."""
    wired = _slim_wired(tmp_path, monkeypatch)
    wired.assemble()
    at_depclean = wired.at["depclean"]
    assert at_depclean["world"] == sorted(SLIM["keep"])
    assert at_depclean["world_sets"] == []
    clean = next(a for a in wired.slim_commands() if a[:2] == ["emerge", "--depclean"])
    assert "--with-bdeps=n" in clean
    live = wired.at["live"]
    # removed, with the files it owned; the keep list, its dependencies and @system stay
    assert "app-misc/unwanted" not in live["installed"]
    assert live["unwanted_bin"] is False
    assert {
        "sys-apps/systemd",
        "dev-lang/python",
        "dev-lang/python-exec",
        "sys-libs/glibc",
    } <= set(live["installed"])


def test_modules_are_dropped_and_indexed_before_the_firmware_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order: depclean, the module subtrees gone, ``depmod -a <kver>``, then
    ``modinfo`` over the modules that remain -- never over a dropped one."""
    wired = _slim_wired(tmp_path, monkeypatch)
    wired.assemble()
    system = wired.index(lambda kind, _a: kind == "system")
    clean = wired.index(lambda kind, a: kind == "run" and a[:2] == ["emerge", "--depclean"], system)
    depmod = wired.index(lambda kind, a: kind == "run" and Path(a[0]).name == "depmod")
    modinfo = wired.index(lambda kind, a: kind == "run" and Path(a[0]).name == "modinfo")
    live = wired.index(lambda kind, _a: kind == "live")
    dracut = wired.index(lambda kind, a: kind == "run" and a[0] == "dracut")
    assert system < clean < depmod < modinfo < live < dracut
    at_depmod = wired.at["depmod"]
    assert KVER in at_depmod["argv"] and "-a" in at_depmod["argv"]
    assert at_depmod["gpu"] is False and at_depmod["mellanox"] is False
    assert at_depmod["realtek"] is True  # wired network of another vendor stays
    assert "amdgpu.ko.zst" not in wired.modinfo_asked
    assert "mlx5_core.ko.zst" not in wired.modinfo_asked
    assert {"iwlwifi.ko.zst", "r8169.ko.xz", "nvme.ko"} <= set(wired.modinfo_asked)


def test_firmware_follows_the_remaining_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile: amdgpu's firmware exists and a module once asked for it -- but that
    module was dropped, so it goes. The compressed ``.zst`` file of a name a remaining
    module asks for stays, as do microcode and the regulatory database."""
    wired = _slim_wired(tmp_path, monkeypatch)
    wired.assemble()
    firmware = wired.at["live"]["firmware"]
    assert "amdgpu/navi10_gpu_info.bin.zst" not in firmware
    assert "mellanox/mlx5_core.bin.zst" not in firmware
    assert {
        "iwlwifi-cc-a0-77.ucode.zst",
        "rtl_nic/rtl8168h-2.fw",
        "intel-ucode/06-9e-0a",
        "regulatory.db",
    } <= set(firmware)


def test_the_initramfs_is_built_from_the_slimmed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dracut --no-hostonly copies what it finds: slimmed before, or the initramfs
    carries the dropped drivers and their firmware."""
    wired = _slim_wired(tmp_path, monkeypatch)
    wired.assemble()
    at_dracut = wired.at["dracut"]
    assert at_dracut["gpu"] is False
    assert "amdgpu/navi10_gpu_info.bin.zst" not in at_dracut["firmware"]
    assert at_dracut["doc"] is False  # pruned
    assert at_dracut["locale"] is True  # not what the prune globs name


def test_the_slim_report_is_recorded_in_the_audit_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.3: what went -- packages, module groups, firmware -- and the size before
    and after, in the step's data."""
    wired = _slim_wired(tmp_path, monkeypatch)
    wired.assemble()
    data = wired.step("slim")
    keys = set(_walk_keys(data))
    assert {
        "removed_packages",
        "dropped_module_dirs",
        "deleted_firmware_files",
        "pruned_files",
        "bytes_before",
        "bytes_after",
    } <= keys
    dump = json.dumps(data)
    assert "app-misc/unwanted" in dump
    assert "drivers/gpu" in dump and "drivers/net/ethernet/mellanox" in dump

    def first(key: str) -> Any:
        def find(node: object) -> Iterator[Any]:
            if isinstance(node, Mapping):
                for k, v in node.items():
                    if k == key:
                        yield v
                    yield from find(v)

        return next(find(data))

    before, after = int(first("bytes_before")), int(first("bytes_after"))
    assert before > after > 0


# --- refusals (R1.2, R1.4) ------------------------------------------------------------


def test_a_keep_atom_that_is_not_installed_stops_before_anything_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1.2, hostile both ways: ``sys-apps/system`` is not ``sys-apps/systemd`` and
    ``dev-lang/python:3.13`` is not the installed 3.14 -- both missing; while
    ``dev-lang/python:3.14``, installed as ``python-3.14.2-r1``, is present and must
    not be named."""
    keep = [
        "sys-apps/systemd",
        "dev-lang/python:3.14",
        "sys-apps/system",
        "dev-lang/python:3.13",
        "app-misc/notthere",
    ]
    wired = _slim_wired(tmp_path, monkeypatch, keep=keep)
    with pytest.raises(AssemblerError) as caught:
        wired.assemble()
    message = str(caught.value)
    assert re.search(r"sys-apps/system(?!d)", message)
    assert "dev-lang/python:3.13" in message
    assert "app-misc/notthere" in message
    assert "sys-apps/systemd" not in message
    assert "dev-lang/python:3.14" not in message
    # nothing removed: no depclean after system, the tree as the install left it
    assert not any(a[:2] == ["emerge", "--depclean"] for a in wired.slim_commands())
    rootfs = wired.rootfs()  # kept for debugging on failure
    assert (rootfs / _MODULES / "drivers/gpu").is_dir()
    assert (rootfs / "usr/lib/firmware/amdgpu/navi10_gpu_info.bin.zst").is_file()
    assert (rootfs / "var/db/pkg/app-misc/unwanted-1.0").is_dir()
    assert _lines(rootfs / "var/lib/portage/world_sets") == ["@base", "@extra-system", "@worker"]
    assert "live" not in [kind for kind, _ in wired.events]
    assert not any(k.startswith("squashfs") for k in wired.at)


def test_every_command_missing_after_the_slim_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1.4: ``/usr/bin/unwanted`` is present when the step starts and gone after the
    depclean -- the check runs on the slimmed image, and names every missing one."""
    commands = ["/usr/bin/systemd-run", "/usr/bin/unwanted", "/usr/bin/never-there"]
    wired = _slim_wired(tmp_path, monkeypatch, commands=commands)
    with pytest.raises(AssemblerError) as caught:
        wired.assemble()
    message = str(caught.value)
    assert "/usr/bin/unwanted" in message
    assert "/usr/bin/never-there" in message
    assert "/usr/bin/systemd-run" not in message
    assert "live" not in [kind for kind, _ in wired.events]
    assert not any(k.startswith("squashfs") for k in wired.at)


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        ("depclean", "slim-test: depclean refused"),
        ("depmod", KVER),
        ("modinfo", KVER),
    ],
)
def test_a_failed_container_command_of_the_slim_step_is_an_assembler_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, needle: str
) -> None:
    wired = _slim_wired(tmp_path, monkeypatch)
    wired.fail = command
    with pytest.raises(AssemblerError) as caught:
        wired.assemble()
    assert needle in str(caught.value)
    assert wired.step("slim")["status"] != "ok"
    assert not any(k.startswith("squashfs") for k in wired.at)


# --- every other image (R1.6) ----------------------------------------------------------


def test_an_image_without_slim_runs_exactly_the_old_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kde declares no slim: the same steps, the same container commands, the same
    tree -- and no budget, however large its squashfs."""
    wired = _slim_wired(tmp_path, monkeypatch, budget_mb=1)
    wired.squashfs_size["zstd"] = 5 * 2**20
    steps = wired.assemble("kde")
    assert [s["step"] for s in steps] == _OLD_STEPS
    assert wired.step("configure")["system"]["slim"] is None  # it knows slim, and has none
    after_system = wired.slim_commands()
    assert [Path(a[0]).name for a in after_system] == ["dracut"]
    depcleans = [a for kind, a in wired.events if kind == "run" and "--depclean" in a]
    assert depcleans == [list(asm.ISO_DEPCLEAN_ARGV)]
    packed = wired.at["squashfs:zstd"]
    assert packed["gpu"] is True and packed["doc"] is True
    assert "amdgpu/navi10_gpu_info.bin.zst" in packed["firmware"]
    assert "app-misc/unwanted" in packed["installed"]
    assert packed["world_sets"] == ["@base", "@extra-system", "@worker"]
    assert len(wired.isos_built) == 1


# --- the shared checkpoint (R1.7) -----------------------------------------------------


def test_the_slimmed_tree_never_reaches_the_shared_packages_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker installs first and slims; minimal's twin (kde here, same inputs)
    then resumes from the SAME packages checkpoint and must find the whole install,
    not the worker's slimmed tree. The worker's next run resumes from it too, and
    slims again."""
    wired = _Slimmed(tmp_path, monkeypatch, slim=SLIM, checkpoints=True)
    assert wired.store is not None
    wired.assemble("worker")
    assert wired.extracted == 1
    assert wired.at["live"]["gpu"] is False  # the worker was slimmed

    wired.assemble("kde")
    assert wired.extracted == 1  # resumed, not installed again
    assert wired.step("seed")["restored"] == checkpoint.PACKAGES
    assert wired.step("seed")["shared_with"] == ["znver5-worker-systemd"]
    assert all(s["step"] != "slim" for s in wired.steps)
    packed = wired.at["squashfs:zstd"]
    assert packed["gpu"] is True and packed["mellanox"] is True
    assert "app-misc/unwanted" in packed["installed"]
    assert "amdgpu/navi10_gpu_info.bin.zst" in packed["firmware"]
    assert packed["world_sets"] == ["@base", "@extra-system", "@worker"]

    wired.assemble("worker")
    assert wired.extracted == 1
    assert wired.step("seed")["restored"] == checkpoint.PACKAGES
    assert wired.step("slim")["status"] == "ok"
    assert wired.at["live"]["gpu"] is False
    (shared,) = wired.store.marks(checkpoint.PACKAGES)
    assert set(shared.images) == {"znver5-worker-systemd", "znver5-kde-systemd"}


# --- the budget (R3.2, R3.4) ------------------------------------------------------------


def test_a_squashfs_exactly_at_its_budget_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _slim_wired(tmp_path, monkeypatch, budget_mb=1)
    wired.squashfs_size["zstd"] = 2**20
    wired.assemble()
    assert [iso.name for iso in wired.isos_built] == ["bentoo-2026.10.11-worker-systemd-znver5.iso"]
    assert wired.step("publish:zstd")["status"] == "ok"


def test_one_byte_over_the_budget_fails_before_the_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _slim_wired(tmp_path, monkeypatch, budget_mb=1)
    wired.squashfs_size["zstd"] = 2**20 + 1
    with pytest.raises(AssemblerError, match="budget"):
        wired.assemble()
    assert wired.isos_built == []
    assert all(s["step"] != "iso:zstd" for s in wired.steps)
    dist = tmp_path / "dist"
    assert not list(dist.glob("*.iso")) and not list(dist.glob("latest-*.txt"))


def test_the_budget_error_names_the_image_its_size_and_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _slim_wired(tmp_path, monkeypatch, budget_mb=1)
    wired.squashfs_size["zstd"] = 3 * 2**20
    with pytest.raises(AssemblerError) as caught:
        wired.assemble()
    message = str(caught.value)
    assert "worker" in message
    assert re.search(r"\b3(?:\.0+)? ?MiB", message), message
    assert re.search(r"\b1 ?MiB", message), message


def test_only_the_compression_over_its_budget_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.4 per compression: zstd within the budget is published, xz over it is not."""
    wired = _slim_wired(tmp_path, monkeypatch, budget_mb=1)
    wired.squashfs_size.update(zstd=2**20, xz=2**20 + 1)
    with pytest.raises(AssemblerError, match="budget"):
        wired.assemble(compressions=("zstd", "xz"))
    assert [iso.name for iso in wired.isos_built] == ["bentoo-2026.10.11-worker-systemd-znver5.iso"]
    assert all(s["step"] != "iso:xz" for s in wired.steps)
    assert not (tmp_path / "dist" / "bentoo-2026.10.11-worker-systemd-znver5-xz.iso").exists()


def test_a_slim_image_without_a_budget_takes_any_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired = _slim_wired(tmp_path, monkeypatch)
    assert "budget_mb" not in SLIM
    wired.squashfs_size["zstd"] = 5 * 2**20
    wired.assemble()
    assert wired.step("slim")["status"] == "ok"
    assert len(wired.isos_built) == 1
