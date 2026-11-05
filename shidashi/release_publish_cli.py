"""``shidashi release sign``: sign a release made with ``--unsigned`` (story 033, R1);
``shidashi release upload``: publish a signed one to the bucket (R2-R5).

Registered by ``release_cli``, which imports this module to do so; ``release_cli`` and
``cli`` are therefore imported here at call time, never at module level (design.md
GOTCHA: the import would be circular).
"""

import json
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from shidashi import audit, config, gpgsign, publish, release_sign
from shidashi import release as release_core
from shidashi import release_upload as up
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


def _open_trail(
    stack: ExitStack, directory: Path, inputs: Mapping[str, str], command: str = "release-sign"
) -> audit.Recorder:
    """The ``command`` audit run, opened as ``shidashi release`` opens its own; a runs
    directory that cannot be written disables the trail with a warning, never the signing."""
    from shidashi import cli  # call time: cli imports release_cli, which imports this module

    try:
        trail: audit.Recorder = stack.enter_context(
            audit.run(
                config.runs_dir(),
                command=command,
                argv=sys.argv,
                inputs={**inputs, "release_dir": str(directory)},
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
            trail = _open_trail(stack, directory, {"key": fingerprint})
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


# --- release upload (R2-R5) -------------------------------------------------------------------

#: Where story 032 sets a pruned release aside: still a host copy (R5.3).
_PRUNED_DIR = ".pruned"
LATEST_OBJECT = "releases/latest.json"


def _signed_manifest(directory: Path) -> dict[str, object]:
    path = directory / release_core.MANIFEST_FILE
    try:
        body = json.loads(path.read_bytes())
    except (OSError, ValueError) as err:
        raise up.UploadError(f"{path}: cannot read the release manifest: {err}") from err
    if not isinstance(body, dict) or body.get("signed") is not True:
        raise up.UploadError(
            f'{path} does not say "signed": true: sign it with `shidashi release sign` first'
        )
    return body


def _entry_for(
    directory: Path, manifest: Mapping[str, object], sums: Mapping[str, str]
) -> dict[str, Any]:
    """The index entry of this release (R4.1): its date is the directory name, the bucket
    key -- ``release.json``'s ``date`` is ISO -- and its ISOs are those ``release.json``
    names, which must be the ``*.iso`` that ``SHA256SUMS`` lists."""
    date = directory.name
    if not up.RELEASE_DATE_RE.fullmatch(date):
        raise up.UploadError(f"{directory}: a release directory is named YYYY.MM.DD")
    variants = manifest.get("variants")
    named = [
        iso
        for v in (variants if isinstance(variants, list) else [])
        if isinstance(v, dict)
        for iso in (v.get("isos") or [])
    ]
    if not all(isinstance(iso, str) for iso in named):
        raise up.UploadError(f"{release_core.MANIFEST_FILE}: an ISO name is not a string")
    listed = sorted(n for n in sums if n.endswith(".iso"))
    if sorted(named) != listed:
        raise up.UploadError(
            f"{release_core.MANIFEST_FILE} names the ISOs {sorted(named)} but "
            f"{release_sign.SUMS_FILE} lists {listed}"
        )
    base = f"releases/{date}"
    return {
        "date": date,
        "isos": listed,
        "sha256sums": f"{base}/{release_sign.SUMS_FILE}",
        "signature": f"{base}/{up.SUMS_SIGNATURE_FILE}",
        "pubkey": f"{base}/{release_sign.RELEASE_KEY_FILE}",
    }


def _preflight(directory: Path) -> tuple[Path, str, up.UploadPlan, dict[str, Any]]:
    """Every check before the first transfer (R2): rclone and its config, ``signed: true``,
    every signature verifying against the key the directory carries, the upload set."""
    up.rclone_binary()
    rclone_conf = up.rclone_config()
    manifest = _signed_manifest(directory)
    key = gpgsign.public_key_fingerprint(directory / release_sign.RELEASE_KEY_FILE)
    failures = release_sign.verify_release(directory, key=key)
    if failures:
        raise up.UploadError("; ".join(failures))
    sums = publish._read_sums(directory / release_sign.SUMS_FILE)
    present = [p.name for p in directory.iterdir() if p.is_file()]
    plan = up.upload_set(sums, present)
    # The signature vouches for SHA256SUMS, not for the bytes beside it: a file changed
    # after signing would be published under a good signature (tech review 4.x).
    for name in plan.data + plan.seal:
        if name in sums and publish.sha256_of(directory / name) != sums[name]:
            raise up.UploadError(f"{directory / name} does not match its SHA256SUMS line")
    return rclone_conf, key, plan, _entry_for(directory, manifest, sums)


def _checked_remote(remote: str) -> str:
    """``remote`` when it names a configured remote (``name:bucket``). rclone also takes
    ``:s3,secret_access_key=...:bucket``, which would skip the checked config and put the
    secret in argv and the audit trail (R2.2, R3.5)."""
    name, colon, bucket = remote.partition(":")
    if not colon or not name or "," in name or not bucket.strip("/"):
        # the value is not echoed: a refused remote may carry a secret (R3.5)
        raise up.UploadError("--remote must be a remote from the rclone config, as NAME:BUCKET")
    return remote


def _host_dates(root: Path) -> set[str]:
    """The release dates the host still holds, under ``root`` or ``root/.pruned`` (R5.3);
    a look-alike name is no copy, a missing directory no crash."""
    dates: set[str] = set()
    for folder in (root, root / _PRUNED_DIR):
        try:
            names = [p.name for p in folder.iterdir() if p.is_dir()]
        except OSError:
            continue
        dates.update(n for n in names if up.RELEASE_DATE_RE.fullmatch(n))
    return dates


def _put_public(runner: up.RcloneRunner, scratch: Path, obj: str, body: Mapping[str, Any]) -> None:
    """``body`` as ``obj``, through a temporary object then ``moveto`` (R4.5): a reader
    sees the old file or the new one, never half of one."""
    name = obj.rsplit("/", 1)[-1]
    local = scratch / name
    local.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    local.chmod(0o644)  # public content; under sudo rclone runs as the maintainer
    temporary = f"releases/.{name}.tmp"
    runner.copyto(local, temporary)
    runner.moveto(temporary, obj)


def _publish_index(runner: up.RcloneRunner, index: Mapping[str, Any]) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="shidashi-index-"))
    try:
        scratch.chmod(0o755)
        _put_public(runner, scratch, up.INDEX_OBJECT, index)
        _put_public(runner, scratch, LATEST_OBJECT, up.latest(index))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _dry_run_lines(
    plan: up.UploadPlan, merged: Mapping[str, Any], kept: up.RetentionPlan
) -> list[str]:
    lines = ["upload set (data, then seal):", *(f"  {n}" for n in (*plan.data, *plan.seal))]
    lines += ["index.json:", json.dumps(merged, indent=2)]
    lines += [f"purge: releases/{e['date']}" for e in kept.drop]
    lines += [f"kept, no host copy (--yes deletes it): {e['date']}" for e in kept.refused]
    return [*lines, "dry run: nothing copied, moved or purged"]


