"""``FakeCiToolbox``: stands in for podman and the toolbox container's QEMU, ``qemu-img``
and ``xorriso`` in the story 034 tests of ``shidashi.ci_guest``.

It is the ``runner`` (a ``subprocess.run`` look-alike) handed to ``ci_guest.prepare`` and
``ci_guest.run_job``. Nothing is booted: every command is recorded, and the few whose
effect the code under test reads back are imitated on the real filesystem -- the binds
are at the same path inside the container, so a path in a command is a host path:

- ``xorriso ... -o ISO`` creates ISO and records the ``user-data``/``meta-data`` it was
  given and the volume label (``-V``);
- ``qemu-img create ... PATH SIZE`` and ``qemu-img convert ... OUT`` create their output;
- ``qemu-system-x86_64`` calls :attr:`FakeCiToolbox.on_boot` (the test decides what the
  "guest" does) and writes what it returns to the ``-serial file:`` path; a ``hang``
  boot raises ``subprocess.TimeoutExpired`` when the caller gave a ``timeout``;
- ``podman container exists|inspect|ps`` answer from :attr:`FakeCiToolbox.alive` plus the
  container a ``podman run --name`` is running at that moment; every other podman verb
  (``image exists``, ``import``, ``rm``, ``kill``, ``stop``) succeeds.

The binary is found anywhere in the argv, so a command wrapped in ``podman run ... IMAGE``
and the same command run natively are seen alike.
"""

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shidashi import vmrunner

#: The marker the prepare boot's guest prints on its console once its prepare check has
#: proved Docker, the runner and both units in place (shared by
#: ``ci_guest_seed.prepare_user_data`` and ``ci_guest.prepare``; tests/test_ci_contract.py
#: checks both sides use it).
PREPARED_MARKER = "SHIDASHI-CI-PREPARED"
#: The line the job guest's runner unit prints on its console when the runner exits.
RESULT_PREFIX = "SHIDASHI-CI-RESULT="

_TOOLS = ("xorriso", "qemu-img", "qemu-system-x86_64")

#: The real preflight, taken before any test stands it in.
_REAL_PREFLIGHT = vmrunner.preflight


@dataclass(frozen=True)
class Boot:
    """What ``on_boot`` returns: QEMU's exit code and the guest's console text, or a hang."""

    rc: int = 0
    serial: str = ""
    hang: bool = False


@dataclass
class SeenBoot:
    """One QEMU run: its own argv, the runner's keyword arguments, the ``--name`` of the
    container it ran in, and the words before QEMU's (the ``podman run`` options)."""

    argv: list[str]
    kwargs: dict[str, Any]
    container: str | None
    outer: list[str] = field(default_factory=list)


@dataclass
class FakeCiToolbox:
    on_boot: Callable[[list[str]], Boot] = field(default=lambda _argv: Boot())
    alive: set[str] = field(default_factory=set)
    fail_tool: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    boots: list[SeenBoot] = field(default_factory=list)
    seeds: list[dict[str, str]] = field(default_factory=list)
    running: str | None = None

    # --- the runner --------------------------------------------------------------------

    def __call__(self, argv: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        del args
        words = [str(a) for a in argv]
        self.calls.append(words)
        tool_at = next(
            (i for i, w in enumerate(words) if Path(w).name in _TOOLS),
            None,
        )
        if tool_at is not None:
            container = _option(words[:tool_at], "--name")
            inner = words[tool_at:]
            tool = Path(inner[0]).name
            if tool in self.fail_tool:
                return self._done(words, kwargs, self.fail_tool[tool], err=f"{tool}: failed\n")
            if tool == "xorriso":
                return self._xorriso(words, inner, kwargs)
            if tool == "qemu-img":
                return self._qemu_img(words, inner, kwargs)
            return self._qemu(words, inner, kwargs, container)
        if words[:1] == ["podman"]:
            return self._podman(words, kwargs)
        return self._done(words, kwargs, 0)

    # --- imitations ----------------------------------------------------------------------

    def _xorriso(self, words: list[str], inner: list[str], kwargs: dict[str, Any]) -> Any:
        out = _option(inner, "-o")
        cwd = Path(kwargs.get("cwd") or _option(words, "-w") or ".")
        seen: dict[str, str] = {"label": _option(inner, "-V") or ""}
        for word in inner[1:]:
            for part in word.split("="):
                path = Path(part) if Path(part).is_absolute() else cwd / part
                if path.name in ("user-data", "meta-data") and path.is_file():
                    seen[path.name] = path.read_text(encoding="utf-8")
        self.seeds.append(seen)
        if out:
            target = Path(out) if Path(out).is_absolute() else cwd / out
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"CD001 fake seed")
        return self._done(words, kwargs, 0)

    def _qemu_img(self, words: list[str], inner: list[str], kwargs: dict[str, Any]) -> Any:
        verb = inner[1] if len(inner) > 1 else ""
        positionals = _positionals(inner[2:], with_value={"-f", "-F", "-b", "-o", "-O", "-B"})
        if verb == "create" and positionals:
            _touch(Path(positionals[0]), b"QFI\xfb fake overlay")
        elif verb == "convert" and positionals:
            _touch(Path(positionals[-1]), b"QFI\xfb fake converted")
        elif verb == "info":
            return self._done(words, kwargs, 0, out=json.dumps({"format": "qcow2"}))
        return self._done(words, kwargs, 0)

    def _qemu(
        self, words: list[str], inner: list[str], kwargs: dict[str, Any], container: str | None
    ) -> Any:
        outer = words[: len(words) - len(inner)]
        self.boots.append(SeenBoot(list(inner), dict(kwargs), container, outer))
        previous, self.running = self.running, container
        try:
            boot = self.on_boot(list(inner))
        finally:
            self.running = previous
        serial = _option(inner, "-serial")
        if serial and serial.startswith("file:") and boot.serial:
            _touch(Path(serial.removeprefix("file:")), boot.serial.encode("utf-8"))
        if boot.hang:
            if "timeout" in kwargs and kwargs["timeout"] is not None:
                raise subprocess.TimeoutExpired(words, kwargs["timeout"])
            raise AssertionError("the guest VM was started without a timeout: it would hang here")
        return self._done(words, kwargs, boot.rc)

    def _podman(self, words: list[str], kwargs: dict[str, Any]) -> Any:
        alive = set(self.alive) | ({self.running} if self.running else set())
        verb = words[1:3]
        if verb == ["container", "exists"] or words[1:2] == ["exists"]:
            return self._done(words, kwargs, 0 if words[-1] in alive else 1)
        if words[1] == "inspect" or verb == ["container", "inspect"]:
            name = words[-1]
            if name not in alive:
                return self._done(words, kwargs, 125, err=f"no such container {name}\n")
            return self._done(words, kwargs, 0, out="true\n")
        if words[1] == "ps":
            return self._done(words, kwargs, 0, out="".join(f"{n}\n" for n in sorted(alive)))
        return self._done(words, kwargs, 0)

    # --- helpers -------------------------------------------------------------------------

    @staticmethod
    def _done(
        words: list[str], kwargs: dict[str, Any], rc: int, *, out: str = "", err: str = ""
    ) -> Any:
        text = bool(
            kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
        )
        stdout: Any = out if text else out.encode()
        stderr: Any = err if text else err.encode()
        if kwargs.get("check") and rc != 0:
            raise subprocess.CalledProcessError(rc, words, stdout, stderr)
        return subprocess.CompletedProcess(words, rc, stdout, stderr)

    def tools_run(self) -> list[str]:
        """The toolbox binaries run, in order: ``xorriso``, ``qemu-img``, ``qemu-system-x86_64``."""
        return [
            next(Path(w).name for w in call if Path(w).name in _TOOLS)
            for call in self.calls
            if any(Path(w).name in _TOOLS for w in call)
        ]

    def everything(self) -> str:
        return "\n".join(" ".join(c) for c in self.calls)


