#!/usr/bin/env python3
"""rootfs-manifest.py ROOTFS OUT.json — what an assembled rootfs holds, to compare two builds.

Written for the trunk's equivalence test (2026-10-05): the same image assembled whole
from the stage3 and grown from its trunk must hold the same thing. Records

  packages  per installed package (the vdb): slot, build id, USE, and a digest of its
            CONTENTS with the mtimes left out (a merge's time is not its content);
  files     per path outside the vdb: type, mode, owner, size, and the sha256 of a
            regular file or the target of a link.

Volatile paths -- logs, caches, runtime state, the vdb itself -- are skipped. Stdlib
only: it runs in the builder guest's own Python.
"""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

#: Not content: what a run leaves behind, or what is recorded per package instead.
SKIP = (
    "dev",
    "proc",
    "sys",
    "run",
    "tmp",
    "var/tmp",
    "var/log",
    "var/cache",
    "var/db/pkg",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def contents_digest(contents: Path) -> str:
    """CONTENTS without the trailing mtime of ``obj`` and ``sym`` lines."""
    lines = []
    for line in read(contents).splitlines():
        kind = line.split(" ", 1)[0]
        if kind in ("obj", "sym"):
            line = line.rsplit(" ", 1)[0]
        lines.append(line)
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def packages(root: Path) -> dict[str, dict[str, str]]:
    found = {}
    for pkg in sorted((root / "var/db/pkg").glob("*/*")):
        found[f"{pkg.parent.name}/{pkg.name}"] = {
            "slot": read(pkg / "SLOT"),
            "build_id": read(pkg / "BUILD_ID"),
            "use": " ".join(sorted(read(pkg / "USE").split())),
            "contents": contents_digest(pkg / "CONTENTS"),
        }
    return found


def files(root: Path) -> dict[str, list[object]]:
    found: dict[str, list[object]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(d for d in dirnames if os.path.join(rel_dir, d) not in SKIP)
        for name in sorted(dirnames) + sorted(filenames):
            rel = os.path.join(rel_dir, name)
            path = root / rel
            st = path.lstat()
            entry: list[object] = [stat.filemode(st.st_mode), st.st_uid, st.st_gid]
            if stat.S_ISLNK(st.st_mode):
                entry.append(os.readlink(path))
            elif stat.S_ISREG(st.st_mode):
                entry += [st.st_size, sha256(path)]
            found[rel] = entry
    return found


def main() -> int:
    if len(sys.argv) != 3:
        print((__doc__ or "").splitlines()[0], file=sys.stderr)
        return 2
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    manifest = {"root": str(root), "packages": packages(root), "files": files(root)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{out}: {len(manifest['packages'])} packages, {len(manifest['files'])} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