def upload(
    directory: Annotated[Path, typer.Argument(help="A release directory signed on this host.")],
    remote: Annotated[
        str, typer.Option("--remote", help="The rclone remote and bucket, e.g. r2:bentoo.")
    ],
    keep: Annotated[int, typer.Option("--keep", help="Releases the bucket keeps.")] = 4,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; copy, move and purge nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Also delete a release the host holds no copy of.")
    ] = False,
    output_root: Annotated[
        Path | None,
        typer.Option("--output-root", help="Where the host keeps releases (default: DIR/..)."),
    ] = None,
) -> None:
    """Verify a signed release, copy its public files to the bucket (signatures last),
    rewrite the public index, then delete from the bucket the releases beyond --keep."""
    from shidashi import cli, release_cli  # call time: release_cli imports this module

    if keep < 1:
        release_cli._die(cli, f"--keep must be at least 1, not {keep}")
    directory = directory.absolute()
    try:
        remote = _checked_remote(remote)
        rclone_conf, key, plan, entry = _preflight(directory)
    except (up.UploadError, gpgsign.GpgError, publish.PublishError, OSError) as err:
        release_cli._die(cli, err)
    date = entry["date"]
    host = _host_dates(output_root if output_root is not None else directory.parent)
    try:
        with release_cli._stop_signals_stop_the_release(), ExitStack() as stack:
            inputs = {"key": key, "remote": remote}
            trail = _open_trail(stack, directory, inputs, command="release-upload")
            runner = up.RcloneRunner(remote=remote, config=rclone_conf, trail=trail)
            # read before sending GiBs: an index that cannot be merged stops the run here
            merged = up.merge_index(up.parse_index(runner.cat(up.INDEX_OBJECT)), entry)
            kept = up.retention(merged, keep, date, host, yes)
            if dry_run:
                for line in _dry_run_lines(plan, kept.index, kept):
                    typer.echo(line)
                return
            for name in (*plan.data, *plan.seal):  # SHA256SUMS.asc last (R3.2)
                runner.copyto(directory / name, f"releases/{date}/{name}")
            # read again: the transfer may have taken hours; no lock spans it (design.md)
            merged = up.merge_index(up.parse_index(runner.cat(up.INDEX_OBJECT)), entry)
            kept = up.retention(merged, keep, date, host, yes)
            _publish_index(runner, kept.index)  # before any purge (R5.1)
            for item in kept.refused:
                typer.echo(f"kept, no host copy (--yes deletes it): releases/{item['date']}")
            left: list[str] = []
            for item in kept.drop:  # each one tried: the index no longer names any of them
                try:
                    runner.purge(f"releases/{item['date']}")
                except up.UploadError as err:
                    left.append(str(err))
                    continue
                typer.echo(f"purged: releases/{item['date']}")
            if left:
                raise up.UploadError(
                    "; ".join(left) + " -- the index no longer names them: purge them by hand"
                )
    except up.UploadError as err:
        release_cli._die(cli, err)
    except KeyboardInterrupt:
        cli._err_console.print("[bold red]interrupted:[/bold red] the upload stopped")
        raise typer.Exit(_SIGN_INTERRUPTED_EXIT) from None
    for name in (*plan.data, *plan.seal):
        typer.echo(f"published: releases/{date}/{name}")
    typer.echo(f"index: {up.INDEX_OBJECT}, {LATEST_OBJECT}")
