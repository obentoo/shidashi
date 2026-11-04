"""``shidashi release sign``: sign a release made with ``--unsigned`` (story 033, R1).

Registered by ``release_cli``, which imports this module to do so; ``release_cli`` and
``cli`` are therefore imported here at call time, never at module level (design.md
GOTCHA: the import would be circular).
"""

import json
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from shidashi import audit, config, gpgsign, publish, release_sign
from shidashi import release as release_core
from shidashi.build_outcome import ReleaseError

#: 128 + SIGINT, as ``shidashi release`` exits on Ctrl-C.
_SIGN_INTERRUPTED_EXIT = 130
#: How a failed restore of ``signed: false`` begins: the one failure a rerun cannot fix.
_RESTORE_STEP = f"restoring {release_core.MANIFEST_FILE}"


class ReleaseSignError(ReleaseError):
    """A release directory ``release sign`` refuses; the message names the file."""


def signable_manifest(directory: Path) -> dict[str, object]:
    """``directory``'s ``release.json`` when its ``signed`` is exactly ``false`` (R1.3).

    Raises :class:`ReleaseSignError` naming the file when it is missing, unreadable, not
    JSON, not an object, or its ``signed`` is anything but ``false``. Reads only.
    """
    path = directory / release_core.MANIFEST_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError as err:
        raise ReleaseSignError(
            f"{path} is missing: only a release made with --unsigned can be signed"
        ) from err
    except OSError as err:
        raise ReleaseSignError(f"{path}: cannot read the release manifest: {err}") from err
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ReleaseSignError(f"{path}: the release manifest is not JSON: {err}") from err
    if not isinstance(body, dict):
        raise ReleaseSignError(f"{path}: the release manifest is not a JSON object")
    if "signed" not in body:
        # story 032 added the key: a release made before it was signed by `release --key`
        raise ReleaseSignError(
            f'{path} has no "signed" field: it predates --unsigned and was signed when made'
        )
    # `is False`, not falsiness: 0, null or "false" were written by something else
    if body["signed"] is not False:
        raise ReleaseSignError(
            f'{path} says "signed": {json.dumps(body["signed"])}, not false: '
            "only an unsigned release is signed here"
        )
    return body


def _refuse_before_claim(directory: Path) -> dict[str, object]:
    """The refusals ``release_sign.claim_release_dir`` would not name, or would make only
    after a write: it creates a missing directory and its lock file, and its "already
    signed" names the directory, not ``SHA256SUMS.asc`` (R1.2, R1.3). Reads only."""
    if not directory.is_dir():
        raise ReleaseSignError(
            f"{directory} is not a release directory: it does not exist, so it holds no "
            f"{release_core.MANIFEST_FILE}"
        )
    signature = directory / f"{release_sign.SUMS_FILE}.asc"
    if signature.is_symlink() or signature.exists():
        raise ReleaseSignError(
            f"{signature} exists: the release is already signed; a published release is "
            "never replaced"
        )
    return signable_manifest(directory)


def _open_trail(stack: ExitStack, directory: Path, fingerprint: str) -> audit.Recorder:
    """The ``release-sign`` audit run, opened as ``shidashi release`` opens its own; a runs
    directory that cannot be written disables the trail with a warning, never the signing."""
    from shidashi import cli  # call time: cli imports release_cli, which imports this module

    try:
        trail: audit.Recorder = stack.enter_context(
            audit.run(
                config.runs_dir(),
                command="release-sign",
                argv=sys.argv,
                inputs={"key": fingerprint, "release_dir": str(directory)},
                disk=directory,
            )
        )
    except OSError as err:
        cli._err_console.print(
            f"[yellow]warning:[/yellow] audit trail disabled: {escape(str(err))}"
        )
        trail = audit.current()
    return trail


def _signed_lines(directory: Path, fingerprint: str) -> list[str]:
    """What a signed release prints: the verified signer, then every ``*.asc`` it holds."""
    lines = [f"signed and verified by {fingerprint}"]
    for asc in sorted(directory.glob("*.asc")):
        what = "public key" if asc.name == release_sign.RELEASE_KEY_FILE else "signature"
        lines.append(f"{what}: {asc.name}")
    return [*lines, f"release: {directory}"]


