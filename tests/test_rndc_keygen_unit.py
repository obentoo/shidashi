"""Story 007, sub-task 3.2 -- rndc-keygen.service generates the key on the booted system.

The contract, from the story: when a booted system with net-dns/bind has no
/etc/bind/rndc.key, generate it ONCE (only when missing), BEFORE named starts
(a oneshot, so "before" means "finished before"), with ``rndc-confgen -a``,
owned root:named with mode 0640. An image without bind skips it. The SSH host
key generation (sshd-keygen.service) stays as it is.

The unit file is read as text, like the sshd-keygen test in tests/test_system.py;
`systemd-analyze verify` runs in the builder VM (the sub-task's Validation).
"""

import configparser
import re

from shidashi import config, system

_UNITS = "init/systemd/rootfs/etc/systemd/system"


def _unit(name: str) -> configparser.ConfigParser:
    text = (config.variants_dir() / _UNITS / name).read_text()
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
    parser.read_string(text)
    return parser


def _values(unit: configparser.ConfigParser, section: str, key: str) -> list[str]:
    """Every value of ``key`` (systemd accepts a key many times; configparser
    keeps the last, so the raw text is read for the repeated ones)."""
    if not unit.has_section(section):
        return []
    return [v for v in unit.get(section, key, fallback="").split() if v]


def _text() -> str:
    return (config.variants_dir() / _UNITS / "rndc-keygen.service").read_text()


def _directives(key: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(rf"^{key}=(.*)$", _text(), re.MULTILINE)]


def test_the_unit_generates_the_key_with_rndc_confgen_once_before_named() -> None:
    unit = _unit("rndc-keygen.service")
    assert "named.service" in _values(unit, "Unit", "Before")
    assert unit.get("Service", "Type") == "oneshot"
    commands = " ".join(_directives("ExecStart") + _directives("ExecStartPost"))
    assert re.search(r"\brndc-confgen\b[^\n]*\s-a\b", commands), commands
    # once: only when the key is missing
    assert "!/etc/bind/rndc.key" in _directives("ConditionPathExists")


def test_the_generated_key_is_root_named_0640() -> None:
    commands = " ".join(_directives("ExecStart") + _directives("ExecStartPost"))
    assert re.search(r"root:named|-o\s*root\b[^\n]*-g\s*named\b", commands), commands
    assert re.search(r"\b0?640\b", commands), commands


def test_an_image_without_bind_skips_the_unit() -> None:
    """The unit ships in every systemd image: without bind's rndc-confgen it must
    not run (a failed unit at every boot otherwise)."""
    conditions = _directives("ConditionPathExists") + _directives("ConditionFileIsExecutable")
    assert any(c.lstrip("|").endswith("/rndc-confgen") for c in conditions), conditions


def test_the_unit_is_enabled_for_an_image_that_carries_bind() -> None:
    """kde pulls net-dns/bind (variants/flavor/kde/world.systemd)."""
    recipe = config.load_recipe("v3", "kde", "systemd")
    cfg = system.load_system_config(recipe, variants_dir=config.variants_dir())
    assert "rndc-keygen.service" in cfg.services.systemd.enable
    assert "rndc-keygen.service" not in cfg.services.systemd.disable


def test_ssh_host_key_generation_is_unchanged() -> None:
    """Unchanged: sshd-keygen still generates the host keys before any sshd."""
    keygen = _unit("sshd-keygen.service")
    assert keygen.get("Service", "ExecStart") == "/usr/bin/ssh-keygen -A"
    assert _values(keygen, "Unit", "Before") == ["sshd.service", "sshd@.service"]
