"""Checkpoints of the assemble — btrfs snapshots of the image's rootfs between steps.

The install is 80% of an assemble (11 minutes for minimal, 44 for kde), and every
failure of 2026-10-01/02 came after it: without a checkpoint, a broken last
package or a ``verify-config`` error paid the whole install again. The rootfs is
a btrfs subvolume, and after each costly step it is frozen into a read-only
snapshot -- instant, and sharing every unchanged block with the rootfs:

* ``install`` -- after the install and its settle;
* ``packages`` -- after depclean and preserved-rebuild: the image's final package
  set, from which only seconds-long steps and the squashfs remain;
* ``install-partial`` -- a FAILED install, with Portage's own resume list inside
  it (``/var/cache/edb/mtimedb``), so that ``emerge --resume`` merges only what
  was left once the broken binpkg is fixed.

Each checkpoint carries the fingerprint of everything its state depends on (the
stage3, the pins, the layers, the sets, the binhost index, the command line),
chained from the previous one. A checkpoint is reused only when the fingerprint
matches, so a resumed image is the image a clean run makes from the same inputs
(the "clean rootfs per ISO" guarantee, F77). ``install-partial`` is the one
controlled exception: its fingerprint leaves the binhost out, because fixing the
binhost is exactly how such a failure is repaired.

Off a btrfs scratch, checkpoints are off and the assemble works as before. The
btrfs commands go through a :class:`Backend`, so the logic is tested with a
directory-copy fake, without root and without btrfs.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: Bumped when the meaning of a checkpoint changes: an older one is never reused.
FORMAT = 3

#: The checkpoints, in the order the assemble makes them; resume tries the
#: deepest first.
INSTALL = "install"
PACKAGES = "packages"
PARTIAL = "install-partial"


class CheckpointError(Exception):
    """A snapshot could not be made, restored or removed."""


def filesystem_type(path: Path) -> str:
    """The filesystem type under ``path`` (or its nearest existing parent). I/O."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    done = subprocess.run(
        ["stat", "-f", "-c", "%T", str(probe)], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def fingerprint(*parts: object) -> str:
    """A stable digest of ``parts`` (JSON with sorted keys). Pure."""
    text = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def tree_digest(roots: Iterable[Path], *, skip: frozenset[str] = frozenset()) -> str:
    """A digest of every file under ``roots``: relative path, mode and content. I/O.

    Roots count by position, not by name. Files whose name is in ``skip`` are left
    out; a missing root counts as empty.
    Symlinks are recorded by their target, not followed.
    """
    digest = hashlib.sha256()
    # by position, not name: a rendered configuration lives in a random temp dir
    for index, root in enumerate(roots):
        digest.update(f"root:{index}\n".encode())
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.name in skip or path.is_dir() and not path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(f"link:{rel}->{os.readlink(path)}\n".encode())
                continue
            digest.update(f"file:{rel}:{path.stat().st_mode & 0o7777:o}\n".encode())
            with path.open("rb") as handle:
                while block := handle.read(1 << 20):
                    digest.update(block)
    return digest.hexdigest()


def file_digest(path: Path) -> str | None:
    """The sha256 of ``path``, or None when it does not exist. I/O."""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


#: A ``[binary ...]`` line of ``emerge --verbose``: the token after the bracket is
#: the binpkg's identity, ``cat/pkg-version[-buildid][:slot]::repo``.
_BINARY_LINE = re.compile(r"^\[binary[^\]]*\]\s+(\S+)")


def plan_tokens(emerge_output: str) -> tuple[str, ...]:
    """The binpkgs an ``emerge --verbose`` chose, in its order. Pure.

    Only the identity token: the merge flags (``N``/``R``) and the USE marks
    (``*``, ``%``) describe the rootfs it ran in, not the package chosen.
    """
    found = (_BINARY_LINE.match(line.strip()) for line in emerge_output.splitlines())
    return tuple(m.group(1) for m in found if m is not None)


def token_cpv(token: str) -> str:
    """``cat/pkg-version`` of a plan token: no slot, repository or build id. Pure."""
    head = token.split(":", 1)[0]
    return re.sub(r"-\d+$", "", head)


def binhost_slice(index: Path, cpvs: Iterable[str]) -> str | None:
    """A digest of the binhost index entries of ``cpvs`` (every instance). I/O.

    The rest of the index stays out: a binpkg rebuilt for another image does
    not touch this one. A rebuilt instance of one of ``cpvs`` (same version,
    new build id or checksum) changes it. None when there is no index.
    """
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    wanted = set(cpvs)
    entries: list[str] = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.strip().splitlines() if ": " in line)
        if fields.get("CPV") in wanted:
            entries.append(
                "|".join(
                    fields.get(key, "")
                    for key in ("CPV", "BUILD_ID", "BUILD_TIME", "SHA1", "MD5", "USE")
                )
            )
    return fingerprint(sorted(entries))