def _print_sign_errors(lines: Sequence[str]) -> None:
    from shidashi import cli  # call time: cli imports release_cli, which imports this module

    for line in lines:
        cli._err_console.print(f"[bold red]error:[/bold red] {escape(line)}", soft_wrap=True)


def _restore_unsigned(directory: Path, manifest: Mapping[str, object]) -> list[str]:
    """Write ``manifest`` (``signed: false``) back, so a rerun of ``sign`` is not refused
    (R1.5). Empty when restored; otherwise the one line naming ``release.json``."""
    try:
        release_core.write_manifest(directory, manifest)
    except (ReleaseError, publish.PublishError) as err:
        return [
            f"{_RESTORE_STEP}: {err}; it may still say "
            '"signed": true -- set it to false by hand, then rerun'
        ]
    return []


def _sign_in_place(
    trail: audit.Recorder, directory: Path, manifest: dict[str, object], fingerprint: str
) -> list[str]:
    """Set ``signed`` true, then sign ``directory`` as ``shidashi release --key`` does.
    The failures, each naming its step; empty = signed and verified (R1.1). On a failure
    or a Ctrl-C ``release.json`` says ``signed: false`` again (R1.5); a restore that fails
    is one more failure, printed before the interrupt goes on."""
    try:
        # release.json first: the SHA256SUMS signed next must carry the final manifest's line
        try:
            release_core.write_manifest(directory, {**manifest, "signed": True})
        except (ReleaseError, publish.PublishError) as err:
            # SHA256SUMS may be unwritable after release.json was replaced: restore anyway
            failures = [f"rewriting {release_core.MANIFEST_FILE}: {err}"]
        else:
            done = release_sign.sign_and_verify(trail, directory, fingerprint)
            failures = [f"signing: {failure}" for failure in done]
    except KeyboardInterrupt:
        _print_sign_errors(_restore_unsigned(directory, manifest))
        raise
    if failures:
        failures += _restore_unsigned(directory, manifest)
    return failures


def sign(
    directory: Annotated[
        Path, typer.Argument(help="A release directory made with `shidashi release --unsigned`.")
    ],
    key: Annotated[
        str, typer.Option("--key", help="Fingerprint of the maintainer's key that signs it.")
    ],
) -> None:
    """Sign an unsigned release in place, as `shidashi release --key` would have, and
    verify every signature; exit 0 only once they all verified."""
    from shidashi import cli, release_cli  # call time: release_cli imports this module

    try:
        fingerprint = release_cli._signing_key(key)
    except ValueError as err:
        release_cli._die(cli, err)
    try:  # every refusal before the first write, the lock file included (R1.2-R1.4)
        gpgsign.check_key(fingerprint)
        _refuse_before_claim(directory)
    except (gpgsign.GpgError, ReleaseError) as err:
        release_cli._die(cli, err)
    try:
        with release_cli._stop_signals_stop_the_release(), ExitStack() as stack:
            try:  # held through the signing: a second sign or release is refused (R1.6)
                stack.enter_context(release_sign.claim_release_dir(directory))
                # again under the lock: another sign may have run between the two reads
                manifest = signable_manifest(directory)
            except ReleaseError as err:
                release_cli._die(cli, err)
            trail = _open_trail(stack, directory, fingerprint)
            failures = _sign_in_place(trail, directory, manifest, fingerprint)
    except KeyboardInterrupt:
        cli._err_console.print("[bold red]interrupted:[/bold red] the release is not signed")
        raise typer.Exit(_SIGN_INTERRUPTED_EXIT) from None
    _print_sign_errors(failures)
    if failures:
        if not failures[-1].startswith(_RESTORE_STEP):
            typer.echo(
                f"{directory} is unsigned again: rerun `shidashi release sign` once fixed",
                err=True,
            )
        raise typer.Exit(1)
    for line in _signed_lines(directory, fingerprint):
        typer.echo(line)
