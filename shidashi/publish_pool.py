"""The signed publish pool: the recorded finals, copied, signed and indexed (story 029).

The build pool (the generation's PKGDIR) is never signed or written: signing rewrites a
gpkg, which would change the SHA1/MD5 the assemble checkpoints hash. ``publish`` copies the
recorded finals to ``<dest>/<same PATH>``, signs each copy in the toolbox through the
maintainer's gpg-agent *extra* socket (the only socket that cannot export the key), gives
the copies a ``Packages`` of their own, and renames the finished tree over ``dest``.
``verify`` (in ``publish_verify``, re-exported here) is the reader's side: every signature of
a finished pool, against one public key.
"""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shidashi import gpgsign
from shidashi.binhost import BinhostError, BinpkgRef, PoolRecord, load_record, parse_refs, safe_path
from shidashi.publish_verify import verify
from shidashi.toolbox import WORK, Toolbox

#: Mount names inside the toolbox (``/work/<name>``): the pool being built, the signing home.
_POOL_MOUNT = "publish"
_GNUPG_MOUNT = "gnupg"
#: gpg looks for the agent at ``<homedir>/S.gpg-agent``; the extra socket is bound there.
_AGENT_SOCKET = "S.gpg-agent"
#: Portage's BINPKG_GPG_SIGNING_BASE_COMMAND (make.globals) minus its ``flock /run/lock/...``:
#: the toolbox is read-only and its /run/lock may not exist, and files are signed one at a
#: time here, so there is no concurrent gpg to serialise.
_SIGNING_COMMAND = "/usr/bin/gpg --sign --armor [PORTAGE_CONFIG]"
_OUTPUT_TAIL_LINES = 20


class ToolboxFactory(Protocol):
    """What the CLI hands ``publish``: a toolbox with ``rw`` mounted and ``file_binds`` bound."""

    def __call__(
        self, rw: Mapping[str, Path], file_binds: Sequence[tuple[Path, Path]]
    ) -> Toolbox: ...


@dataclass(frozen=True)
class PublishResult:
    dest: Path
    published: int
    #: instances in no stage's finals or transients (OLD included): publish sees no repos
    skipped_unrecorded: int


def plan_publish(refs: Sequence[BinpkgRef], record: PoolRecord) -> list[BinpkgRef]:
    """The refs that are a recorded final of some stage, and nothing else. Pure.

    Fail-closed: an unrecorded instance, or one only a USE cut built, is never published.
    Matched on ``(cpv, build_id)``: two builds of one CPV differ.
    """
    finals = frozenset().union(*record.finals.values())
    return [ref for ref in refs if (ref.cpv, ref.build_id) in finals]


# --- the maintainer's agent and keyring, on the host ----------------------------------------


# One copy of the rule, in ``gpgsign`` (story 030); the aliases keep the names 029's code
# and tests use.
_as_maintainer = gpgsign.as_maintainer
_host_gpg = gpgsign.host_gpg


def _agent_extra_socket() -> Path:
    out = _host_gpg(
        _as_maintainer(["gpgconf", "--list-dirs", "agent-extra-socket"]),
        "ask gpgconf for the agent extra socket",
    )
    where = out.decode("utf-8", "replace").strip()
    if not where or not Path(where).exists():
        raise BinhostError(
            f"the gpg-agent extra socket {where or '(none reported)'!r} does not exist: start "
            "the agent of the maintainer's session and enable its extra socket, then retry"
        )
    return Path(where)


def _public_key(key: str) -> bytes:
    exported = _host_gpg(
        _as_maintainer(["gpg", "--batch", "--export", "--armor", "--", key]),
        f"export the public key {key}",
    )
    if not exported.strip():  # gpg exits 0 and prints nothing for a key it does not have
        raise BinhostError(f"the keyring has no public key {key}: check the fingerprint")
    return exported


# --- the build ---------------------------------------------------------------------------


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-_OUTPUT_TAIL_LINES:])


def _run_in_toolbox(tools: Toolbox, argv: list[str], what: str) -> None:
    try:
        tools.container.run(argv)
    except subprocess.CalledProcessError as err:
        said = _tail(f"{err.output or ''}{err.stderr or ''}")
        raise BinhostError(f"{what} failed (exit {err.returncode}):\n{said}") from err
    except OSError as err:
        raise BinhostError(f"cannot run {what} in the toolbox: {err}") from err


def _copy_planned(build_pool: Path, tmp: Path, planned: Sequence[BinpkgRef]) -> None:
    for ref in planned:
        src, dst = safe_path(build_pool, ref.path), safe_path(tmp, ref.path)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # a copy: gpkg-sign rewrites the file it signs
        except OSError as err:
            raise BinhostError(f"cannot copy {src} to {dst}: {err}") from err


