"""``shidashi binhost``: look at, trim and publish a generation's build pool (story 029).

``cli.py`` only registers :data:`app`. Everything here reaches the CLI's own helpers and
the toolbox through module attributes looked up at call time, so tests can stand in for the
PKGDIR resolution and for the host's containers.
"""

import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from shidashi import binhost, binhost_quarantine, config, ownership, publish_pool, toolbox
from shidashi.binhost import BinhostError, BinpkgRef, Kind
from shidashi.config import UnknownAxisError
from shidashi.recipe import TOOLBOX_STAGE
from shidashi.seed import SeedError, load_pointer
from shidashi.toolbox import ToolboxError
from shidashi.tree import load_pin_id, pinned_repos

app = typer.Typer(no_args_is_help=True, help="The build pool: classify, trim and publish binpkgs.")

_ARCH = Annotated[str, typer.Argument(help="Microarchitecture whose pool to read (e.g. v3).")]
_INIT = Annotated[str, typer.Option("--init", help="Init flavor whose PKGDIR and toolbox to use.")]
_GENERATION = Annotated[
    str | None,
    typer.Option(
        "--generation",
        help="Generation (stage3 snapshot) to read; default is the one factory builds.",
    ),
]


def _die(message: object) -> NoReturn:
    """Print ``message`` on stderr and exit 1 (R2.8): every failure of this sub-app."""
    typer.echo(str(message), err=True)
    raise typer.Exit(code=1)


def _pkgdir_of(arch: str, init: str, generation: str | None) -> Path:
    """The PKGDIR of ``arch``: ``generation``'s, else the one the factory writes to."""
    if generation is not None:
        return config.pkgdir(arch, generation)
    # looked up at call time: cli registers this module, and the tests patch the helper
    from shidashi import cli

    return cli._generation_pkgdir(arch, init)


def _toolbox_for(
    arch: str,
    init: str,
    stack: ExitStack,
    *,
    rw: Mapping[str, Path],
    file_binds: Sequence[tuple[Path, Path]] = (),
) -> toolbox.Toolbox:
    """The toolbox of ``arch`` × ``init`` with ``rw`` mounted read-write, as the assembler's.

    The tarball is keyed by the current pins, so a toolbox of older pins is not it (D7).
    The rootfs's shared lock (``toolbox.using``) is entered on ``stack``, so it outlives this
    function and a concurrent toolbox rebuild waits until the caller closes the stack (R3.13).
    """
    recipe = config.load_recipe(arch, TOOLBOX_STAGE, init)
    seeds = config.seeds_dir()
    tarball = toolbox.tarball_path(
        recipe,
        snapshot=load_pointer(init, seeds_dir=seeds).snapshot,
        pins=load_pin_id(seeds),
        fork_points_dir=config.fork_points_dir(),
    )
    if not tarball.is_file():
        raise BinhostError(
            f"no toolbox for {arch}/{init} at {tarball}; build it first: "
            f"shidashi factory {arch} {TOOLBOX_STAGE} {init}"
        )
    root = config.scratch_dir() / "toolbox" / f"{arch}-{init}"
    try:
        stack.enter_context(toolbox.using(tarball, root))
    except (OSError, ToolboxError) as err:
        raise BinhostError(f"cannot use the toolbox {root} ({tarball.name}): {err}") from err
    return toolbox.Toolbox(root, rw=dict(rw), file_binds=list(file_binds))


def _read_pool_refs(pkgdir: Path, arch: str) -> list[BinpkgRef]:
    """The instances of ``pkgdir``'s ``Packages`` index; absent names the PKGDIR and factory."""
    index = pkgdir / "Packages"
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise BinhostError(
            f"no binhost index {index}; nothing is built in {pkgdir} yet: run "
            f"`shidashi factory {arch} <target>` first"
        ) from None
    except OSError as err:
        raise BinhostError(f"cannot read the binhost index {index}: {err}") from err
    return binhost.parse_refs(text)


def _pinned_availability(arch: str, init: str) -> Callable[[str], bool]:
    """Is a CPV an ebuild in the pinned ``::gentoo`` or an overlay of the current pins?

    The current pins also for an older generation: the question is what a build made now
    could still install, not what that generation was built from.
    """
    try:
        repos = pinned_repos(
            seeds_dir=config.seeds_dir(), cache_dir=config.cache_dir(), download=False
        )
    except SeedError as err:
        raise BinhostError(
            f"{err}; `shidashi factory {arch} <target> {init}` extracts the pinned trees"
        ) from err
    return binhost.ebuild_available(list(repos.values()))