def has_resume_list(rootfs: Path) -> bool:
    """Whether Portage left a resume list in ``rootfs`` (a merge was interrupted). I/O."""
    mtimedb = rootfs / "var" / "cache" / "edb" / "mtimedb"
    try:
        data = json.loads(mtimedb.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    resume = data.get("resume") if isinstance(data, dict) else None
    return bool(isinstance(resume, dict) and resume.get("mergelist"))


class Backend(Protocol):
    """The filesystem operations a store needs: btrfs in production."""

    def create(self, path: Path) -> None: ...
    def snapshot(self, source: Path, dest: Path, *, readonly: bool) -> None: ...
    def is_subvolume(self, path: Path) -> bool: ...
    def delete(self, path: Path) -> None: ...


def subvolume_create_argv(path: Path) -> list[str]:
    """``btrfs subvolume create``. Pure."""
    return ["btrfs", "-q", "subvolume", "create", str(path)]


def snapshot_argv(source: Path, dest: Path, *, readonly: bool) -> list[str]:
    """``btrfs subvolume snapshot [-r]``. Pure."""
    return [
        "btrfs",
        "-q",
        "subvolume",
        "snapshot",
        *(["-r"] if readonly else []),
        str(source),
        str(dest),
    ]


def subvolume_delete_argv(path: Path) -> list[str]:
    """``btrfs subvolume delete``. Pure."""
    return ["btrfs", "-q", "subvolume", "delete", str(path)]


class Btrfs:
    """The real backend: the ``btrfs`` command (btrfs-progs). PRIVILEGED."""

    def _run(self, argv: list[str]) -> None:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise CheckpointError(f"{' '.join(argv)}: {done.stderr.strip() or done.returncode}")

    def create(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(subvolume_create_argv(path))

    def snapshot(self, source: Path, dest: Path, *, readonly: bool) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(snapshot_argv(source, dest, readonly=readonly))

    def is_subvolume(self, path: Path) -> bool:
        # the root of every subvolume has inode 256 on btrfs
        return path.is_dir() and not path.is_symlink() and path.stat().st_ino == 256

    def delete(self, path: Path) -> None:
        self._run(subvolume_delete_argv(path))


def remove_tree(path: Path, backend: Backend | None) -> None:
    """Remove ``path``, a subvolume or a plain directory tree; nothing if absent. I/O."""
    if not path.exists() and not path.is_symlink():
        return
    if backend is not None and backend.is_subvolume(path):
        backend.delete(path)
    else:
        shutil.rmtree(path)


@dataclass(frozen=True)
class Mark:
    """One checkpoint as its manifest describes it."""

    step: str
    fingerprint: str
    path: Path
    run_id: str | None
    created: str
    #: The images whose latest assemble uses this checkpoint; it goes with the last.
    images: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` atomically (a crash leaves the old file or the new one). I/O."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class Store:
    """The checkpoints of one ``<arch>-<init>``, shared by every image of it.

    A checkpoint is addressed by its CONTENT -- its step and fingerprint --
    not by the image that made it: ``worker`` resumes from the checkpoint
    ``minimal`` left, because the two install the same configuration.
    ``root/<step>-<fp>`` is the read-only snapshot, ``root/<step>-<fp>.json`` its
    manifest, written after the snapshot succeeds -- a snapshot without a
    manifest is never trusted. The manifest lists the images using it: an image
    replaces its own older checkpoints, and one goes when no image uses it.
    """

    root: Path
    backend: Backend

    def _name(self, step: str, fp: str) -> str:
        return f"{step}-{fp[:24]}"

    def _manifest(self, mark: Mark) -> Path:
        return mark.path.with_name(mark.path.name + ".json")

    def _read(self, manifest: Path) -> Mark | None:
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        if not isinstance(raw, dict) or raw.get("format") != FORMAT:
            return None
        return Mark(
            step=str(raw.get("step")),
            fingerprint=str(raw.get("fingerprint")),
            path=manifest.with_suffix(""),
            run_id=raw.get("run_id"),
            created=str(raw.get("created")),
            images=tuple(raw.get("images") or ()),
            data=raw.get("data") or {},
        )

    def _write(self, mark: Mark) -> None:
        _write_json(
            self._manifest(mark),
            {
                "format": FORMAT,
                "step": mark.step,
                "fingerprint": mark.fingerprint,
                "run_id": mark.run_id,
                "created": mark.created,
                "images": list(mark.images),
                "data": dict(mark.data),
            },
        )

    def marks(self, step: str | None = None) -> list[Mark]:
        """Every readable checkpoint (of ``step``), oldest first. I/O."""
        if not self.root.is_dir():
            return []
        found = [m for p in sorted(self.root.glob("*.json")) if (m := self._read(p)) is not None]
        return [m for m in found if step is None or m.step == step]

    def find(self, step: str, expected: str) -> Mark | None:
        """The checkpoint ``step`` with fingerprint ``expected``, if it exists. I/O."""
        mark = self._read(self.root / f"{self._name(step, expected)}.json")
        if mark is None or mark.step != step or mark.fingerprint != expected:
            return None
        return mark if mark.path.is_dir() else None

    def save(
        self,
        rootfs: Path,
        step: str,
        fp: str,
        *,
        image: str,
        run_id: str | None,
        data: Mapping[str, Any] | None = None,
    ) -> Mark:
        """Freeze ``rootfs`` as checkpoint ``step`` for ``image``. I/O.

        If the same checkpoint already exists (another image made it from the
        same inputs) it is claimed instead of frozen twice. Either way the
        image's older checkpoints of ``step`` are released.
        """
        mark = self.find(step, fp)
        if mark is None:
            path = self.root / self._name(step, fp)
            remove_tree(path, self.backend)  # a snapshot left without a manifest
            self.backend.snapshot(rootfs, path, readonly=True)
            mark = Mark(
                step=step,
                fingerprint=fp,
                path=path,
                run_id=run_id,
                created=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                images=(image,),
                data=dict(data or {}),
            )
            self._write(mark)
        else:
            mark = self.claim(mark, image)
        self.release_step(step, image, keep=fp)
        return mark

    def claim(self, mark: Mark, image: str) -> Mark:
        """Record that ``image`` now uses ``mark``. I/O."""
        if image in mark.images:
            return mark
        claimed = Mark(**{**mark.__dict__, "images": (*mark.images, image)})
        self._write(claimed)
        return claimed

    def release_step(self, step: str, image: str, *, keep: str | None = None) -> None:
        """Drop ``image`` from its checkpoints of ``step`` (but ``keep``); delete unused. I/O."""
        for mark in self.marks(step):
            if mark.fingerprint == keep or image not in mark.images:
                continue
            left = tuple(i for i in mark.images if i != image)
            if left:
                self._write(Mark(**{**mark.__dict__, "images": left}))
            else:
                self.drop(mark)

    def retain(self, image: str, fingerprints: Iterable[str]) -> None:
        """Drop ``image`` from its checkpoints whose fingerprint is not current. I/O.

        A configuration change leaves an image's older checkpoints useless to it;
        held, they would pin disk until the image happened to save a newer one
        (an image that resumes from another's checkpoint saves nothing).
        """
        current = set(fingerprints)
        for mark in self.marks():
            if image in mark.images and mark.fingerprint not in current:
                left = tuple(i for i in mark.images if i != image)
                if left:
                    self._write(Mark(**{**mark.__dict__, "images": left}))
                else:
                    self.drop(mark)

    def release(self, image: str) -> None:
        """Drop ``image`` from every checkpoint; delete those no image uses. I/O."""
        for step in (PARTIAL, PACKAGES, INSTALL):
            self.release_step(step, image)

    def prune(self) -> list[str]:
        """Remove what no run can use: other formats, snapshots without a manifest. I/O.

        A manifest of another :data:`FORMAT` is invisible to :meth:`marks`, so
        nothing would ever release it; a snapshot whose manifest is gone was
        interrupted mid-save. Returns the names removed.
        """
        if not self.root.is_dir():
            return []
        removed: list[str] = []
        for manifest in sorted(self.root.glob("*.json")):
            if self._read(manifest) is None:
                manifest.unlink(missing_ok=True)
                remove_tree(manifest.with_suffix(""), self.backend)
                removed.append(manifest.stem)
        for snapshot in sorted(self.root.iterdir()):
            if snapshot.is_dir() and not snapshot.with_name(snapshot.name + ".json").exists():
                remove_tree(snapshot, self.backend)
                removed.append(snapshot.name)
        return removed

    def restore(self, mark: Mark, rootfs: Path) -> None:
        """Make ``rootfs`` a writable copy of checkpoint ``mark``. I/O."""
        remove_tree(rootfs, self.backend)
        self.backend.snapshot(mark.path, rootfs, readonly=False)

    def drop(self, mark: Mark) -> None:
        """Remove ``mark`` (manifest first: never a manifest without data). I/O."""
        self._manifest(mark).unlink(missing_ok=True)
        remove_tree(mark.path, self.backend)


def open_store(root: Path, *, backend: Backend | None = None) -> Store | None:
    """The ``<arch>-<init>`` store, or None when ``root`` is not on btrfs (off). I/O."""
    if backend is None:
        if filesystem_type(root) != "btrfs" or shutil.which("btrfs") is None:
            return None
        backend = Btrfs()
    root.mkdir(parents=True, exist_ok=True)
    return Store(root, backend)


@dataclass(frozen=True)
class Fingerprints:
    """The chained fingerprints of one assemble's checkpoints."""

    install: str
    packages: str
    partial: str


def resume_point(store: Store, fps: Fingerprints) -> Mark | None:
    """The deepest checkpoint this assemble can resume from, or None. I/O.

    ``install-partial`` only counts while its rootfs still holds Portage's resume
    list: without one nothing was merged and a fresh install costs nothing extra.
    """
    for step, expected in ((PACKAGES, fps.packages), (INSTALL, fps.install)):
        mark = store.find(step, expected)
        if mark is not None:
            return mark
    mark = store.find(PARTIAL, fps.partial)
    if mark is not None and has_resume_list(mark.path):
        return mark
    return None
