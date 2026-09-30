"""Unit tests of shidashi.system -- the image's configuration, the Handbook as data (F79)."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, system
from shidashi.container import CommandResult

_UNITS = ("NetworkManager.service", "systemd-timesyncd.service", "plasmalogin.service",
          "systemd-resolved.service", "systemd-networkd.service", "sshd.service",
          "systemd-homed.service")


def _image(tmp_path: Path) -> Path:
    """A rootfs with what apply_* reads: zoneinfo, unit files, groups, shadow."""
    root = tmp_path / "rootfs"
    (root / "usr/share/zoneinfo").mkdir(parents=True)
    (root / "usr/share/zoneinfo/UTC").write_text("TZif")
    units = root / "usr/lib/systemd/system"
    units.mkdir(parents=True)
    for unit in _UNITS:
        (units / unit).write_text("[Unit]\n")
    (root / "usr/share/wayland-sessions").mkdir(parents=True)
    (root / "usr/share/wayland-sessions/plasma.desktop").write_text("[Desktop Entry]\n")
    etc = root / "etc"
    etc.mkdir()
    (etc / "group").write_text("root:x:0:\nwheel:x:10:\naudio:x:18:\nvideo:x:27:\nusers:x:100:\n")
    (etc / "passwd").write_text("root:x:0:0::/root:/bin/bash\n")
    (etc / "shadow").write_text("root:*:20000::::::\n")
    (etc / "hosts").write_text("127.0.0.1\tlocalhost\n")
    (etc / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (etc / "resolv.conf").write_text("nameserver 8.8.8.8\n")
    (root / "usr/lib/os-release").write_text("NAME='Gentoo'\n")
    (etc / "os-release").symlink_to("../usr/lib/os-release")  # baselayout's
    return root


class _Image:
    """Plays what the commands do inside the image."""

    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs
        self.calls: list[list[str]] = []

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, check: bool = True
    ) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        etc = self.rootfs / "etc"
        if argv[:3] == ["eselect", "locale", "set"]:
            (etc / "locale.conf").write_text(f'LANG="{argv[3]}"\n')
        elif argv[:2] == ["systemctl", "preset-all"]:
            preset = (etc / "systemd/system-preset/80-bentoo.preset").read_text().splitlines()
            wants = etc / "systemd/system/multi-user.target.wants"
            wants.mkdir(parents=True, exist_ok=True)
            for line in preset:
                if line.startswith("enable "):
                    unit = line.split()[1]
                    if not system.unit_exists(self.rootfs, unit):
                        continue
                    if unit == "plasmalogin.service":
                        (etc / "systemd/system/display-manager.service").symlink_to(
                            f"/usr/lib/systemd/system/{unit}")
                    else:
                        (wants / unit).symlink_to(f"/usr/lib/systemd/system/{unit}")
        elif argv[0] == "useradd":
            user = argv[-1]
            with (etc / "passwd").open("a") as f:
                f.write(f"{user}:x:1000:1000::/home/{user}:/bin/bash\n")
            with (etc / "shadow").open("a") as f:
                f.write(f"{user}:!:20000:0:99999:7:::\n")
        return CommandResult(0, "", "")


def _kde(init: str = "systemd") -> system.SystemConfig:
    recipe = config.load_recipe("v3", "kde", init)
    return system.load_system_config(recipe, variants_dir=config.variants_dir())


def test_the_layers_merge_scalars_override_and_lists_add_up() -> None:
    cfg = _kde()
    assert (cfg.hostname, cfg.locale, cfg.keymap, cfg.timezone) == (
        "bentoo", "en_US.UTF-8", "us", "UTC")
    # the base's services, then kde's display manager after them
    assert cfg.services.systemd.enable[0] == "NetworkManager.service"
    assert cfg.services.systemd.enable[-1] == "plasmalogin.service"
    assert cfg.live.user == "bentoo" and cfg.live.session == "plasma.desktop"
    assert system.display_manager(cfg, init="systemd") == "plasmalogin"
    assert system.display_manager(_kde("openrc"), init="openrc") == "sddm"


def test_minimal_has_no_display_manager_and_logs_in_on_the_console() -> None:
    recipe = config.load_recipe("v3", "minimal", "systemd")
    cfg = system.load_system_config(recipe, variants_dir=config.variants_dir())
    assert "plasmalogin.service" not in cfg.services.systemd.enable
    assert cfg.live.session is None


def test_merge_is_pure_and_adds_list_items_once() -> None:
    base = {"a": 1, "l": ["x", "y"], "m": {"k": 1}}
    assert system._merge(base, {"a": 2, "l": ["y", "z"], "m": {"j": 2}}) == {
        "a": 2, "l": ["x", "y", "z"], "m": {"k": 1, "j": 2}}
    assert base == {"a": 1, "l": ["x", "y"], "m": {"k": 1}}


def _configure(tmp_path: Path, cfg: system.SystemConfig) -> tuple[_Image, dict[str, Any]]:
    image = _Image(_image(tmp_path))
    done = system.apply_system(image, cfg, init="systemd")
    done.update(system.apply_live(image, cfg, init="systemd", hasher=lambda p: "$6$salt$h"))
    # nspawn rewrites resolv.conf at every command: the build does this last
    (image.rootfs / "etc/resolv.conf").write_text("nameserver 8.8.8.8\n")
    done.update(system.finalize(image.rootfs, cfg, init="systemd"))
    return image, done


def test_apply_then_verify_leaves_nothing_missing(tmp_path: Path) -> None:
    cfg = _kde()
    image, done = _configure(tmp_path, cfg)
    etc = image.rootfs / "etc"
    assert system.verify(image.rootfs, cfg, init="systemd", live=True) == []
    assert (etc / "hostname").read_text() == "bentoo\n"
    assert "127.0.1.1\tbentoo" in (etc / "hosts").read_text()
    assert str((etc / "localtime").readlink()) == "../usr/share/zoneinfo/UTC"
    assert (etc / "vconsole.conf").read_text() == "KEYMAP=us\n"
    # the build host's DNS gone; names resolve through resolved, fed by NetworkManager
    assert str((etc / "resolv.conf").readlink()) == "../run/systemd/resolve/stub-resolv.conf"
    assert (etc / "sudoers.d/10-wheel").read_text() == "%wheel ALL=(ALL:ALL) ALL\n"
    assert (etc / "sudoers.d/10-wheel").stat().st_mode & 0o777 == 0o440
    assert (etc / "machine-id").read_text() == ""
    assert (etc / "plasmalogin.conf.d/90-bentoo-live.conf").read_text() == (
        "[Autologin]\nUser=bentoo\nSession=plasma.desktop\nRelogin=false\n")
    # only the groups the image has; the password never on a command line
    useradd = next(c for c in image.calls if c[0] == "useradd")
    assert useradd[useradd.index("--groups") + 1] == "users,wheel,audio,video"
    assert not any("bentoo" in arg for call in image.calls for arg in call[:-1]
                   if call[0] != "useradd")
    assert "bentoo:$6$salt$h:" in (etc / "shadow").read_text()
    assert done["disabled"] == ["systemd-networkd.service", "systemd-homed.service",
                                "sshd.service"]
    assert "bluetooth.service" in done["skipped"]  # not installed in this image


def test_verify_names_every_declared_thing_that_is_missing(tmp_path: Path) -> None:
    cfg = _kde()
    image, _ = _configure(tmp_path, cfg)
    etc = image.rootfs / "etc"
    (etc / "hostname").write_text("gentoo\n")
    (etc / "systemd/system/multi-user.target.wants/NetworkManager.service").unlink()
    (etc / "systemd/system/display-manager.service").unlink()
    (etc / "machine-id").chmod(0o644)  # 0444 like the real one; root would not care
    (etc / "machine-id").write_text("fixed\n")
    (etc / "plasmalogin.conf.d/90-bentoo-live.conf").unlink()
    problems = system.verify(image.rootfs, cfg, init="systemd", live=True)
    assert problems == [
        "/etc/hostname is 'gentoo', not 'bentoo'",
        "NetworkManager.service is not enabled",
        "plasmalogin.service is not enabled",
        "/etc/machine-id is not empty",
        "plasmalogin autologin for bentoo is missing",
    ]


def test_without_a_display_manager_the_console_logs_the_live_user_in(tmp_path: Path) -> None:
    recipe = config.load_recipe("v3", "minimal", "systemd")
    cfg = system.load_system_config(recipe, variants_dir=config.variants_dir())
    image, done = _configure(tmp_path, cfg)
    assert done["autologin"] == "console:tty1"
    dropin = image.rootfs / "etc/systemd/system/getty@tty1.service.d/autologin.conf"
    assert "--autologin bentoo" in dropin.read_text()
    assert system.verify(image.rootfs, cfg, init="systemd", live=True) == []


def test_an_unknown_timezone_refuses_to_configure(tmp_path: Path) -> None:
    cfg = _kde().model_copy(update={"timezone": "Mars/Olympus"})
    with pytest.raises(system.ConfigurationError, match="Mars/Olympus"):
        system.apply_system(_Image(_image(tmp_path)), cfg, init="systemd")


def test_hash_password_is_sha512_crypt() -> None:
    hashed = system.hash_password("bentoo")
    assert hashed.startswith("$6$") and "bentoo" not in hashed


def test_a_broken_system_yaml_fails_loading(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base/system.yaml").write_text("hostname: x\nunknown_key: 1\n")
    recipe = config.load_recipe("v3", "minimal", "systemd")
    with pytest.raises(system.ConfigurationError):
        system.load_system_config(recipe, variants_dir=tmp_path)


def test_os_release_names_bentoo_in_etc_and_leaves_baselayouts_file(tmp_path: Path) -> None:
    """KDE's Welcome Center said "Welcome to the Gentoo operating system"."""
    cfg = _kde()
    image = _Image(_image(tmp_path))
    system.apply_system(image, cfg, init="systemd",
                        build={"VERSION_ID": "2026.09.30", "VARIANT": "Kde", "VARIANT_ID": "kde"})
    release = image.rootfs / "etc/os-release"
    assert not release.is_symlink()
    text = release.read_text()
    assert 'NAME="Bentoo"' in text and 'ID_LIKE="gentoo"' in text
    assert 'VARIANT="KDE Plasma"' in text  # the flavor's own name wins
    assert 'VERSION_ID="2026.09.30"' in text and 'VARIANT_ID="kde"' in text
    assert (image.rootfs / "usr/lib/os-release").read_text() == "NAME='Gentoo'\n"


