"""Tests of the worker image's boot units for a provisioned identity (story 020, task 2.2),
read from the shipped variants/worker tree like tests/test_worker_rootfs.py.

A new oneshot, ``shidashi-worker-identity.service``, restores the identity a host wrote
onto the boot medium (dracut mounts it at ``/run/initramfs/live``) before the work-disk
restore and the pairing window. Both of those are skipped once the RAM record
``/run/shidashi/pairing.json`` exists, so the medium wins over the work disk (R2.4), and
nothing changes for a medium without an identity (R2.5).

systemd lets a key repeat (two ``ConditionPathExists=`` lines are both checked, ANDed);
``configparser`` would keep only the last, so the units are read here as lists of values.

Requirements exercised: R2.2, R2.4, R2.5.
"""

from pathlib import Path

from shidashi.system import unit_exists
from tests.test_worker_rootfs import (
    LISTENER,
    LISTENER_NAME,
    ROOTFS,
    UNIT,
    UNIT_NAME,
    _point_at_real_variants,
    _services,
)

#: Autouse fixture of tests/test_worker_rootfs.py (SHIDASHI_VARIANTS_DIR -> the real tree);
#: naming it here marks the import as used.
_FIXTURES = (_point_at_real_variants,)

IDENTITY_NAME = "shidashi-worker-identity.service"
IDENTITY = ROOTFS / "etc" / "systemd" / "system" / IDENTITY_NAME
MEDIUM_RECORD = "/run/initramfs/live/shidashi/identity/pairing.json"
RAM_RECORD = "/run/shidashi/pairing.json"
DISK_RECORD = "/mnt/work/.shidashi/pairing.json"

Directives = dict[str, dict[str, list[str]]]


def _directives(path: Path) -> Directives:
    """Every ``Key=value`` of a unit file, per section, in order, repeats kept."""
    sections: Directives = {}
    current: dict[str, list[str]] | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"{path}: {line!r} outside a section"
        key, sep, value = line.partition("=")
        assert sep, f"{path}: {line!r} is not Key=value"
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def _words(unit: Directives, section: str, key: str) -> list[str]:
    """A list-valued directive (After=, Before=, WantedBy=...) across all its lines."""
    return [word for value in unit.get(section, {}).get(key, []) for word in value.split()]


def _argv(unit: Directives) -> list[str]:
    (exec_start,) = unit["Service"]["ExecStart"]
    return exec_start.lstrip("-@:+!").split()


# --- the identity unit ---------------------------------------------------------------------


def test_identity_unit_runs_only_when_the_medium_carries_an_identity() -> None:
    """Hostile first: conditioned on the RAM or the disk record it would run on every
    paired boot, or never; a medium without an identity must leave the boot as today."""
    conditions = _directives(IDENTITY)["Unit"]["ConditionPathExists"]
    assert RAM_RECORD not in conditions and f"!{RAM_RECORD}" not in conditions
    assert DISK_RECORD not in conditions
    assert conditions == [MEDIUM_RECORD]


def test_identity_unit_is_a_oneshot_after_local_filesystems_and_resolved() -> None:
    unit = _directives(IDENTITY)
    assert unit["Service"]["Type"] == ["oneshot"]
    after = _words(unit, "Unit", "After")
    assert "local-fs.target" in after
    assert "systemd-resolved.service" in after  # it writes a .dnssd and reloads resolved
    # a worker with no work disk still gets its identity
    assert not _words(unit, "Unit", "RequiresMountsFor")


def test_identity_unit_runs_the_worker_script_in_restore_medium_mode() -> None:
    """Hostile first: ``--restore`` would read the work disk, not the medium."""
    argv = _argv(_directives(IDENTITY))
    assert "--restore" not in argv
    assert argv == [
        "/usr/bin/python3",
        "/usr/local/lib/shidashi/kyomei_worker.py",
        "--restore-medium",
    ]


def test_identity_unit_runs_before_the_disk_restore_and_the_pairing_window() -> None:
    """R2.2/R2.4: the other two check the RAM record when they start, so the medium
    restore must have written it by then. Hostile first: an identity unit ordered after
    either one (or both ways round: a cycle systemd breaks by dropping a unit)."""
    identity = _directives(IDENTITY)
    restore = _directives(UNIT)
    listener = _directives(LISTENER)
    identity_after = _words(identity, "Unit", "After")
    for other_name, other in ((UNIT_NAME, restore), (LISTENER_NAME, listener)):
        assert other_name not in identity_after, other_name
        assert IDENTITY_NAME not in _words(other, "Unit", "Before"), other_name
        assert other_name in _words(identity, "Unit", "Before") or IDENTITY_NAME in _words(
            other, "Unit", "After"
        ), other_name
    # and the pairing window still skips on the RAM record the medium restore writes
    assert listener["Unit"]["ConditionPathExists"] == [f"!{RAM_RECORD}"]


def test_identity_unit_does_not_pull_sshd_in() -> None:
    """Condition*= does not stop what Wants=/Requires= pull in: a medium without an
    identity must not start sshd. The script starts it itself."""
    unit = _directives(IDENTITY)["Unit"]
    for key in ("Wants", "Requires", "BindsTo", "Upholds"):
        assert not any("sshd" in value for value in unit.get(key, [])), key


def test_identity_unit_can_be_enabled_and_the_worker_image_enables_it() -> None:
    assert _words(_directives(IDENTITY), "Install", "WantedBy") == ["multi-user.target"]
    assert unit_exists(ROOTFS, IDENTITY_NAME)
    services = _services()
    enabled = services.enable  # type: ignore[attr-defined]
    assert IDENTITY_NAME in enabled
    # the fallback paths stay enabled, sshd stays off until a restore or a pairing
    assert UNIT_NAME in enabled
    assert LISTENER_NAME in enabled
    assert "sshd.service" not in enabled
    assert "sshd.service" in services.disable  # type: ignore[attr-defined]


# --- the units that stay -------------------------------------------------------------------


def test_disk_restore_is_skipped_once_a_pairing_is_in_ram_and_still_needs_the_disk_record() -> None:
    """R2.4: the medium identity wins and the work disk is not touched. Hostile first:
    replacing the disk condition instead of adding one would run the disk restore on a
    worker that has no persisted pairing (R2.5)."""
    conditions = _directives(UNIT)["Unit"]["ConditionPathExists"]
    assert DISK_RECORD in conditions
    assert f"!{RAM_RECORD}" in conditions
    assert sorted(conditions) == sorted([DISK_RECORD, f"!{RAM_RECORD}"])
