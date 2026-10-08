"""Tests of the worker image's boot files, read from the shipped variants/worker tree (v3).

No tree is built: like tests/test_variants.py, these read the repository's real
``variants/`` and prove what the worker boots with -- the work disk mounted by LABEL
(and a boot without it that neither waits nor touches a disk), the restore unit that
brings a persisted pairing back before sshd, the announcing listener that starts only
when nothing was restored, mDNS switched on for resolved and NetworkManager, the
console entry that opens a new pairing window, and no ``shidashi.trust`` parameter
baked into the public image.

Requirements exercised: R3.1, R3.7, R3.8, R3.9, R5.1, R5.2 (and the restore path of
R5.4), R7.7.
"""

import configparser
import os
import re
from pathlib import Path

import pytest

from shidashi import config, image
from shidashi.system import load_system_config, unit_exists


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "variants" / "worker").is_dir():
            return parent
    raise RuntimeError("repository root not found above " + __file__)


ROOT = _repo_root()
VARIANTS = ROOT / "variants"
ROOTFS = VARIANTS / "worker" / "rootfs"
FSTAB = ROOTFS / "etc" / "fstab"
UNIT_NAME = "shidashi-worker-restore.service"
UNIT = ROOTFS / "etc" / "systemd" / "system" / UNIT_NAME
LISTENER_NAME = "shidashi-kyomei.service"
LISTENER = ROOTFS / "etc" / "systemd" / "system" / LISTENER_NAME
RESOLVED_DROPIN = ROOTFS / "etc" / "systemd" / "resolved.conf.d" / "10-shidashi-mdns.conf"
NM_DROPIN = ROOTFS / "etc" / "NetworkManager" / "conf.d" / "10-shidashi-mdns.conf"
ENTRY = ROOTFS / "usr" / "local" / "bin" / "shidashi"
LIB = ROOTFS / "usr" / "local" / "lib" / "shidashi"


@pytest.fixture(autouse=True)
def _point_at_real_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(VARIANTS))


def _fstab_entries() -> list[list[str]]:
    rows = []
    for line in FSTAB.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            rows.append(line.split())
    return rows


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(path.read_text())
    return parser


def _unit() -> configparser.ConfigParser:
    return _ini(UNIT)


def _listener() -> configparser.ConfigParser:
    return _ini(LISTENER)


def _services() -> object:
    recipe = config.load_recipe("v3", "worker", "systemd")
    return load_system_config(recipe, variants_dir=VARIANTS).services.systemd


# --- the work disk by label (R5.1, R5.2) --------------------------------------------


def test_fstab_mounts_the_shidashi_work_label_at_mnt_work_as_btrfs() -> None:
    work = [row for row in _fstab_entries() if row[1] == "/mnt/work"]
    assert len(work) == 1, work
    spec, _target, fstype, options, *_rest = work[0]
    assert spec == "LABEL=SHIDASHI-WORK"
    assert fstype == "btrfs"
    assert "noatime" in options.split(",")


def test_fstab_boots_without_the_disk_without_waiting_for_it() -> None:
    """Without nofail and a short device timeout, a worker with no SHIDASHI-WORK disk
    waits 90 s and drops to emergency mode."""
    (row,) = [row for row in _fstab_entries() if row[1] == "/mnt/work"]
    options = row[3].split(",")
    assert "nofail" in options
    assert "x-systemd.device-timeout=10s" in options


def test_fstab_never_names_a_disk_by_its_kernel_name() -> None:
    """A renamed device (sda <-> sdb) must never be mounted, let alone written."""
    for row in _fstab_entries():
        assert not row[0].startswith("/dev/sd") and not row[0].startswith("/dev/nvme"), row


# --- the restore unit (R5.4 at boot, R5.2 without a pairing) --------------------------


def test_the_restore_unit_is_a_oneshot_after_local_filesystems() -> None:
    unit = _unit()
    assert unit["Service"]["Type"] == "oneshot"
    assert "local-fs.target" in unit["Unit"]["After"].split()


