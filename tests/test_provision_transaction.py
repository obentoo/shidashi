"""Tests of ``shidashi.provision.provision`` -- the provisioning transaction (story 020,
task 1.3), end to end against real ISOs.

The source ISOs are built here: a small one with ``xorriso -as mkisofs`` (BIOS El
Torito and a protective MBR) and, where grub-mkrescue is installed, a real hybrid made
the way the worker ISO is (``grub-mkrescue … -- -volid BENTOO_WORKER``: BIOS and UEFI El
Torito, GPT with the EFI partition). ``ssh-keygen`` and ``xorriso`` really run; the
runner handed to ``provision`` passes every command through, except where a test makes
one step fail (a write that leaves a partial file, a key generation that fails, a copy
that lost its boot entry or its label). Refusals are checked with hand-built volume
descriptors, so they also run on a host without xorriso.

"Nothing changed" is checked on the whole temporary tree: paths, modes, bytes, links.

A copy's boot is compared by what a PC boots from -- the El Torito entries (platform,
emulation, load size, image path), the system-area kinds (MBR, GPT, grub2-mbr) and the
GPT partition paths -- never by LBAs or the image size, which a correct copy moves.
The Apple partition map (APM, Mac boot) is not required to survive, nor the MBR's
cylinder alignment (xorriso re-picks it for a copy of another size).

Requirements exercised: R1.1, R1.2, R1.3, R1.4, R1.5, R1.6, R1.7, R1.8, R4.2, R4.3.
"""

import base64
import hashlib
import importlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shidashi import workers

NAME = "bentoo-lab"
LABEL_OFFSET = 0x8028

needs_tools = pytest.mark.skipif(
    shutil.which("xorriso") is None or shutil.which("ssh-keygen") is None,
    reason="needs xorriso and ssh-keygen",
)
needs_grub = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("grub-mkrescue", "mformat", "xorriso", "ssh-keygen"))
    or not Path("/usr/lib/grub/i386-pc").is_dir()
    or not Path("/usr/lib/grub/x86_64-efi").is_dir(),
    reason="needs grub-mkrescue with the i386-pc and x86_64-efi platforms, mformat, xorriso",
)


def _provision() -> Any:
    return importlib.import_module("shidashi.provision")


# --- keys -----------------------------------------------------------------------------


def _ed25519_line(seed: str, comment: str = "") -> str:
    raw = hashlib.sha256(seed.encode()).digest()
    blob = len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + raw
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".rstrip()


def _fingerprint(line: str) -> str:
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


OLD_KEY = _ed25519_line("an-earlier-pairing")


# --- ISOs -----------------------------------------------------------------------------


def _pvd_image(path: Path, label: bytes, *, magic: bytes = b"CD001") -> Path:
    """An ISO 9660 image reduced to its Primary Volume Descriptor and terminator."""
    pvd = bytearray(2048)
    pvd[0], pvd[1:6], pvd[6] = 1, magic, 1
    pvd[8:40] = b" " * 32
    pvd[40:72] = label.ljust(32, b" ")
    end = bytearray(2048)
    end[0], end[1:6], end[6] = 255, magic, 1
    path.write_bytes(bytes(16 * 2048) + bytes(pvd) + bytes(end))
    return path


def _tree(base: Path) -> Path:
    tree = base / "tree"
    (tree / "LiveOS").mkdir(parents=True)
    (tree / "LiveOS" / "squashfs.img").write_bytes(hashlib.sha512(b"squash").digest() * 64)
    (tree / "bentoo").mkdir()
    (tree / "bentoo" / "release").write_text("20261010T1200\n")
    return tree