def _swap_in(tmp: Path, dest: Path) -> None:
    """Rename ``tmp`` over ``dest``: the old pool is set aside first and restored on failure."""
    old = dest.parent / f".{dest.name}.old"
    try:
        shutil.rmtree(old, ignore_errors=True)
        had_previous = dest.exists()
        if had_previous:
            os.rename(dest, old)
        try:
            os.rename(tmp, dest)
        except OSError:
            if had_previous:
                os.rename(old, dest)
            raise
    except OSError as err:
        raise BinhostError(f"cannot move the new pool {tmp} to {dest}: {err}") from err
    try:
        shutil.rmtree(old, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError as err:
        raise BinhostError(
            f"{dest} is the new pool, but {old} could not be removed: {err}"
        ) from err


def publish(
    build_pool: Path, dest: Path, *, key: str, toolbox_for: ToolboxFactory
) -> PublishResult:
    """Build the signed publish pool of ``build_pool`` at ``dest``. I/O, PRIVILEGED.

    Every refusal comes before the first copy. The tree is built in ``dest.parent/.<name>.tmp``
    and renamed into place only when every step succeeded; a failure leaves the previous
    ``dest`` as it was and the ``.tmp`` for inspection. ``build_pool`` is only read.
    """
    index = build_pool / "Packages"
    try:
        refs = parse_refs(index.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        raise BinhostError(
            f"no binhost index {index}; nothing is built in {build_pool} yet: run "
            "`shidashi factory <arch> <target>` first"
        ) from None
    except OSError as err:
        raise BinhostError(f"cannot read the binhost index {index}: {err}") from err
    record = load_record(build_pool)
    planned = plan_publish(refs, record)
    if not planned:
        raise BinhostError(
            f"no binpkg of {build_pool} is a recorded final: run `shidashi factory` to record "
            "the finals of each shipped stage, then publish"
        )
    tmp = dest.parent / f".{dest.name}.tmp"
    for ref in planned:  # the whole plan, before the first copy (R3.11)
        safe_path(build_pool, ref.path)
    socket_path = _agent_extra_socket()
    public_key = _public_key(key)
    kept = frozenset().union(*record.finals.values(), *record.transients.values())
    skipped = sum(1 for ref in refs if (ref.cpv, ref.build_id) not in kept)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if tmp.exists():  # a failed run's leftover
            shutil.rmtree(tmp)
        tmp.mkdir()
    except OSError as err:
        raise BinhostError(f"cannot prepare {tmp}: {err}") from err
    _copy_planned(build_pool, tmp, planned)

    gnupghome = Path(tempfile.mkdtemp(prefix="shidashi-gnupg-"))  # public key only, mode 0700
    try:
        _host_gpg(
            ["gpg", "--homedir", str(gnupghome), "--batch", "--import"],
            "import the public key into a throwaway GNUPGHOME",
            stdin=public_key,
        )
        home_inside = WORK / _GNUPG_MOUNT
        # ONE file: the socket's directory also holds the full-access standard socket
        tools = toolbox_for(
            rw={_POOL_MOUNT: tmp, _GNUPG_MOUNT: gnupghome},
            file_binds=[(socket_path, home_inside / _AGENT_SOCKET)],
        )
        for ref in planned:
            _run_in_toolbox(
                tools,
                [
                    "env",
                    f"BINPKG_GPG_SIGNING_KEY={key}",
                    f"BINPKG_GPG_SIGNING_GPG_HOME={home_inside}",
                    f"BINPKG_GPG_SIGNING_BASE_COMMAND={_SIGNING_COMMAND}",
                    "gpkg-sign",
                    str(tools.path(safe_path(tmp, ref.path))),
                ],
                f"gpkg-sign of {ref.path}",
            )
        _run_in_toolbox(
            tools,
            # Over signed gpkgs emaint verifies each signature, and in the read-only toolbox
            # gpg cannot create its random_seed: every binpkg is rejected and the index lists
            # none (found on bentoo-lab, 2026-10-20). ``verify`` checks the signatures.
            [
                "env",
                f"PKGDIR={tools.path(tmp)}",
                "FEATURES=binpkg-ignore-signature",
                "emaint",
                "binhost",
                "--fix",
            ],
            f"emaint binhost --fix over {tmp}",
        )
    finally:
        shutil.rmtree(gnupghome, ignore_errors=True)
    _swap_in(tmp, dest)
    return PublishResult(dest=dest, published=len(planned), skipped_unrecorded=skipped)


__all__ = ["PublishResult", "ToolboxFactory", "plan_publish", "publish", "verify"]
