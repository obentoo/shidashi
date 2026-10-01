"""System configuration of an image, and the live medium's -- the Handbook as data.

The first ISO booted to a text login nobody could use: no service enabled, no
hostname, no account, a machine-id shared by every copy (F79). The pipeline had
implemented the Handbook's package chapters and none of its configuration ones.
This module is those chapters: ``variants/<layer>/system.yaml`` says what every
image is (hostname, locale, keymap, timezone, sudo, services per init) and
``variants/livecd.yaml`` what only the live medium adds (its user, autologin,
an empty machine-id, what its squashfs leaves out).

Three steps, each audited by the caller:

- :func:`load_system_config` merges the layers of a recipe (scalars override,
  lists add up, so a flavor adds its display manager to the base's services);
- :func:`apply_system` / :func:`apply_live` write files from the host and run the
  commands only the image can run (eselect, systemctl, rc-update, useradd) in
  its container;
- :func:`verify` reads the result back and names every declared thing that is
  not there. The assembler fails on any, so a configuration that silently did
  not apply cannot ship again.
"""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict

from shidashi.container import CommandResult
from shidashi.recipe import ResolvedRecipe

_STRICT = ConfigDict(frozen=True, extra="forbid")

SYSTEM_FILE = "system.yaml"
#: The live medium's file: top-level, like flow.yaml -- one live, no layers.
LIVECD_FILE = "livecd.yaml"

#: Display managers this module knows how to autologin, by systemd unit.
_DM_BY_UNIT = {"plasmalogin.service": "plasmalogin", "sddm.service": "sddm"}
#: Their drop-in directories (both read ``[Autologin] User= Session=``: plasma-
#: login-manager is SDDM's fork; the keys were read from its binary, 2026-09-30).
_DM_CONF_DIR = {"plasmalogin": "etc/plasmalogin.conf.d", "sddm": "etc/sddm.conf.d"}
_UNIT_DIRS = ("etc/systemd/system", "usr/lib/systemd/system")
_PRESET = Path("etc/systemd/system-preset/80-bentoo.preset")
_SUDOERS_WHEEL = Path("etc/sudoers.d/10-wheel")
_LIVE_AUTOLOGIN_NAME = "90-bentoo-live.conf"
_GETTY_AUTOLOGIN = Path("etc/systemd/system/getty@tty1.service.d/autologin.conf")


class ConfigurationError(Exception):
    """A system.yaml that does not load, or a configuration that did not apply."""


class _Runner(Protocol):
    rootfs: Path

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, check: bool = True
    ) -> CommandResult: ...


class SystemdServices(BaseModel):
    model_config = _STRICT
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()


class OpenrcServices(BaseModel):
    model_config = _STRICT
    boot: tuple[str, ...] = ()
    default: tuple[str, ...] = ()


class Services(BaseModel):
    model_config = _STRICT
    systemd: SystemdServices = SystemdServices()
    openrc: OpenrcServices = OpenrcServices()


class LivecdFile(BaseModel):
    """``variants/livecd.yaml`` as written: the session per image, and the
    squashfs exclude list beside the live user."""

    model_config = _STRICT
    user: str
    password: str
    full_name: str = ""
    shell: str = "/bin/bash"
    groups: tuple[str, ...] = ()
    autologin: bool = True
    #: Image (target) -> its graphical session; an image not listed has none.
    sessions: dict[str, str] = {}
    empty_machine_id: bool = True
    #: ``mksquashfs -wildcards -ef`` patterns, relative to the rootfs.
    squashfs_exclude: tuple[str, ...] = ()


class LiveConfig(BaseModel):
    model_config = _STRICT
    user: str
    password: str
    full_name: str = ""
    shell: str = "/bin/bash"
    groups: tuple[str, ...] = ()
    autologin: bool = True
    #: The display-manager session to log into; ``None`` logs in on the console.
    session: str | None = None
    empty_machine_id: bool = True