def _mkisofs(base: Path, label: str) -> Path:
    tree = _tree(base)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "eltorito.img").write_bytes(bytes(2048))
    iso = base / f"{label.lower()}.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-quiet", "-R", "-V", label]
        + ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4"]
        + ["--protective-msdos-label", "-o", str(iso), str(tree)],
        check=True,
        capture_output=True,
    )
    return iso


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Real source ISOs, built once per module (only where xorriso exists)."""
    if shutil.which("xorriso") is None:
        return {}
    return {
        "worker": _mkisofs(tmp_path_factory.mktemp("worker-iso"), "BENTOO_WORKER"),
        "kde": _mkisofs(tmp_path_factory.mktemp("kde-iso"), "BENTOO_KDE"),
    }


@pytest.fixture(scope="module")
def hybrid(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real grub-mkrescue hybrid (BIOS + UEFI El Torito, GPT, EFI partition)."""
    base = tmp_path_factory.mktemp("hybrid-iso")
    tree = _tree(base)
    (tree / "boot" / "grub").mkdir(parents=True)
    (tree / "boot" / "grub" / "grub.cfg").write_text("menuentry bentoo {\n true\n}\n")
    iso = base / "hybrid.iso"
    subprocess.run(
        ["grub-mkrescue", "-o", str(iso), "-iso-level", "3", str(tree), "--"]
        + ["-volid", "BENTOO_WORKER"],
        check=True,
        capture_output=True,
    )
    return iso


def _label(iso: Path) -> str:
    with iso.open("rb") as handle:
        handle.seek(LABEL_OFFSET)
        return handle.read(32).decode("ascii").rstrip(" ")


def _xorriso(*args: str) -> str:
    done = subprocess.run(["xorriso", *args], check=True, capture_output=True, text=True)
    return done.stdout


def _boot_shape(iso: Path) -> tuple[list[tuple[str, ...]], set[str], list[str]]:
    """What a PC boots from: El Torito entries (no LBA), system-area kinds (no APM, no
    ``cyl-align-*``: xorriso re-picks the MBR geometry for a copy of another size),
    GPT partition paths."""
    report = _xorriso("-indev", str(iso), "-report_el_torito", "plain")
    report += _xorriso("-indev", str(iso), "-report_system_area", "plain")
    images: dict[str, tuple[str, ...]] = {}
    paths: dict[str, str] = {}
    kinds: set[str] = set()
    gpt_paths: list[str] = []
    for line in report.splitlines():
        key, _sep, value = line.partition(":")
        fields = value.split()
        key = key.strip()
        if key == "El Torito boot img" and len(fields) >= 7:
            images[fields[0]] = tuple(fields[1:7])
        elif key == "El Torito img path" and len(fields) >= 2:
            paths[fields[0]] = fields[1]
        elif key == "System area summary":
            kinds = {f for f in fields if f != "APM" and not f.startswith("cyl-align")}
        elif key == "GPT partition path" and len(fields) >= 2:
            gpt_paths.append(fields[1])
    torito = sorted((*images[n], paths.get(n, "")) for n in images)
    return torito, kinds, sorted(gpt_paths)


def _extract(iso: Path, inside: str, dest: Path) -> Path:
    _xorriso("-osirrox", "on", "-indev", str(iso), "-extract", inside, str(dest))
    return dest


def _listing(iso: Path) -> list[str]:
    return [p.strip("'") for p in _xorriso("-indev", str(iso), "-find", "/").split()]


# --- the host -------------------------------------------------------------------------