@app.command()
def show(arch: _ARCH, init: _INIT = "systemd", generation: _GENERATION = None) -> None:
    """Print each binpkg instance with its kind (final, transient, old, unrecorded)."""
    try:
        pkgdir = _pkgdir_of(arch, init, generation)
        refs = _read_pool_refs(pkgdir, arch)
        record = binhost.load_record(pkgdir)
        kinds = binhost.classify(refs, record, available=_pinned_availability(arch, init))
    except (BinhostError, SeedError, UnknownAxisError) as err:
        _die(err)
    for ref in sorted(kinds, key=lambda r: (r.path, r.build_id)):
        typer.echo(f"{kinds[ref].value} {ref.path}")
    count = Counter(kinds.values())
    typer.echo(" · ".join(f"{kind.value} {count[kind]}" for kind in Kind))


#: The pool's mount name in the toolbox: ``/work/pool`` holds the PKGDIR being reindexed.
_POOL_MOUNT = "pool"
#: How much of a failed reindex's output is shown: the end is where Portage says why.
_OUTPUT_TAIL_LINES = 20


def _refuse_if_busy(arch: str) -> None:
    """Exit 1, naming the holder, while another owner holds ``arch`` or a pull is running."""
    try:
        holder = ownership.current(arch)
        puller = ownership.pulling_pid(arch)
    except ownership.LockError as err:
        _die(err)
    if holder is not None:
        _die(ownership.OwnedElsewhere(holder))
    if puller is not None:
        _die(ownership.PullRunning(arch, puller))


def _reindex(tools: toolbox.Toolbox, pkgdir: Path) -> str | None:
    """``emaint binhost --fix`` over ``pkgdir`` in the toolbox; None, or why it failed.

    PKGDIR rides in the argv: nspawn passes no host environment into the container.
    """
    argv = ["env", f"PKGDIR={tools.path(pkgdir)}", "emaint", "binhost", "--fix"]
    try:
        tools.container.run(argv)
    except subprocess.CalledProcessError as err:
        tail = "\n".join(f"{err.output or ''}{err.stderr or ''}".splitlines()[-_OUTPUT_TAIL_LINES:])
        return f"emaint binhost --fix failed (exit {err.returncode}) over {pkgdir}:\n{tail}"
    return None


@app.command()
def gc(
    arch: _ARCH,
    init: _INIT = "systemd",
    keep: Annotated[
        int,
        typer.Option("--keep", min=0, help="Newest CPVs kept per (package, SLOT) once gone."),
    ] = 2,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the planned PATHs; move and write nothing.")
    ] = False,
) -> None:
    """Quarantine the binpkgs whose ebuild is gone, beyond --keep per package, and reindex."""
    from shidashi import cli  # looked up at call time: the tests patch the owner and PKGDIR

    # before any local work: a worker-owned arch has no pool on this host (R3.10)
    if (owner := cli._cache_owner(arch)) is not None:
        _gc_on_worker(cli, owner, arch, init=init, keep=keep, dry_run=dry_run)
        return
    _refuse_if_busy(arch)
    try:
        with ownership.held(arch, cli._host_owner(arch, "binhost-gc")):
            _collect(arch, init, keep=keep, dry_run=dry_run)
    except (ownership.OwnedElsewhere, ownership.PullRunning, ownership.LockError) as err:
        _die(err)


def _gc_on_worker(cli: Any, owner: str, arch: str, *, init: str, keep: int, dry_run: bool) -> None:
    """Run this gc as a job on ``owner``; ``worker job`` exits with the job's code.

    ``worker job`` already turns a job that cannot start into exit 1 with the worker's
    error. No owner lock is taken here (``ownership.writes_pkgdir`` is false for
    ``binhost``): the job's own gc takes the worker's.
    """
    argv = ["binhost", "gc", arch, "--init", init, "--keep", str(keep)]
    if dry_run:
        argv.append("--dry-run")
    cli.worker_job(
        owner,
        f"binhost-gc-{arch}",
        argv,
        allow_dirty=False,
        no_follow=False,
        results=None,
        bwlimit=None,
        address=None,
    )


