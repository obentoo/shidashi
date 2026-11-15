"""What the three soname-waiver path tests share: the case, and a ``variants/`` to read.

The case is the weekly release 2026.11.13's: ``gentoo-kernel-bin-7.3.3`` needs
``libcrypto.so.3`` (its prebuilt ``certs/extract-cert``) while the plan's OpenSSL 4
offers ``libcrypto.so.4``.

The waivers are read from ``variants/`` at the boundary, so each test gets a COPY of
the shipped tree with its own ``soname-waivers.yaml`` (or none) -- never the shipped
file, whose entries come and go.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

from shidashi import audit
from shidashi.soname_waivers import SONAME_WAIVERS_FILE
from tests.test_phases_soname import _entry

_SHIPPED_VARIANTS = Path(__file__).resolve().parent.parent / "variants"

KERNEL_PACKAGE = "sys-kernel/gentoo-kernel-bin"
KERNEL_CPV = f"{KERNEL_PACKAGE}-7.3.3"
UNTIL = "Gentoo ships a gentoo-kernel-bin whose extract-cert links libcrypto.so.4"
WHY = "certs/extract-cert is prebuilt against OpenSSL 3"


def waiver_yaml(package: str = KERNEL_PACKAGE, soname: str = "libcrypto.so.3") -> str:
    return (
        f"- package: {package}\n  soname: {soname}\n  why: {WHY}\n"
        f"  found: weekly release 2026.11.13\n  until: {UNTIL}\n"
    )


def variants_with_waivers(tmp: Path, monkeypatch: pytest.MonkeyPatch, text: str | None) -> Path:
    """Point ``SHIDASHI_VARIANTS_DIR`` at a copy of the shipped tree whose
    ``soname-waivers.yaml`` is ``text``; ``None`` leaves the copy without the file."""
    root = tmp / "variants"
    shutil.copytree(_SHIPPED_VARIANTS, root)
    (root / SONAME_WAIVERS_FILE).unlink(missing_ok=True)
    if text is not None:
        (root / SONAME_WAIVERS_FILE).write_text(text, encoding="utf-8")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


class InMemoryAuditTrail(audit.Recorder):
    """Stands in for the run in force: keeps the events, nothing else."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def waiver_events(self) -> list[dict[str, Any]]:
        return [fields for kind, fields in self.events if kind == "binpkgs.soname_waivers"]


# --- the index entries ----------------------------------------------------------------------

#: the binpkg of the release: one REQUIRES line for the whole package, extract-cert's
#: ``libcrypto.so.3`` in it
KERNEL = _entry(KERNEL_CPV, 1, slot="7.3.3", requires="x86_64: libc.so.6 libcrypto.so.3")
#: what a fixed gentoo-kernel-bin would record (the exit condition)
KERNEL_FIXED = _entry(KERNEL_CPV, 2, slot="7.3.3", requires="x86_64: libc.so.6 libcrypto.so.4")
#: the waived soname AND one no waiver covers (objtool's libelf)
KERNEL_MIXED = _entry(
    KERNEL_CPV, 1, slot="7.3.3", requires="x86_64: libc.so.6 libcrypto.so.3 libelf.so.1"
)
#: another build of the same version, stale for a reason no waiver covers
KERNEL_SIBLING = _entry(KERNEL_CPV, 3, slot="7.3.3", requires="x86_64: libc.so.6 libelf.so.1")
OPENSSL = _entry(
    "dev-libs/openssl-4.0.1", 1, slot="0/4", provides="x86_64: libcrypto.so.4 libssl.so.4"
)
ELFUTILS = _entry("dev-libs/elfutils-0.199", 1, provides="x86_64: libelf.so.2")
#: another package still on OpenSSL 3: the waiver is not its
CURL_OPENSSL3 = _entry("net-misc/curl-8.16.0", 1, requires="x86_64: libc.so.6 libcrypto.so.3")

#: plan lines as ``emerge --verbose --pretend`` prints them
P_KERNEL = f"[binary   N    ] {KERNEL_CPV}-1:7.3.3::gentoo  0 KiB"
P_OPENSSL = "[binary   N    ] dev-libs/openssl-4.0.1-1:0/4::gentoo  0 KiB"
P_ELFUTILS = "[binary   N    ] dev-libs/elfutils-0.199-1::gentoo  0 KiB"
P_CURL = "[binary  N ] net-misc/curl-8.16.0-1::gentoo  0 KiB"

#: the refusal lines (binpkgs.describe)
KERNEL_LINE = f"{KERNEL_CPV} (build 1): needs libcrypto.so.3 — offered libcrypto.so.4"
LIBELF_LINE = f"{KERNEL_CPV} (build 1): needs libelf.so.1 — offered libelf.so.2"
CURL_LINE = "net-misc/curl-8.16.0 (build 1): needs libcrypto.so.3 — offered libcrypto.so.4"

#: what a judgement records of the waived finding
WAIVED = {
    "cpv": KERNEL_CPV,
    "build_id": 1,
    "needs": "libcrypto.so.3",
    "offered": ["libcrypto.so.4"],
    "why": WHY,
    "until": UNTIL,
}
UNUSED = {"package": KERNEL_PACKAGE, "soname": "libcrypto.so.3", "until": UNTIL}