@dataclass
class _Env:
    workers_dir: Path
    dist: Path
    iso: Path
    host_pub: str


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path) -> _Env:
    """The host: its workers directory with its key, and an assemble output directory
    holding the source ISO, its checksum and a ``latest`` link."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    wd = tmp_path / "xdg" / "shidashi" / "worker"
    wd.mkdir(parents=True, mode=0o700)
    key = wd / "id_ed25519"
    if shutil.which("ssh-keygen"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "shidashi-worker-key"]
            + ["-f", str(key)],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    else:
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END\n")
        key.chmod(0o600)
        Path(f"{key}.pub").write_text(_ed25519_line("host", "shidashi-worker-key") + "\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    iso = dist / "bentoo-2026.10.10-worker-systemd-v3.iso"
    shutil.copyfile(source, iso)
    (dist / f"{iso.name}.sha512").write_text(
        f"{hashlib.sha512(iso.read_bytes()).hexdigest()}  {iso.name}\n"
    )
    (dist / "latest-worker.iso").symlink_to(iso.name)
    return _Env(wd, dist, iso, Path(f"{key}.pub").read_text().strip())


@pytest.fixture
def crafted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """A host whose source ISO is a hand-built worker descriptor (enough to be refused)."""
    source = _pvd_image(tmp_path / "crafted.iso", b"BENTOO_WORKER")
    env = _env(tmp_path, monkeypatch, source)
    source.unlink()
    return env


@pytest.fixture
def real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built: dict[str, Path]) -> _Env:
    if "worker" not in built:
        pytest.skip("needs xorriso")
    return _env(tmp_path, monkeypatch, built["worker"])


def _snapshot(root: Path) -> dict[str, tuple[str, int, bytes]]:
    """Every path under ``root``: its kind, mode and bytes (a link: its target)."""
    out: dict[str, tuple[str, int, bytes]] = {}
    for path in sorted([root, *root.rglob("*")]):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", 0, os.readlink(path).encode())
        elif path.is_dir():
            out[rel] = ("dir", stat.S_IMODE(path.stat().st_mode), b"")
        else:
            out[rel] = ("file", stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    return out


def _keys_for(known_hosts: Path, name: str) -> list[str]:
    """Every key ssh would accept for ``name`` (HostKeyAlias)."""
    if not known_hosts.exists():
        return []
    found = []
    for line in known_hosts.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        hosts, *key = line.split()
        if name in hosts.split(","):
            found.append(" ".join(key[:2]))
    return found


def _ssh_keygen_find(known_hosts: Path, name: str) -> list[str]:
    """The keys OpenSSH itself finds for ``name`` (``ssh-keygen -F``)."""
    done = subprocess.run(
        ["ssh-keygen", "-F", name, "-f", str(known_hosts)], capture_output=True, text=True
    )
    return [
        " ".join(line.split()[1:3])
        for line in done.stdout.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _registry_raw(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps(entries, indent=1) + "\n")
    path.chmod(0o600)


def _paired(name: str, key: str, address: str | None = "192.168.15.7") -> dict[str, Any]:
    """A registry entry as story 009's pairing writes it (pre-020 shape)."""
    return {
        "name": name,
        "address": address,
        "host_key": key,
        "host_key_fingerprint": _fingerprint(key),
        "paired_at": "2026-10-05T12:00:00+00:00",
        "cpu_flags": ["avx2"],
        "image": "20261005T1200",
    }


# --- the runner -----------------------------------------------------------------------

Before = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]
After = Callable[[list[str], "subprocess.CompletedProcess[Any]"], None]


@dataclass
class _Runner:
    """``subprocess.run`` for real, recording argv; ``before`` may answer a command
    instead of running it, ``after`` may alter what a real run left behind."""

    before: Before | None = None
    after: After | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        check = bool(kwargs.pop("check", False))
        answer = self.before(argv) if self.before else None
        if answer is None:
            done: subprocess.CompletedProcess[Any] = subprocess.run(argv, *args, **kwargs)
            if self.after:
                self.after(argv, done)
        else:
            textual = (
                kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
            )
            if textual:
                done = answer
            else:
                done = subprocess.CompletedProcess(
                    argv, answer.returncode, answer.stdout.encode(), answer.stderr.encode()
                )
        if check and done.returncode:
            raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
        return done


def _is_xorriso(argv: list[str]) -> bool:
    return Path(argv[0]).name == "xorriso"


def _arg_after(argv: list[str], flag: str) -> Path | None:
    if flag not in argv or argv.index(flag) + 1 >= len(argv):
        return None
    return Path(argv[argv.index(flag) + 1].removeprefix("stdio:"))


def _writes_copy(argv: list[str]) -> bool:
    return _is_xorriso(argv) and "-outdev" in argv


def _is_keygen(argv: list[str]) -> bool:
    return Path(argv[0]).name == "ssh-keygen" and "-t" in argv


def _run(env: _Env, runner: Any = subprocess.run, *, replace: bool = False) -> Any:
    return _provision().provision(
        NAME, env.iso, workers_dir=env.workers_dir, replace=replace, runner=runner
    )


# ===================================================================================
# refusals -- nothing written (R1.5, R4.3, R1.6)
# ===================================================================================