def _collect(arch: str, init: str, *, keep: int, dry_run: bool) -> None:
    """The gc proper, under the arch's lock: plan, check, move, record, reindex.

    The toolbox rootfs's shared lock is held from before the first move until the reindex
    is done, and released on every way out (R3.13).
    """
    with ExitStack() as stack:
        try:
            pkgdir = _pkgdir_of(arch, init, None)
            plan = binhost.gc_plan(
                _read_pool_refs(pkgdir, arch),
                available=_pinned_availability(arch, init),
                keep=keep,
            )
            paths = [ref.path for ref in plan]
            for rel in paths:  # the whole plan before the first move (R3.11)
                binhost.safe_path(pkgdir, rel)
            binhost.load_quarantined(pkgdir)  # a damaged list stops the gc here, not mid-way
            if dry_run:
                for rel in paths:
                    typer.echo(rel)
                typer.echo(f"{len(paths)} binpkgs would move to quarantine")
                return
            # before the first move: an index naming moved files fails the next emerge (R3.12)
            tools = _toolbox_for(arch, init, stack, rw={_POOL_MOUNT: pkgdir})
            root = binhost_quarantine.quarantine_dir(arch, pkgdir)
            outcome = binhost_quarantine.quarantine_paths(pkgdir, root, paths)
            binhost.add_quarantined(pkgdir, outcome.moved)
        except (BinhostError, SeedError, UnknownAxisError) as err:
            _die(err)
        # a failed move still reindexes: what did move must leave the index
        failures = [outcome.failure] if outcome.failure else []
        if (reindex_failure := _reindex(tools, pkgdir)) is not None:
            failures.append(reindex_failure)
    typer.echo(f"moved {len(outcome.moved)} binpkgs to {root}")
    if failures:
        _die("\n".join(failures))


def _default_publish_dest(arch: str, pkgdir: Path) -> Path:
    """Where a generation's signed pool lives unless ``--dest`` says otherwise."""
    return config.cache_dir() / "publish" / arch / pkgdir.name


@app.command("publish")
def publish_command(
    arch: _ARCH,
    key: Annotated[
        str, typer.Option("--key", help="Fingerprint of the maintainer's key that signs.")
    ],
    init: _INIT = "systemd",
    generation: _GENERATION = None,
    dest: Annotated[
        Path | None,
        typer.Option(
            "--dest",
            help="Where the signed pool is made; default <cache>/publish/<arch>/<generation>.",
        ),
    ] = None,
) -> None:
    """Build the signed publish pool of one generation: its recorded finals, and no more."""
    from shidashi import cli  # looked up at call time: the tests patch the owner and PKGDIR

    # the pool and the maintainer's agent are on this host; a worker never gets the key (R5.10)
    if (owner := cli._cache_owner(arch)) is not None:
        _die(
            f"{arch}'s binpkgs live on worker {owner}, not here: release them to publish from "
            f"this host with `shidashi worker cache-owner {owner} --arch {arch} --off`"
        )
    # one hold across the signing and the reindex; closed on every exit, a refusal included
    with ExitStack() as stack:
        try:
            pkgdir = _pkgdir_of(arch, init, generation)
            target = dest or _default_publish_dest(arch, pkgdir)

            def toolbox_for(
                rw: Mapping[str, Path], file_binds: Sequence[tuple[Path, Path]]
            ) -> toolbox.Toolbox:
                return _toolbox_for(arch, init, stack, rw=rw, file_binds=file_binds)

            result = publish_pool.publish(pkgdir, target, key=key, toolbox_for=toolbox_for)
        except (BinhostError, SeedError, UnknownAxisError) as err:
            _die(err)
    typer.echo(f"published {result.published} binpkgs to {result.dest}")
    typer.echo(f"{result.skipped_unrecorded} unrecorded binpkgs left out")


@app.command("verify")
def verify_command(
    arch: _ARCH,
    pubkey: Annotated[
        Path, typer.Option("--pubkey", help="Armored public key file: the only key trusted.")
    ],
    init: _INIT = "systemd",
    generation: _GENERATION = None,
    dest: Annotated[
        Path | None,
        typer.Option(
            "--dest",
            help="The signed pool to check; default <cache>/publish/<arch>/<generation>.",
        ),
    ] = None,
) -> None:
    """Check every signature of the publish pool against --pubkey; exit 1 on any failure."""
    try:
        target = dest or _default_publish_dest(arch, _pkgdir_of(arch, init, generation))
        failures = publish_pool.verify(target, pubkey=pubkey)
    except (BinhostError, SeedError, UnknownAxisError) as err:
        _die(err)
    if failures:
        _die("\n".join(failures))
    typer.echo(f"verified {target}: every gpkg is signed by {pubkey}")
