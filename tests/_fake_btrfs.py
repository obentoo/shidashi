"""A btrfs stand-in for the checkpoint tests: a snapshot is a directory copy."""

import shutil
from pathlib import Path


class FakeBackend:
    """:class:`shidashi.checkpoint.Backend` over plain directories, recording calls."""

    def __init__(self) -> None:
        self.subvolumes: set[Path] = set()
        self.readonly: set[Path] = set()
        self.calls: list[tuple[str, Path]] = []

    def create(self, path: Path) -> None:
        self.calls.append(("create", path))
        path.mkdir(parents=True)
        self.subvolumes.add(path)

    def snapshot(self, source: Path, dest: Path, *, readonly: bool) -> None:
        self.calls.append(("snapshot-ro" if readonly else "snapshot", dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, symlinks=True)
        self.subvolumes.add(dest)
        if readonly:
            self.readonly.add(dest)

    def is_subvolume(self, path: Path) -> bool:
        return path in self.subvolumes

    def delete(self, path: Path) -> None:
        self.calls.append(("delete", path))
        shutil.rmtree(path)
        self.subvolumes.discard(path)
        self.readonly.discard(path)