def _option(words: Sequence[str], name: str) -> str | None:
    for i, word in enumerate(words):
        if word == name and i + 1 < len(words):
            return words[i + 1]
        if word.startswith(name + "="):
            return word.split("=", 1)[1]
    return None


def _positionals(words: Sequence[str], *, with_value: set[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word in with_value:
            skip = True
            continue
        if word.startswith("-"):
            continue
        out.append(word)
    return out


def _touch(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def option_values(argv: Sequence[str], name: str) -> list[str]:
    """Every value of ``name`` in ``argv`` (``name V`` and ``name=V``)."""
    values = []
    for i, word in enumerate(argv):
        if word == name and i + 1 < len(argv):
            values.append(argv[i + 1])
        elif word.startswith(name + "="):
            values.append(word.split("=", 1)[1])
    return values


def stand_in_toolbox_host(monkeypatch: Any, base: Path) -> None:
    """The toolbox's host prerequisites, stood in under ``base``: no preflight (podman and
    ``/dev/kvm`` are not on every test host), a runner tarball file, a
    seccomp profile and the OVMF firmware. The commands themselves still go through the
    ``runner`` the test hands in."""
    from shidashi import vm

    base.mkdir(parents=True, exist_ok=True)
    tarball = base / "v3-systemd-stand-in-toolbox.tar"
    tarball.write_bytes(b"toolbox stand-in")
    profile = base / "runner-seccomp.json"
    profile.write_text("{}", encoding="utf-8")
    code, vars_ = base / "OVMF_CODE.fd", base / "OVMF_VARS.fd"
    code.write_bytes(b"\0" * 64)
    vars_.write_bytes(b"\0" * 64)
    monkeypatch.setattr(vmrunner, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(vmrunner, "runner_tarball", lambda *a, **k: tarball)
    monkeypatch.setattr(vmrunner, "seccomp_profile", lambda *a, **k: profile)
    monkeypatch.setattr(vm, "ovmf", lambda: (str(code), str(vars_), "raw"))


def missing_toolbox(monkeypatch: Any) -> None:
    """``vmrunner.preflight`` as on a worker that never got the toolbox: the REAL preflight,
    describing a node with podman and ``/dev/kvm`` but no QEMU and no runner tarball -- its
    message names ``shidashi worker sync push <worker> --arch v3``."""

    def preflight(*_a: Any, **_k: Any) -> None:
        _REAL_PREFLIGHT(
            which=lambda name: "/usr/bin/podman" if name == "podman" else None,
            exists=lambda path: path in ("/dev/kvm", "/dev/vhost-vsock"),
            euid=lambda: 0,
        )

    monkeypatch.setattr(vmrunner, "preflight", preflight)


def no_download(monkeypatch: Any) -> None:
    """Any URL opened from here on fails the test: nothing may be downloaded."""
    import urllib.request

    def refuse(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        full = getattr(url, "full_url", url)
        raise AssertionError(f"unexpected download of {full}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