@pytest.mark.parametrize(
    "label", ["BENTOO_WORKERS", "BENTOO_WORKER_OLD", "XBENTOO_WORKER", "bentoo_worker"]
)
def test_an_iso_whose_label_only_looks_like_the_workers_is_refused_naming_it(
    tmp_path: Path, crafted: _Env, label: str
) -> None:
    """Hostile: the live root is found by ``CDLABEL=BENTOO_WORKER`` exactly; a copy of
    a lookalike would not boot as a worker."""
    prov = _provision()
    _pvd_image(crafted.iso, label.encode())
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(crafted)
    message = str(caught.value)
    assert "not a worker ISO" in message
    assert label in message
    assert crafted.iso.name in message
    assert _snapshot(tmp_path) == before


def test_another_images_iso_is_refused_naming_its_volume(tmp_path: Path, crafted: _Env) -> None:
    prov = _provision()
    _pvd_image(crafted.iso, b"BENTOO_KDE")
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(crafted)
    assert "not a worker ISO" in str(caught.value)
    assert "BENTOO_KDE" in str(caught.value)
    assert _snapshot(tmp_path) == before


def test_a_file_that_is_no_iso_is_refused_writing_nothing(tmp_path: Path, crafted: _Env) -> None:
    prov = _provision()
    crafted.iso.write_bytes(bytes(40 * 2048))
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(crafted)
    assert crafted.iso.name in str(caught.value)
    assert _snapshot(tmp_path) == before


@needs_tools
def test_a_real_iso_of_another_image_is_refused_naming_its_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built: dict[str, Path]
) -> None:
    prov = _provision()
    env = _env(tmp_path, monkeypatch, built["kde"])
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(env)
    assert "BENTOO_KDE" in str(caught.value)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("exists", [True, False], ids=["existing", "not-created-yet"])
def test_a_workers_dir_inside_a_git_work_tree_is_refused_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    """R4.3: an identity under a checkout is one ``git add`` away from being published."""
    prov = _provision()
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    wd = tmp_path / "repo" / "xdg" / "shidashi" / "worker"
    if exists:
        wd.mkdir(parents=True, mode=0o700)
    iso = _pvd_image(tmp_path / "worker.iso", b"BENTOO_WORKER")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "repo" / "xdg"))
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        prov.provision(NAME, iso, workers_dir=wd, replace=False, runner=_Runner())
    assert "inside a git work tree" in str(caught.value)
    assert str(wd) in str(caught.value)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "where",
    ["paired", "registry-only", "known-hosts-only", "known-hosts-host-list", "provisioned"],
)
def test_a_name_already_holding_a_key_is_refused_naming_its_fingerprint(
    tmp_path: Path, crafted: _Env, where: str
) -> None:
    """R1.6. Hostile ``known-hosts-host-list``: the name pinned among other names on one
    line is pinned all the same (ssh accepts any line whose host list names it)."""
    prov = _provision()
    wd = crafted.workers_dir
    if where in ("paired", "registry-only"):
        _registry_raw(wd / "workers.json", {NAME: _paired(NAME, OLD_KEY)})
    if where == "provisioned":
        entry = _paired(NAME, OLD_KEY, address=None) | {"provisioned": True}
        entry.update(cpu_flags=[], image="")
        _registry_raw(wd / "workers.json", {NAME: entry})
    if where in ("paired", "known-hosts-only", "provisioned"):
        (wd / "known_hosts").write_text(f"{NAME} {OLD_KEY}\n")
    if where == "known-hosts-host-list":
        (wd / "known_hosts").write_text(f"192.168.15.7,{NAME} {OLD_KEY}\n")
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(crafted)
    message = str(caught.value)
    assert NAME in message
    assert _fingerprint(OLD_KEY) in message
    assert "--replace" in message
    assert _snapshot(tmp_path) == before


# ===================================================================================
# the identity, the copy, the pin, the record (R1.1-R1.4, R4.2)
# ===================================================================================


