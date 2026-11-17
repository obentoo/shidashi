"""scripts/zeo-builder-image.sh --dry-run: the plan and the Containerfile, from fake trees.

The real build runs on a worker (podman, the desktop fork point, hours of emerge); these
cases pin what the script decides before it touches one: which zeo, which Rust, which
tag, and what the build may never do.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "zeo-builder-image.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, str]:
    gentoo = tmp_path / "gentoo"
    (gentoo / "metadata").mkdir(parents=True)
    (gentoo / "metadata" / "timestamp.chk").write_text("Tue, 17 Nov 2026 00:16:01 +0000\n")
    overlay = tmp_path / "bentoo"
    for rel in (
        "app-editors/zeo/zeo-0.9.0_p20261108.ebuild",
        "app-editors/zeo/zeo-0.12.0_p20261114.ebuild",
        "app-editors/zeo/zeo-0.10.3_p20261110.ebuild",
        "dev-lang/rust-bin/rust-bin-1.100.0.ebuild",
    ):
        (overlay / rel).parent.mkdir(parents=True, exist_ok=True)
        (overlay / rel).write_text("EAPI=8\n")
    _git(overlay, "init", "-q")
    _git(overlay, "add", ".")
    _git(overlay, "commit", "-qm", "fixture")
    use = tmp_path / "zeo.use"
    use.write_text("app-editors/zeo ai-jail inspector llvm_slot_23\n")
    env = dict(os.environ)
    env.update(
        ZEO_BUILDER_GENTOO=str(gentoo),
        ZEO_BUILDER_OVERLAY=str(overlay),
        ZEO_BUILDER_USE=str(use),
        ZEO_BUILDER_RUST="1.100.0",
    )
    return env


def _dry_run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--worker", "nowhere.invalid"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_the_plan_takes_the_newest_zeo_and_the_requested_rust(trees: dict[str, str]) -> None:
    done = _dry_run(trees)
    assert done.returncode == 0, done.stderr
    assert "zeo      zeo-0.12.0_p20261114" in done.stdout  # version order, not text order
    assert "--onlydeps =app-editors/zeo-0.12.0_p20261114" in done.stdout
    assert "=dev-lang/rust-bin-1.100.0" in done.stdout
    assert "eselect rust set rust-bin-1.100.0" in done.stdout
    assert 'LLVM_SLOT="23"' in done.stdout
    assert "-ccache" in done.stdout  # userpriv cannot write the base's ccache dir
    # the trees and zeo's USE are inside the image, before any emerge uses them
    file = done.stdout.split("--- Containerfile", 1)[1]
    copy = file.index("cp -a /mnt/trees/gentoo /var/db/repos/gentoo")
    assert copy < file.index("emerge ") and "cp -a /mnt/trees/bentoo /var/db/repos/bentoo" in file
    assert "cp /mnt/trees/zeo.use /etc/portage/package.use/zeo" in file
    assert "x11-base/xorg-server xvfb minimal" in done.stdout  # no drivers (mesa[X])
    assert 'INPUT_DEVICES="" VIDEO_CARDS=""' in done.stdout
    assert (
        "rustup toolchain install 1.100.0 --profile minimal --target wasm32-wasip2" in done.stdout
    )
    # rustup's homes never reach the emerges (dev-util/rustup's install phase would
    # try to create them inside the sandbox)
    file = done.stdout.split("--- Containerfile", 1)[1]
    assert file.index("ENV RUSTUP_HOME") > file.rindex("emerge ")
    assert "image    localhost/shidashi-zeo-builder:" in done.stdout
    assert 'org.bentoo.builder.gentoo-timestamp="Tue, 17 Nov 2026 00:16:01 +0000"' in done.stdout


def test_the_build_never_empties_the_shared_distfiles_cache(trees: dict[str, str]) -> None:
    done = _dry_run(trees)
    assert "distfiles/*" not in done.stdout
    # the worker-side half mounts the shared cache read-write: it must not delete there
    assert "rm -rf /var/cache/distfiles" not in SCRIPT.read_text()


def test_a_rust_the_overlay_does_not_carry_is_refused(trees: dict[str, str]) -> None:
    done = _dry_run({**trees, "ZEO_BUILDER_RUST": "1.101.0"})
    assert done.returncode == 2
    assert "has no dev-lang/rust-bin-1.101.0" in done.stderr


def test_missing_inputs_are_refused_by_name(trees: dict[str, str], tmp_path: Path) -> None:
    done = _dry_run({**trees, "ZEO_BUILDER_GENTOO": str(tmp_path / "none")})
    assert done.returncode == 2 and "no ::gentoo tree" in done.stderr
    done = _dry_run({**trees, "ZEO_BUILDER_USE": str(tmp_path / "none.use")})
    assert done.returncode == 2 and "no package.use for app-editors/zeo" in done.stderr


def test_the_llvm_slot_comes_from_zeos_package_use(trees: dict[str, str], tmp_path: Path) -> None:
    # make.conf may say otherwise; zeo's own llvm_slot_NN is what it builds against
    done = _dry_run(trees)
    assert 'LLVM_SLOT="23"' in done.stdout, done.stdout
    use = tmp_path / "zeo-nollvm.use"
    use.write_text("app-editors/zeo ai-jail\n")
    done = _dry_run({**trees, "ZEO_BUILDER_USE": str(use), "ZEO_BUILDER_LLVM_SLOT": "21"})
    assert 'LLVM_SLOT="21"' in done.stdout