def test_the_restore_unit_runs_only_when_a_pairing_was_persisted() -> None:
    assert _unit()["Unit"]["ConditionPathExists"] == "/mnt/work/.shidashi/pairing.json"


def test_the_restore_unit_waits_for_the_work_disk_without_requiring_it() -> None:
    """Regression (review of 2026-10-08): a ``nofail`` mount is only wanted by
    local-fs.target, not ordered before it (systemd.mount(5)). With After=local-fs.target
    alone, a late disk made the condition false: no restore, a fresh pairing window and,
    on an image without host keys, new ones the host's pin no longer matches.
    WantsMountsFor= orders the unit after the mount and still lets a worker with no disk
    boot: the condition is then simply false."""
    unit = _unit()["Unit"]
    assert unit["WantsMountsFor"].split() == ["/mnt/work"]
    assert "RequiresMountsFor" not in unit  # a missing disk must not fail the unit


def test_the_restore_unit_runs_the_worker_script_in_restore_mode() -> None:
    argv = _unit()["Service"]["ExecStart"].lstrip("-@:+!").split()
    script = next(a for a in argv if a.endswith("kyomei_worker.py"))
    assert "--restore" in argv
    assert script == "/usr/local/lib/shidashi/kyomei_worker.py"
    shipped = LIB / "kyomei_worker.py"
    assert shipped.is_file()
    if argv[0] == script:  # run directly: it needs its shebang and the exec bit
        assert shipped.read_text().startswith("#!")
        assert os.access(shipped, os.X_OK)


def test_the_restore_unit_runs_the_script_through_the_interpreter() -> None:
    """The scripts ship 0644: ExecStart names /usr/bin/python3, no shebang or exec bit."""
    argv = _unit()["Service"]["ExecStart"].lstrip("-@:+!").split()
    assert argv[0] == "/usr/bin/python3"
    assert argv[1:] == ["/usr/local/lib/shidashi/kyomei_worker.py", "--restore"]


def test_the_restore_unit_does_not_pull_sshd_in() -> None:
    """Condition*= does not stop what Wants=/Requires= pull in: with sshd there, a
    worker with no pairing would start sshd anyway."""
    unit = _unit()["Unit"]
    for key in ("Wants", "Requires", "BindsTo", "Upholds"):
        assert "sshd" not in unit.get(key, ""), key


def test_the_restore_unit_can_be_enabled() -> None:
    assert _unit()["Install"]["WantedBy"].strip()
    assert unit_exists(ROOTFS, UNIT_NAME)


# --- the announcing listener (R3.1, R3.9) ----------------------------------------------


def test_the_listener_unit_runs_the_worker_script_in_listen_mode() -> None:
    service = _listener()["Service"]
    assert service["Type"] == "simple"
    argv = service["ExecStart"].lstrip("-@:+!").split()
    assert argv == ["/usr/bin/python3", "/usr/local/lib/shidashi/kyomei_worker.py", "--listen"]
    assert service.get("Restart", "no") == "no"


def test_the_listener_unit_starts_after_the_network_resolved_and_the_restore() -> None:
    unit = _listener()["Unit"]
    after = unit["After"].split()
    for dependency in ("network-online.target", "systemd-resolved.service", UNIT_NAME):
        assert dependency in after, dependency
    assert "network-online.target" in unit.get("Wants", "").split()


def test_the_listener_unit_is_skipped_once_a_pairing_is_in_ram() -> None:
    """Hostile: the condition is on the RAM record a restore (or a pairing) writes -- not
    on the disk's record, which a worker paired RAM-only never has."""
    condition = _listener()["Unit"]["ConditionPathExists"]
    assert condition == "!/run/shidashi/pairing.json"


def test_the_listener_unit_does_not_pull_sshd_in() -> None:
    unit = _listener()["Unit"]
    for key in ("Wants", "Requires", "BindsTo", "Upholds"):
        assert "sshd" not in unit.get(key, ""), key


def test_the_listener_unit_can_be_enabled() -> None:
    assert _listener()["Install"]["WantedBy"].strip()
    assert unit_exists(ROOTFS, LISTENER_NAME)