@needs_tools
def test_lookalike_names_neither_block_provisioning_nor_lose_their_pins(real: _Env) -> None:
    """Hostile: workers named like N (and a comment) are other workers -- provisioning N
    is not refused because of them, and their pins and entries stay as they were."""
    wd = real.workers_dir
    others = {
        "bentoo-lab2": _ed25519_line("lab2"),
        "bentoo": _ed25519_line("bentoo"),
        "bentoo-lab.local": _ed25519_line("dotlocal"),
        "lab": _ed25519_line("lab"),
    }
    lines = ["# pinned by shidashi"] + [f"{n} {k}" for n, k in others.items()]
    (wd / "known_hosts").write_text("\n".join(lines) + "\n")
    _registry_raw(
        wd / "workers.json", {"bentoo-lab2": _paired("bentoo-lab2", others["bentoo-lab2"])}
    )

    result = _run(real, _Runner())

    text = (wd / "known_hosts").read_text()
    assert "# pinned by shidashi" in text
    for name, key in others.items():
        assert _keys_for(wd / "known_hosts", name) == [" ".join(key.split()[:2])], name
    registry = workers.load_registry(wd / "workers.json")
    assert registry["bentoo-lab2"].host_key == others["bentoo-lab2"]
    assert registry[NAME].host_key_fingerprint == result.fingerprint


@needs_tools
def test_provision_writes_the_identity_owner_only_and_a_personalized_copy(real: _Env) -> None:
    """R1.1, R1.4."""
    source_sha = hashlib.sha512(real.iso.read_bytes()).hexdigest()
    result = _run(real, _Runner())

    home = real.workers_dir / NAME
    assert result.name == NAME
    assert Path(result.iso_path) == home / f"{NAME}.iso"
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    for path in home.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            assert mode & 0o077 == 0, path
        else:
            assert mode == 0o600, path

    identity = home / "identity"
    pub = (identity / "ssh" / "ssh_host_ed25519_key.pub").read_text().strip()
    assert result.fingerprint == _fingerprint(pub)
    assert sorted(p.name for p in (identity / "ssh").iterdir()) == [
        "ssh_host_ed25519_key",
        "ssh_host_ed25519_key.pub",
    ]
    derived = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(identity / "ssh" / "ssh_host_ed25519_key")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert derived.split()[:2] == pub.split()[:2]  # a real pair, not two unrelated files
    record = json.loads((identity / "pairing.json").read_text())
    assert (record["v"], record["name"], record["source"]) == (1, NAME, "provisioned")
    assert record["granting_key_fingerprint"] == _fingerprint(real.host_pub)
    granted = (identity / "authorized_keys").read_text().splitlines()
    assert [ln.split()[:2] for ln in granted] == [real.host_pub.split()[:2]]

    copy = Path(result.iso_path)
    assert _label(copy) == "BENTOO_WORKER"
    assert _boot_shape(copy) == _boot_shape(real.iso)
    medium = _extract(copy, "/shidashi/identity", real.workers_dir.parent / "medium")
    files = sorted(str(p.relative_to(identity)) for p in identity.rglob("*") if p.is_file())
    assert sorted(str(p.relative_to(medium)) for p in medium.rglob("*") if p.is_file()) == files
    for path in identity.rglob("*"):
        if path.is_file():
            assert (medium / path.relative_to(identity)).read_bytes() == path.read_bytes(), path
    squash = _extract(copy, "/LiveOS/squashfs.img", real.workers_dir.parent / "squashfs.img")
    original = _extract(real.iso, "/LiveOS/squashfs.img", real.workers_dir.parent / "orig.img")
    assert squash.read_bytes() == original.read_bytes()  # the squashfs is not touched
    assert set(_listing(real.iso)) < set(_listing(copy))
    assert hashlib.sha512(real.iso.read_bytes()).hexdigest() == source_sha


@needs_tools
def test_provision_pins_the_new_key_under_the_name_before_the_first_boot(real: _Env) -> None:
    """R1.2: what OpenSSH itself finds for N is N's new key, and only it."""
    result = _run(real, _Runner())
    known_hosts = real.workers_dir / "known_hosts"
    pub = (real.workers_dir / NAME / "identity/ssh/ssh_host_ed25519_key.pub").read_text()
    expected = " ".join(pub.split()[:2])
    assert _keys_for(known_hosts, NAME) == [expected]
    assert _ssh_keygen_find(known_hosts, NAME) == [expected]
    assert _fingerprint(expected) == result.fingerprint