class SystemConfig(BaseModel):
    model_config = _STRICT
    hostname: str
    locale: str
    keymap: str
    timezone: str
    sudo_wheel: bool = False
    services: Services = Services()
    #: OpenRC's display-manager service starts the one named per init here.
    display_manager: dict[str, str] = {}
    #: /etc/os-release's static fields; the build adds its own (render_os_release).
    os_release: dict[str, str] = {}
    live: LiveConfig


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """``over`` on top of ``base``: scalars replace, lists add (in order, once),
    mappings merge. Pure."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _merge(dict(out[key]), value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = [*out[key], *(v for v in value if v not in out[key])]
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as err:
        raise ConfigurationError(f"{path}: {err}") from err
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}: expected a mapping")
    return data


def load_livecd(variants_dir: Path) -> LivecdFile:
    """``variants/livecd.yaml``, validated. I/O."""
    path = variants_dir / LIVECD_FILE
    try:
        return LivecdFile.model_validate(_read_yaml(path))
    except ValueError as err:
        raise ConfigurationError(f"{path}: {err}") from err


def load_system_config(recipe: ResolvedRecipe, *, variants_dir: Path) -> SystemConfig:
    """The merged system configuration of ``recipe``'s layers, with the live
    medium's from ``variants/livecd.yaml``.

    Order: the base, then the init, then the rest -- the init's file holds what
    every image of that init enables, so a stage (kde's display manager) adds to
    it and overrides it, the more specific layer last.
    """
    livecd = load_livecd(variants_dir)
    live = livecd.model_dump(exclude={"sessions", "squashfs_exclude"})
    merged: dict[str, Any] = {"live": {**live, "session": livecd.sessions.get(recipe.flavor)}}
    init_layer = f"init/{recipe.init}"
    head = [layer for layer in recipe.portage_layers if layer == "base"]
    rest = [layer for layer in recipe.portage_layers if layer not in ("base", init_layer)]
    for layer in (*head, init_layer, *rest):
        path = variants_dir / layer / SYSTEM_FILE
        if path.is_file():
            data = _read_yaml(path)
            if "live" in data:
                raise ConfigurationError(
                    f"{path}: `live:` belongs in {LIVECD_FILE} (the session: `sessions:`)"
                )
            merged = _merge(merged, data)
    try:
        return SystemConfig.model_validate(merged)
    except ValueError as err:
        raise ConfigurationError(f"system configuration of {recipe.flavor}: {err}") from err


# --- applying -------------------------------------------------------------------------


def unit_exists(rootfs: Path, unit: str) -> bool:
    """Whether the image has a unit file named ``unit``. I/O."""
    return any((rootfs / d / unit).exists() for d in _UNIT_DIRS)


def preset_lines(cfg: SystemConfig) -> list[str]:
    """``80-bentoo.preset``: the enables, then the disables. Pure."""
    return [
        "# Written by shidashi (variants/*/system.yaml). Read before 90-systemd.preset.",
        *(f"enable {u}" for u in cfg.services.systemd.enable),
        *(f"disable {u}" for u in cfg.services.systemd.disable),
    ]


def _write(path: Path, text: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _set_hosts(rootfs: Path, hostname: str) -> None:
    hosts = rootfs / "etc" / "hosts"
    lines = hosts.read_text(encoding="utf-8").splitlines() if hosts.is_file() else []
    entry = f"127.0.1.1\t{hostname}"
    if entry not in lines:
        lines.append(entry)
    _write(hosts, "\n".join(lines) + "\n")


def render_os_release(cfg: SystemConfig, build: Mapping[str, str]) -> str:
    """/etc/os-release: the static fields of system.yaml, then the build's. Pure.

    Values are double-quoted with ``\\``, ``"``, ``$`` and backquote escaped,
    as os-release(5) asks of shell-compatible assignments.
    """

    def quote(value: str) -> str:
        escaped = "".join("\\" + c if c in '\\"$`' else c for c in value)
        return f'"{escaped}"'

    fields = {**cfg.os_release, **{k: v for k, v in build.items() if k not in cfg.os_release}}
    return "".join(f"{key}={quote(value)}\n" for key, value in fields.items())


def apply_system(
    container: _Runner,
    cfg: SystemConfig,
    *,
    init: str,
    build: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Configure the image as ``cfg`` says. PRIVILEGED (runs in the container).

    ``build`` holds the build's own os-release fields (version, build id,
    variant). Returns what it did, for the audit trail.
    """
    rootfs = container.rootfs
    done: dict[str, Any] = {}

    if cfg.os_release:
        os_release = rootfs / "etc" / "os-release"
        os_release.unlink(missing_ok=True)  # baselayout's symlink to /usr/lib
        _write(os_release, render_os_release(cfg, build or {}))
        done["os_release"] = cfg.os_release.get("PRETTY_NAME", cfg.os_release.get("NAME"))

    _write(rootfs / "etc" / "hostname", cfg.hostname + "\n")
    _set_hosts(rootfs, cfg.hostname)

    localtime = rootfs / "etc" / "localtime"
    if not (rootfs / "usr/share/zoneinfo" / cfg.timezone).exists():
        raise ConfigurationError(f"timezone {cfg.timezone!r} is not in the image's zoneinfo")
    localtime.unlink(missing_ok=True)
    localtime.symlink_to(Path("../usr/share/zoneinfo") / cfg.timezone)
    if init == "openrc":
        _write(rootfs / "etc" / "timezone", cfg.timezone + "\n")

    if init == "systemd":
        _write(rootfs / "etc" / "vconsole.conf", f"KEYMAP={cfg.keymap}\n")
    else:
        _write(rootfs / "etc/conf.d/keymaps", f'keymap="{cfg.keymap}"\n')

    if cfg.sudo_wheel:
        _write(rootfs / _SUDOERS_WHEEL, "%wheel ALL=(ALL:ALL) ALL\n", mode=0o440)

    container.run(["eselect", "locale", "set", cfg.locale])
    container.run(["env-update"])
    done["locale"] = cfg.locale

    if init == "systemd":
        _write(rootfs / _PRESET, "\n".join(preset_lines(cfg)) + "\n")
        container.run(["systemctl", "preset-all", "--preset-mode=enable-only"])
        disable = [u for u in cfg.services.systemd.disable if unit_exists(rootfs, u)]
        if disable:
            container.run(["systemctl", "disable", *disable])
        done["enabled"] = [u for u in cfg.services.systemd.enable if unit_exists(rootfs, u)]
        done["disabled"] = disable
        done["skipped"] = [
            u
            for u in (*cfg.services.systemd.enable, *cfg.services.systemd.disable)
            if not unit_exists(rootfs, u)
        ]
    else:
        manager = cfg.display_manager.get("openrc")
        if manager:
            _write(rootfs / "etc/conf.d/display-manager", f'DISPLAYMANAGER="{manager}"\n')
        added: list[str] = []
        for level, names in (
            ("boot", cfg.services.openrc.boot),
            ("default", cfg.services.openrc.default),
        ):
            for name in names:
                if (rootfs / "etc/init.d" / name).exists():
                    container.run(["rc-update", "add", name, level])
                    added.append(f"{name}@{level}")
        done["enabled"] = added
    return done


#: /etc/resolv.conf under systemd-resolved: its stub, which NetworkManager feeds.
_RESOLVED_STUB = Path("../run/systemd/resolve/stub-resolv.conf")
_RESOLV_STUB_TEXT = "# Written at boot by the network manager (NetworkManager or dhcpcd).\n"


def uses_resolved(rootfs: Path, cfg: SystemConfig, *, init: str) -> bool:
    """Whether the image resolves names through systemd-resolved. I/O."""
    unit = "systemd-resolved.service"
    return init == "systemd" and unit in cfg.services.systemd.enable and unit_exists(rootfs, unit)


def finalize(rootfs: Path, cfg: SystemConfig, *, init: str) -> dict[str, Any]:
    """The files the build's own container keeps rewriting. Host-side, AFTER the
    last command in the container. I/O.

    nspawn's ``--resolv-conf=copy-host`` (the build's fetches need DNS) writes
    the BUILD HOST's resolv.conf into the rootfs at every command: set earlier,
    the image's own was overwritten, and the first ISO with F80's fix still
    shipped ``nameserver 8.8.8.8`` (F81).
    """
    resolv = rootfs / "etc" / "resolv.conf"
    resolv.unlink(missing_ok=True)
    if uses_resolved(rootfs, cfg, init=init):
        resolv.symlink_to(_RESOLVED_STUB)
        return {"resolv_conf": f"-> {_RESOLVED_STUB}"}
    _write(resolv, _RESOLV_STUB_TEXT)
    return {"resolv_conf": "stub, written at boot"}


def hash_password(password: str) -> str:
    """A SHA-512 crypt hash of ``password`` (``openssl passwd -6``, fed on stdin
    so the clear text is never an argument). I/O."""
    done = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _set_shadow_hash(rootfs: Path, user: str, hashed: str) -> None:
    shadow = rootfs / "etc" / "shadow"
    lines = shadow.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        fields = line.split(":")
        if fields[0] == user:
            fields[1] = hashed
            lines[i] = ":".join(fields)
            break
    else:
        raise ConfigurationError(f"{user} is not in /etc/shadow after useradd")
    shadow.write_text("\n".join(lines) + "\n", encoding="utf-8")


def display_manager(cfg: SystemConfig, *, init: str) -> str | None:
    """The display manager the image starts under ``init``, if any. Pure."""
    if init == "systemd":
        return next((_DM_BY_UNIT[u] for u in cfg.services.systemd.enable if u in _DM_BY_UNIT), None)
    return cfg.display_manager.get("openrc")


def apply_live(
    container: _Runner,
    cfg: SystemConfig,
    *,
    init: str,
    hasher: Callable[[str], str] = hash_password,
) -> dict[str, Any]:
    """Add what only the live medium has. PRIVILEGED. Returns what it did."""
    rootfs = container.rootfs
    live = cfg.live
    existing = {
        line.split(":", 1)[0]
        for line in (rootfs / "etc/group").read_text(encoding="utf-8").splitlines()
        if line
    }
    groups = [g for g in live.groups if g in existing]
    container.run(
        [
            "useradd",
            "--create-home",
            "--user-group",
            "--shell",
            live.shell,
            "--comment",
            live.full_name,
            "--groups",
            ",".join(groups),
            live.user,
        ]
    )
    _set_shadow_hash(rootfs, live.user, hasher(live.password))
    done: dict[str, Any] = {"user": live.user, "groups": groups}

    if live.autologin:
        manager = display_manager(cfg, init=init)
        if live.session and manager in _DM_CONF_DIR:
            _write(
                rootfs / _DM_CONF_DIR[manager] / _LIVE_AUTOLOGIN_NAME,
                f"[Autologin]\nUser={live.user}\nSession={live.session}\nRelogin=false\n",
            )
            done["autologin"] = f"{manager}:{live.session}"
        elif init == "systemd":
            _write(
                rootfs / _GETTY_AUTOLOGIN,
                "[Service]\nExecStart=\n"
                f"ExecStart=-/sbin/agetty -o '-p -f -- \\\\u' --noclear --autologin {live.user}"
                " %I $TERM\n",
            )
            done["autologin"] = "console:tty1"
        else:
            _write(
                rootfs / "etc/conf.d/agetty.tty1",
                f'agetty_options="--autologin {live.user} --noclear"\n',
            )
            done["autologin"] = "console:tty1"

    if live.empty_machine_id:
        _write(rootfs / "etc" / "machine-id", "", mode=0o444)
        done["machine_id"] = "empty"
    return done


# --- verifying ------------------------------------------------------------------------


def normalize_locale(text: str) -> str:
    """``en_US.UTF-8`` and ``en_US.utf8`` alike, as glibc treats them. Pure."""
    return text.lower().replace("-", "")


def _enabled(rootfs: Path, unit: str) -> bool:
    """Whether ``unit`` is linked into some target of /etc/systemd/system. I/O."""
    base = rootfs / "etc/systemd/system"
    if unit.endswith(".service") and unit in _DM_BY_UNIT:
        link = base / "display-manager.service"
        return link.is_symlink() and link.resolve().name == unit
    return any(base.glob(f"*.wants/{unit}")) or any(base.glob(f"*.requires/{unit}"))


def verify(rootfs: Path, cfg: SystemConfig, *, init: str, live: bool) -> list[str]:
    """Every declared thing the configured image does not have. I/O, read-only."""
    problems: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            problems.append(what)

    etc = rootfs / "etc"
    if cfg.os_release.get("NAME"):
        release = etc / "os-release"
        text = release.read_text() if release.is_file() else ""
        check(
            f'NAME="{cfg.os_release["NAME"]}"' in text,
            f"/etc/os-release does not name the system {cfg.os_release['NAME']}",
        )
    hostname = (etc / "hostname").read_text().strip() if (etc / "hostname").is_file() else None
    check(hostname == cfg.hostname, f"/etc/hostname is {hostname!r}, not {cfg.hostname!r}")
    localtime = etc / "localtime"
    check(
        localtime.is_symlink() and str(localtime.readlink()).endswith("/" + cfg.timezone),
        f"/etc/localtime does not point at {cfg.timezone}",
    )
    locale = (etc / "locale.conf").read_text() if (etc / "locale.conf").is_file() else ""
    check(
        normalize_locale(cfg.locale) in normalize_locale(locale),
        f"/etc/locale.conf does not set {cfg.locale}",
    )
    if cfg.sudo_wheel:
        check((rootfs / _SUDOERS_WHEEL).is_file(), f"/{_SUDOERS_WHEEL} is missing")
    resolv = etc / "resolv.conf"
    if uses_resolved(rootfs, cfg, init=init):
        check(
            resolv.is_symlink() and resolv.readlink() == _RESOLVED_STUB,
            f"/etc/resolv.conf does not point at {_RESOLVED_STUB}",
        )
    else:
        check(
            resolv.is_file() and resolv.read_text() == _RESOLV_STUB_TEXT,
            "/etc/resolv.conf is not the stub (the build host's leaked in?)",
        )
    if init == "systemd":
        vconsole = (etc / "vconsole.conf").read_text() if (etc / "vconsole.conf").is_file() else ""
        check(f"KEYMAP={cfg.keymap}" in vconsole, f"/etc/vconsole.conf lacks KEYMAP={cfg.keymap}")
        for unit in cfg.services.systemd.enable:
            if unit_exists(rootfs, unit):
                check(_enabled(rootfs, unit), f"{unit} is not enabled")
        for unit in cfg.services.systemd.disable:
            if unit_exists(rootfs, unit):
                check(not _enabled(rootfs, unit), f"{unit} is still enabled")
    else:
        for name in cfg.services.openrc.default:
            if (etc / "init.d" / name).exists():
                check(
                    (etc / "runlevels/default" / name).exists(),
                    f"{name} is not in the default runlevel",
                )
    if live:
        user = cfg.live.user
        passwd = (etc / "passwd").read_text() if (etc / "passwd").is_file() else ""
        check(
            any(line.startswith(f"{user}:") for line in passwd.splitlines()),
            f"live user {user} is not in /etc/passwd",
        )
        try:
            shadow = (etc / "shadow").read_text()
        except OSError:
            shadow = ""
        entry = next(
            (ln.split(":") for ln in shadow.splitlines() if ln.startswith(f"{user}:")), None
        )
        check(
            entry is not None and entry[1].startswith("$6$"),
            f"live user {user} has no SHA-512 password",
        )
        if cfg.live.empty_machine_id:
            machine_id = etc / "machine-id"
            check(
                machine_id.is_file() and machine_id.stat().st_size == 0,
                "/etc/machine-id is not empty",
            )
        if cfg.live.autologin:
            manager = display_manager(cfg, init=init)
            if cfg.live.session and manager in _DM_CONF_DIR:
                conf = rootfs / _DM_CONF_DIR[manager] / _LIVE_AUTOLOGIN_NAME
                check(
                    conf.is_file() and f"User={user}" in conf.read_text(),
                    f"{manager} autologin for {user} is missing",
                )
                session = rootfs / "usr/share/wayland-sessions" / cfg.live.session
                xsession = rootfs / "usr/share/xsessions" / cfg.live.session
                check(
                    session.is_file() or xsession.is_file(),
                    f"autologin session {cfg.live.session} is not installed",
                )
    return problems