def test_the_worker_image_enables_both_units_and_keeps_sshd_disabled() -> None:
    services = _services()
    assert UNIT_NAME in services.enable  # type: ignore[attr-defined]
    assert LISTENER_NAME in services.enable  # type: ignore[attr-defined]
    assert "sshd.service" not in services.enable  # type: ignore[attr-defined]
    assert "sshd.service" in services.disable  # type: ignore[attr-defined]
    assert "systemd-resolved.service" in services.enable  # type: ignore[attr-defined]


# --- mDNS on (R3.3) -------------------------------------------------------------------


def test_resolved_answers_mdns() -> None:
    assert _ini(RESOLVED_DROPIN)["Resolve"]["MulticastDNS"] == "yes"


def test_networkmanager_turns_mdns_on_for_every_link() -> None:
    """resolved announces only on links where mDNS is on per link too."""
    assert _ini(NM_DROPIN)["connection"]["connection.mdns"] == "2"


# --- the console entry (R3.8) ----------------------------------------------------------


def test_the_console_entry_opens_a_new_pairing_window_in_the_foreground() -> None:
    text = ENTRY.read_text()
    assert re.search(r"systemctl\s+stop\s+shidashi-kyomei(\.service)?", text), text
    assert re.search(r"python3\s+/usr/local/lib/shidashi/kyomei_worker\.py\s+--listen", text)
    assert "sudo" in text  # kyomei and disk-init still re-execute as root


def test_the_console_entry_hands_disk_init_to_python() -> None:
    text = ENTRY.read_text()
    assert "/usr/local/lib/shidashi/worker_disk.py" in text
    assert "disk-init" in text
    for name in ("kyomei_worker.py", "worker_disk.py", "kyomei_protocol.py"):
        assert (LIB / name).is_file(), name


def test_the_console_entry_no_longer_fetches_over_plain_http() -> None:
    text = ENTRY.read_text()
    assert "curl" not in text
    assert "http://" not in text
    assert "id_ed25519.pub" not in text
    assert "Host IP" not in text  # nothing is typed at the worker any more


# --- guards ------------------------------------------------------------------------------


def test_the_sshd_drop_in_refuses_passwords_and_keyboard_interactive() -> None:
    """[guard] R3.7: both prompts, not only the password one."""
    conf = ROOT / "variants/worker/rootfs/etc/ssh/sshd_config.d/10-shidashi-worker.conf"
    lines = {ln.strip() for ln in conf.read_text().splitlines()}
    assert "PasswordAuthentication no" in lines
    assert "KbdInteractiveAuthentication no" in lines


TRUST_VALUE = re.compile(r"shidashi\.trust=\s*\d")


def test_no_boot_entry_of_the_image_carries_a_trust_parameter() -> None:
    """[guard] R7.7: the image is public; a baked-in shidashi.trust would trust one
    maintainer's host on every machine that boots it."""
    menu = image._grub_cfg(  # the menu every ISO ships
        volume="BENTOO_WORKER", title="Bentoo worker", text_target=None, open_nvidia=True
    )
    assert not TRUST_VALUE.search(menu), menu
    for path in (VARIANTS / "worker").rglob("*"):
        if path.is_file() and path.suffix not in (".py",):
            assert not TRUST_VALUE.search(path.read_text(errors="replace")), path


# --- podman's storage on the work disk (review of 2026-10-08) -------------------------


def _storage_conf() -> dict[str, str]:
    import tomllib

    raw = tomllib.loads((ROOTFS / "etc" / "containers" / "storage.conf").read_text())
    return dict(raw["storage"])


def test_podman_stores_images_on_the_work_disk_not_in_the_live_ram() -> None:
    """The live root is RAM: without this, the first ~8 GB image podman pulls fills
    the memory that also holds the root overlay."""
    storage = _storage_conf()
    assert storage["graphroot"].startswith("/mnt/work/")
    assert storage["runroot"].startswith("/run/")  # runtime state: RAM, gone at boot
    assert storage["driver"] == "overlay"