@needs_tools
def test_provision_records_the_worker_provisioned_with_no_address(real: _Env) -> None:
    """R1.3, read back from the registry file."""
    _registry_raw(real.workers_dir / "workers.json", {"spare": _paired("spare", OLD_KEY)})
    result = _run(real, _Runner())
    registry = workers.load_registry(real.workers_dir / "workers.json")
    entry = registry[NAME]
    assert entry.model_dump()["provisioned"] is True
    assert entry.address is None
    assert entry.host_key_fingerprint == result.fingerprint
    assert _fingerprint(entry.host_key) == result.fingerprint
    assert registry["spare"].host_key == OLD_KEY


@needs_tools
def test_provision_writes_nothing_outside_its_workers_dir(tmp_path: Path, real: _Env) -> None:
    """R4.2: the assemble's output directory -- the ISO, its checksum, its ``latest``
    link -- is exactly as it was, and no ISO or identity appears anywhere else."""
    outside = {k: v for k, v in _snapshot(tmp_path).items() if not k.startswith("xdg")}
    runner = _Runner()
    _run(real, runner)
    after = {k: v for k, v in _snapshot(tmp_path).items() if not k.startswith("xdg")}
    assert after == outside
    for argv in runner.calls:
        target = _arg_after(argv, "-outdev")
        if target is not None:
            assert real.workers_dir in target.resolve().parents, argv
    assert sorted(p.name for p in real.workers_dir.iterdir()) == sorted(
        ["id_ed25519", "id_ed25519.pub", "known_hosts", "workers.json", NAME]
    )  # no temporary directory left behind


@needs_grub
def test_the_copy_of_a_grub_mkrescue_hybrid_keeps_its_bios_and_uefi_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hybrid: Path
) -> None:
    """The worker ISO's own shape: a copy that lost El Torito, the EFI partition or the
    MBR/GPT would not boot from the stick."""
    env = _env(tmp_path, monkeypatch, hybrid)
    result = _run(env, _Runner())
    copy = Path(result.iso_path)
    torito, kinds, gpt = _boot_shape(hybrid)
    assert {entry[0] for entry in torito} == {"BIOS", "UEFI"}  # the fixture is a hybrid
    assert _boot_shape(copy) == (torito, kinds, gpt)
    assert _label(copy) == "BENTOO_WORKER"
    assert "/shidashi/identity/pairing.json" in _listing(copy)


# ===================================================================================
# replace (R1.7)
# ===================================================================================


@needs_tools
def test_replace_regenerates_the_key_and_drops_the_old_pin(real: _Env) -> None:
    first = _run(real, _Runner())
    old_pub = (real.workers_dir / NAME / "identity/ssh/ssh_host_ed25519_key.pub").read_text()
    second = _run(real, _Runner(), replace=True)

    assert second.fingerprint != first.fingerprint
    known_hosts = real.workers_dir / "known_hosts"
    new_pub = (real.workers_dir / NAME / "identity/ssh/ssh_host_ed25519_key.pub").read_text()
    assert _ssh_keygen_find(known_hosts, NAME) == [" ".join(new_pub.split()[:2])]
    assert old_pub.split()[1] not in known_hosts.read_text()  # an old medium is refused
    entry = workers.load_registry(real.workers_dir / "workers.json")[NAME]
    assert entry.host_key_fingerprint == second.fingerprint
    medium = _extract(Path(second.iso_path), "/shidashi/identity", real.dist / "medium")
    assert (medium / "ssh/ssh_host_ed25519_key.pub").read_text() == new_pub
    assert sorted(p.name for p in real.workers_dir.iterdir()) == sorted(
        ["id_ed25519", "id_ed25519.pub", "known_hosts", "workers.json", NAME]
    )


# ===================================================================================
# rollback (R1.8)
# ===================================================================================