def test_verify_notices_an_image_that_calls_itself_gentoo(tmp_path: Path) -> None:
    cfg = _kde()
    image, _ = _configure(tmp_path, cfg)
    (image.rootfs / "etc/os-release").write_text("NAME=Gentoo\n")
    assert system.verify(image.rootfs, cfg, init="systemd", live=True) == [
        "/etc/os-release does not name the system Bentoo"]


def test_render_os_release_escapes_like_a_shell() -> None:
    cfg = _kde().model_copy(update={"os_release": {"NAME": 'A "b" $c `d` \\e'}})
    assert system.render_os_release(cfg, {}) == 'NAME="A \\"b\\" \\$c \\`d\\` \\\\e"\n'


def test_the_systemd_layer_generates_ssh_host_keys_before_socket_activated_sshd() -> None:
    """The first vsock SSH into the live ISO was closed at once: sshd@.service
    never generates host keys and the image has none (2026-09-30)."""
    units = config.variants_dir() / "init/systemd/rootfs/etc/systemd/system"
    keygen = (units / "sshd-keygen.service").read_text()
    assert "ExecStart=/usr/bin/ssh-keygen -A" in keygen and "Type=oneshot" in keygen
    assert "Before=sshd.service sshd@.service" in keygen
    dropin = (units / "sshd@.service.d/10-keygen.conf").read_text()
    assert "Wants=sshd-keygen.service" in dropin and "After=sshd-keygen.service" in dropin


def test_verify_catches_the_build_hosts_resolv_conf(tmp_path: Path) -> None:
    """F81: set before the last container command, the image's resolv.conf was
    overwritten by nspawn's copy of the host's -- and nothing checked it."""
    cfg = _kde()
    image, _ = _configure(tmp_path, cfg)
    resolv = image.rootfs / "etc/resolv.conf"
    resolv.unlink()
    resolv.write_text("nameserver 8.8.8.8\n")
    assert system.verify(image.rootfs, cfg, init="systemd", live=True) == [
        "/etc/resolv.conf does not point at ../run/systemd/resolve/stub-resolv.conf"]


def test_without_resolved_finalize_writes_the_stub(tmp_path: Path) -> None:
    cfg = _kde()
    root = _image(tmp_path)
    (root / "usr/lib/systemd/system/systemd-resolved.service").unlink()
    (root / "etc/resolv.conf").write_text("nameserver 8.8.8.8\n")
    assert system.finalize(root, cfg, init="systemd") == {"resolv_conf": "stub, written at boot"}
    assert "8.8.8.8" not in (root / "etc/resolv.conf").read_text()