def _partial_write(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """xorriso fails mid-write, leaving a partial image at its output."""
    if not _writes_copy(argv):
        return None
    target = _arg_after(argv, "-outdev")
    assert target is not None
    target.write_bytes(bytes(417792))
    return subprocess.CompletedProcess(
        argv, 32, "", "libburn : FAILURE : Premature end of input encountered.\n"
    )


def _keygen_fails(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not _is_keygen(argv):
        return None
    return subprocess.CompletedProcess(argv, 1, "", "ssh-keygen: generating key: No entropy\n")


def _assert_nothing_changed(
    caught: pytest.ExceptionInfo[Any], before: dict[str, Any], tmp_path: Path, detail: str
) -> None:
    message = str(caught.value)
    assert "provision failed at" in message
    assert "nothing was changed" in message
    assert detail in message
    assert _snapshot(tmp_path) == before


@needs_tools
@pytest.mark.parametrize(
    ("hook", "detail"),
    [(_partial_write, "Premature end of input"), (_keygen_fails, "No entropy")],
    ids=["xorriso-write", "ssh-keygen"],
)
def test_a_failing_step_leaves_no_copy_no_pin_and_no_record(
    tmp_path: Path, real: _Env, hook: Before, detail: str
) -> None:
    prov = _provision()
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(real, _Runner(before=hook))
    _assert_nothing_changed(caught, before, tmp_path, detail)


def _copy_lost_its_boot(source: Path) -> After:
    """The copy's reports say it has no El Torito boot entry any more."""

    def after(argv: list[str], done: subprocess.CompletedProcess[Any]) -> None:
        indev = _arg_after(argv, "-indev")
        if not _is_xorriso(argv) or indev is None or indev.resolve() == source.resolve():
            return
        if not isinstance(done.stdout, (str, bytes)):
            return
        text = done.stdout if isinstance(done.stdout, str) else done.stdout.decode()
        kept = "\n".join(ln for ln in text.splitlines() if "El Torito" not in ln) + "\n"
        done.stdout = kept if isinstance(done.stdout, str) else kept.encode()

    return after


def _copy_lost_its_label(argv: list[str], done: subprocess.CompletedProcess[Any]) -> None:
    """The write succeeds but the copy declares another volume."""
    target = _arg_after(argv, "-outdev")
    if _writes_copy(argv) and done.returncode == 0 and target is not None:
        with target.open("r+b") as handle:
            handle.seek(LABEL_OFFSET)
            handle.write(b"BENTOO_KDE".ljust(32, b" "))


@needs_tools
def test_a_copy_that_lost_its_boot_entry_is_refused_and_removed(tmp_path: Path, real: _Env) -> None:
    """GOTCHA of design.md: a copy without El Torito no longer boots from the stick."""
    prov = _provision()
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(real, _Runner(after=_copy_lost_its_boot(real.iso)))
    assert "nothing was changed" in str(caught.value)
    assert _snapshot(tmp_path) == before


@needs_tools
def test_a_copy_with_another_label_is_refused_and_removed(tmp_path: Path, real: _Env) -> None:
    """GOTCHA of design.md: ``root=live:CDLABEL=BENTOO_WORKER`` finds no other label."""
    prov = _provision()
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(real, _Runner(after=_copy_lost_its_label))
    assert "nothing was changed" in str(caught.value)
    assert _snapshot(tmp_path) == before


@needs_tools
def test_a_failed_replace_keeps_the_previous_identity_pin_and_record(
    tmp_path: Path, real: _Env
) -> None:
    """R1.8 with R1.7: the old medium must keep working when its replacement failed."""
    prov = _provision()
    _run(real, _Runner())
    before = _snapshot(tmp_path)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(real, _Runner(before=_partial_write), replace=True)
    _assert_nothing_changed(caught, before, tmp_path, "Premature end of input")


@needs_tools
@pytest.mark.parametrize("replace", [False, True], ids=["first", "replace"])
def test_a_failing_registry_write_undoes_the_copy_and_the_pin(
    tmp_path: Path, real: _Env, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    """The last step fails after the copy is in place and N is pinned: both are undone
    -- a pin without its record (or the reverse) is a half-provisioned worker."""
    prov = _provision()
    if replace:
        _run(real, _Runner())
    before = _snapshot(tmp_path)

    def _disk_full(*_a: object, **_k: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(workers, "save_registry", _disk_full)
    monkeypatch.setattr(prov, "save_registry", _disk_full, raising=False)
    with pytest.raises(prov.ProvisionError) as caught:
        _run(real, _Runner(), replace=replace)
    _assert_nothing_changed(caught, before, tmp_path, "No space left on device")
