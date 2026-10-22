"""The release core (story 030, tasks 4.1-4.4).

Contract from design.md "shidashi/release.py" and "Data Models":

- 4.1 (R1.1, R1.2, R1.4, R1.8, R2.1, R2.2): ``Variant(arch, init)``;
  ``variants(arch, init, *, all_variants)`` -- the one pair, or every ``arch x init`` of the
  recipe axes, sorted; an unknown value raises ``config.UnknownAxisError``;
  ``release_dir(root, when)`` = ``root / YYYY.MM.DD``; ``build_argv(v, out, outcome,
  passthrough)`` = ``[sys.executable, *parallel.CHILD_COMMAND, "build", arch, init,
  "--images", "all", "-o", out, "--outcome-file", outcome, *passthrough]``.
- 4.2 (R5.1-R5.3): ``write_manifest(directory, body)`` -- ``release.json`` (indent 1, sorted
  keys, UTF-8, trailing newline) through a temporary file renamed over it, then its SHA-256
  in the directory's ``SHA256SUMS``, the other lines kept.
- 4.3 (R6.1-R6.3, R6.5, R6.6): ``sign_release(directory, *, key)`` -- the public key exported
  to ``bentoo-release-key.asc`` and listed in ``SHA256SUMS``, a detached armored signature
  per ``*.iso.DIGESTS``, ``SHA256SUMS`` signed last; no ``.asc`` is listed; a signature that
  cannot be made leaves no ``SHA256SUMS.asc``. ``verify_release(directory, *, key)`` checks
  each signature against ``bentoo-release-key.asc`` alone and returns every failure.
- 4.4 (R7.2, R8.1-R8.4): ``summary(results, upstream, *, signed, verify_failures)`` -> the
  lines (one per variant: status, ISOs or reason, its arch's publish result; one per init
  whose upstream is newer or unknown) and the exit code. ``results`` are
  ``VariantResult(variant, outcome, publish)``; ``upstream`` a sequence of
  ``UpstreamStage3``. (design.md names ``summary(results, upstream)``; the signing state is
  passed in, so the function stays pure -- tasks.md 4.4.)

The maintainer's gpg is ``FakeMaintainerGpg``: it stands in for ``gpgsign`` (check, export,
detach-sign) and for ``seed.gpg_verify``; a "signature" is the signer and the SHA-256 of the
signed bytes, so a file changed after it was signed fails, as with gpg. It is shared with
tests/test_release_cli.py and tests/test_release_contracts.py.
"""

import datetime
import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from shidashi import config, parallel, publish, seed
from shidashi.seed import SeedError
from tests._pending import try_import

Variant: Any = try_import("shidashi.release", "Variant")
VariantResult: Any = try_import("shidashi.release", "VariantResult")
variants: Any = try_import("shidashi.release", "variants")
release_dir: Any = try_import("shidashi.release", "release_dir")
build_argv: Any = try_import("shidashi.release", "build_argv")
write_manifest: Any = try_import("shidashi.release", "write_manifest")
sign_release: Any = try_import("shidashi.release", "sign_release")
verify_release: Any = try_import("shidashi.release", "verify_release")
summary: Any = try_import("shidashi.release", "summary")
BuildOutcome: Any = try_import("shidashi.build_outcome", "BuildOutcome")
ToolchainRefusal: Any = try_import("shidashi.build_outcome", "ToolchainRefusal")
UpstreamStage3: Any = try_import("shidashi.upstream", "UpstreamStage3")
GpgError: Any = try_import("shidashi.gpgsign", "GpgError")

FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
STRANGER = "FEDCBA9876543210FEDCBA9876543210FEDCBA98"
GEN = "20260823T153057Z"
LATEST = "20261018T124555Z"
DAY = "2026.10.25"
KEY_FILE = "bentoo-release-key.asc"


def iso_name(flavor: str, init: str, arch: str, *, day: str = DAY, xz: bool = False) -> str:
    return f"bentoo-{day}-{flavor}-{init}-{arch}{'-xz' if xz else ''}.iso"


# --- the maintainer's gpg, faked ----------------------------------------------------------------


def fake_signature(key: str, data: bytes) -> str:
    return (
        "-----BEGIN PGP SIGNATURE-----\n\n"
        f"{key} {hashlib.sha256(data).hexdigest()}\n"
        "-----END PGP SIGNATURE-----\n"
    )


def fake_public_key(key: str) -> str:
    return (
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
        f"fake public key {key}\n"
        "-----END PGP PUBLIC KEY BLOCK-----\n"
    )


class FakeMaintainerGpg:
    """Stands in for the maintainer's gpg-agent and keyring (``gpgsign``) and for
    ``gpg --verify`` (``seed.gpg_verify``). Holds the secret of ``key`` only."""

    def __init__(self, key: str = FPR) -> None:
        self.key = key
        self.signed: list[str] = []
        self.exported: list[str] = []
        self.checked: list[str] = []
        self.verified: list[tuple[tuple[str, ...], Path]] = []
        self.fail_on: set[str] = set()
        #: data files whose signature check fails whatever it holds (a corrupted download)
        self.reject: set[str] = set()

    def check_key(self, key: str) -> None:
        self.checked.append(key)
        if key != self.key:
            raise GpgError(f"no secret key {key} in the maintainer's keyring")

    def export_public(self, key: str, dest: Path) -> Path:
        if key != self.key:
            raise GpgError(f"unknown key {key}")
        self.exported.append(key)
        dest.write_text(fake_public_key(key), encoding="utf-8")
        return dest

    def detach_sign(self, path: Path, *, key: str) -> Path:
        path = Path(path)
        self.signed.append(path.name)
        asc = path.with_name(path.name + ".asc")
        if path.name in self.fail_on:
            asc.write_text("half a signature", encoding="utf-8")  # what a dying gpg leaves
            raise GpgError(f"cannot sign {path}: signing failed")
        if key != self.key:
            raise GpgError(f"no secret key {key}")
        asc.write_text(fake_signature(key, path.read_bytes()), encoding="utf-8")
        return asc

    def gpg_verify(self, *files: Path, keyring: Path = seed.GENTOO_KEYRING) -> None:
        self.verified.append((tuple(Path(f).name for f in files), Path(keyring)))
        if len(files) != 2:
            raise SeedError("not a detached signature and its data")
        asc, data = (Path(f) for f in files)
        try:
            trusted = Path(keyring).read_text(encoding="utf-8")
            signature = asc.read_text(encoding="utf-8")
            payload = data.read_bytes()
        except OSError as err:
            raise SeedError(f"GPG verification failed for {data.name}: {err}") from err
        found = re.search(r"fake public key (\S+)", trusted)
        if (
            data.name in self.reject
            or found is None
            or signature != fake_signature(found.group(1), payload)
        ):
            raise SeedError(f"GPG verification failed for {data.name}: BAD signature")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeMaintainerGpg:
        """Patch the module attributes and any name the release modules bound by import."""
        gpgsign = importlib.import_module("shidashi.gpgsign")
        targets = {"check_key": gpgsign, "export_public": gpgsign, "detach_sign": gpgsign}
        for name, module in targets.items():
            monkeypatch.setattr(module, name, getattr(self, name))
        monkeypatch.setattr(seed, "gpg_verify", self.gpg_verify)
        for module_name in ("shidashi.release", "shidashi.release_cli"):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            for name in ("check_key", "export_public", "detach_sign", "gpg_verify"):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name, getattr(self, name))
        return self


def listed(directory: Path) -> dict[str, str]:
    """``SHA256SUMS`` as file name -> digest."""
    path = directory / "SHA256SUMS"
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    return {name: digest for digest, _, name in (line.partition("  ") for line in lines if line)}


def release_tree(directory: Path) -> list[str]:
    """A release directory as two builds leave it: their ISOs, DIGESTS and SHA256SUMS
    lines, a pointer file, the outcome files and ``release.json``. Returns the ISO names."""
    directory.mkdir(parents=True, exist_ok=True)
    isos = [iso_name("minimal", "systemd", "v3"), iso_name("kde", "systemd", "v3", xz=True)]
    sums: dict[str, str] = {}
    for name in isos:
        iso = directory / name
        iso.write_bytes(f"iso {name}".encode())
        digests, sha256 = publish.write_digests(iso)
        sums[name] = sha256
        sums[digests.name] = publish.sha256_of(digests)
    pointer = directory / "latest-minimal-systemd-v3.txt"
    pointer.write_text(f"{isos[0]}\n", encoding="utf-8")
    sums[pointer.name] = publish.sha256_of(pointer)
    (directory / "release.json").write_text('{"date": "2026-10-25"}\n', encoding="utf-8")
    sums["release.json"] = publish.sha256_of(directory / "release.json")
    publish.update_sha256sums(directory, sums)
    (directory / ".outcome").mkdir(exist_ok=True)
    (directory / ".outcome" / "v3-systemd.json").write_text("{}", encoding="utf-8")
    return isos


@pytest.fixture
def maintainer(monkeypatch: pytest.MonkeyPatch) -> FakeMaintainerGpg:
    return FakeMaintainerGpg().install(monkeypatch)


# --- 4.1: variants, release_dir, build_argv ------------------------------------------------------


@pytest.fixture
def axes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A variants tree made out of sorted order, with a stray file among the arches."""
    root = tmp_path / "variants"
    for arch in ("znver5", "v3", "arrowlake"):
        (root / "arch" / arch).mkdir(parents=True)
    for init in ("systemd", "openrc"):
        (root / "init" / init).mkdir(parents=True)
    (root / "arch" / "README").write_text("not an arch\n", encoding="utf-8")
    monkeypatch.setenv("SHIDASHI_VARIANTS_DIR", str(root))
    return root


def pairs(found: Sequence[Any]) -> list[tuple[str, str]]:
    return [(v.arch, v.init) for v in found]


def test_variants_of_one_pair_is_exactly_that_variant(axes: Path) -> None:
    """R1.1."""
    assert pairs(variants("v3", "openrc", all_variants=False)) == [("v3", "openrc")]


def test_variants_all_is_every_arch_by_every_init_in_sorted_order(axes: Path) -> None:
    """R1.2: the axes on disk, not a list in the code; sorted whatever the disk order."""
    assert pairs(variants(None, None, all_variants=True)) == [
        ("arrowlake", "openrc"),
        ("arrowlake", "systemd"),
        ("v3", "openrc"),
        ("v3", "systemd"),
        ("znver5", "openrc"),
        ("znver5", "systemd"),
    ]


@pytest.mark.parametrize(
    ("arch", "init", "unknown"),
    [
        pytest.param("v5", "systemd", "v5", id="arch"),
        pytest.param("v3", "runit", "runit", id="init"),
        pytest.param("systemd", "v3", "systemd", id="swapped"),
    ],
)
def test_variants_with_a_value_no_recipe_has_raise_naming_it(
    axes: Path, arch: str, init: str, unknown: str
) -> None:
    """R1.4: each axis is checked -- a valid arch does not carry an unknown init through,
    and an init's name is not an arch."""
    with pytest.raises(config.UnknownAxisError) as err:
        variants(arch, init, all_variants=False)
    assert unknown in str(err.value)


@pytest.mark.parametrize(
    ("when", "name"),
    [
        (datetime.date(2026, 10, 25), "2026.10.25"),
        (datetime.date(2026, 1, 5), "2026.01.05"),
    ],
)
def test_release_dir_is_the_dotted_date_under_the_root(
    tmp_path: Path, when: datetime.date, name: str
) -> None:
    """R1.8."""
    assert release_dir(tmp_path / "releases", when) == tmp_path / "releases" / name


def test_build_argv_is_the_child_build_of_that_variant_with_every_image(tmp_path: Path) -> None:
    """R2.1: the same interpreter, the child command of parallel.py, every image, the
    release directory as output, and the variant's outcome file."""
    out, outcome = tmp_path / DAY, tmp_path / DAY / ".outcome" / "v3-systemd.json"
    argv = build_argv(Variant("v3", "systemd"), out, outcome, [])
    assert [str(a) for a in argv] == [
        sys.executable,
        *parallel.CHILD_COMMAND,
        "build",
        "v3",
        "systemd",
        "--images",
        "all",
        "-o",
        str(out),
        "--outcome-file",
        str(outcome),
    ]


def test_build_argv_passes_every_option_through_in_its_order_after_the_outcome_file(
    tmp_path: Path,
) -> None:
    """R2.2: values stay beside their options; nothing is sorted, merged or dropped."""
    passthrough = [
        "--boot-test",
        "--jobs",
        "8",
        "--compression",
        "both",
        "--no-sbom",
        "--no-download",
        "--local",
        "--allow-dirty",
        "--work-dir",
        str(tmp_path / "work"),
        "--verbose",
    ]
    argv = [
        str(a)
        for a in build_argv(Variant("znver5", "openrc"), tmp_path, tmp_path / "o", passthrough)
    ]
    assert argv[-len(passthrough) :] == passthrough
    assert argv[: -len(passthrough)][-2:] == ["--outcome-file", str(tmp_path / "o")]


# --- 4.2: release.json and its SHA256SUMS line -------------------------------------------------


BODY = {
    "date": "2026-10-25",
    "variants": [{"arch": "v3", "init": "systemd", "status": "ok"}],
    "shidashi": {"commit": "31770bb", "dirty": False},
    "note": "Bentoo · ドリル",
}


def test_write_manifest_writes_release_json_sorted_indented_utf8_with_a_newline(
    tmp_path: Path,
) -> None:
    write_manifest(tmp_path, BODY)
    path = tmp_path / "release.json"
    assert path.is_file(), "no release.json"
    expected = json.dumps(BODY, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    assert path.read_bytes() == expected.encode("utf-8")


def test_write_manifest_adds_its_digest_and_keeps_every_other_line(tmp_path: Path) -> None:
    """R5.2, R9.2: the builds' lines stay as they were."""
    iso = tmp_path / iso_name("minimal", "systemd", "v3")
    iso.write_bytes(b"iso")
    publish.update_sha256sums(tmp_path, {iso.name: publish.sha256_of(iso)})
    write_manifest(tmp_path, BODY)
    assert (tmp_path / "release.json").is_file(), "no release.json"
    assert listed(tmp_path) == {
        iso.name: publish.sha256_of(iso),
        "release.json": publish.sha256_of(tmp_path / "release.json"),
    }


def test_write_manifest_over_an_earlier_one_replaces_its_line_never_adds_a_second(
    tmp_path: Path,
) -> None:
    """A rerun into an unsigned directory: one release.json, one line, the new digest."""
    write_manifest(tmp_path, {**BODY, "date": "2026-10-24"})
    write_manifest(tmp_path, BODY)
    assert (tmp_path / "SHA256SUMS").is_file(), "no SHA256SUMS"
    text = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8")
    assert text.count("  release.json\n") == 1
    assert listed(tmp_path)["release.json"] == publish.sha256_of(tmp_path / "release.json")
    assert json.loads((tmp_path / "release.json").read_text(encoding="utf-8")) == BODY


def test_write_manifest_renames_a_sibling_temporary_file_over_release_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.3."""
    (tmp_path / "release.json").write_text('{"date": "old"}\n', encoding="utf-8")
    renames: list[tuple[Path, Path]] = []
    real_replace, real_rename = os.replace, os.rename

    def spy_replace(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst, *a, **k)

    def spy_rename(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "rename", spy_rename)
    write_manifest(tmp_path, BODY)
    into = [(src, dst) for src, dst in renames if dst == tmp_path / "release.json"]
    assert len(into) == 1, renames
    ((src, _dst),) = into
    assert src.parent == tmp_path and src.name != "release.json"


def test_write_manifest_with_a_corrupt_sha256sums_raises_and_leaves_it_as_it_was(
    tmp_path: Path,
) -> None:
    (tmp_path / "SHA256SUMS").write_text("not a checksum line\n", encoding="utf-8")
    with pytest.raises(publish.PublishError):
        write_manifest(tmp_path, BODY)
    assert (tmp_path / "SHA256SUMS").read_text(encoding="utf-8") == "not a checksum line\n"


# --- 4.3: sign_release and verify_release --------------------------------------------------


def test_sign_release_signs_every_digests_then_sha256sums_last(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R6.2, R6.3: one detached signature per ISO's DIGESTS (the xz twin included); the
    outcome files, the pointer and the ISOs themselves are not signed."""
    isos = release_tree(tmp_path)
    made = sign_release(tmp_path, key=FPR)
    digests = sorted(f"{iso}.DIGESTS" for iso in isos)
    assert sorted(maintainer.signed[:-1]) == digests
    assert maintainer.signed[-1:] == ["SHA256SUMS"]
    assert sorted(Path(p).name for p in made) == sorted(
        [*(f"{d}.asc" for d in digests), "SHA256SUMS.asc"]
    )
    for name in [*digests, "SHA256SUMS"]:
        signed = (tmp_path / name).read_bytes()
        assert (tmp_path / f"{name}.asc").read_text(encoding="utf-8") == fake_signature(FPR, signed)


def test_sign_release_exports_the_key_and_lists_it_before_sha256sums_is_signed(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R6.1, R6.3: the key is covered by SHA256SUMS, and SHA256SUMS.asc signs the final
    SHA256SUMS -- a line added after signing would break it."""
    release_tree(tmp_path)
    sign_release(tmp_path, key=FPR)
    verify_release(tmp_path, key=FPR)
    key = tmp_path / KEY_FILE
    assert key.is_file(), "the public key was not exported"
    assert key.read_text(encoding="utf-8") == fake_public_key(FPR)
    assert listed(tmp_path)[KEY_FILE] == publish.sha256_of(key)
    final = (tmp_path / "SHA256SUMS").read_bytes()
    assert (tmp_path / "SHA256SUMS.asc").read_text(encoding="utf-8") == fake_signature(FPR, final)


def test_sign_release_never_lists_a_signature_or_an_outcome_file(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """A signature covers its file (GOTCHA: signing order); outcome files are for
    inspection only. What is listed is what exists, with its digest."""
    release_tree(tmp_path)
    sign_release(tmp_path, key=FPR)
    names = listed(tmp_path)
    assert KEY_FILE in names and "release.json" in names
    assert not [n for n in names if n.endswith(".asc") and n != KEY_FILE]
    assert not [n for n in names if "outcome" in n]
    for name, digest in names.items():
        assert publish.sha256_of(tmp_path / name) == digest, name


def test_verify_release_does_not_take_the_exported_key_for_a_signature(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """Hostile (a third ``.asc``): ``bentoo-release-key.asc`` ends in ``.asc`` but signs
    nothing; a verifier that treats it as one reports a release that is fine as broken."""
    release_tree(tmp_path)
    sign_release(tmp_path, key=FPR)
    assert verify_release(tmp_path, key=FPR) == []
    checked = {names[0] for names, _keyring in maintainer.verified}
    assert KEY_FILE not in checked
    assert "SHA256SUMS.asc" in checked


def test_verify_release_trusts_the_exported_key_alone(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R6.5: every check is against ``bentoo-release-key.asc``; swap that file for
    another key and every signature fails."""
    release_tree(tmp_path)
    sign_release(tmp_path, key=FPR)
    verify_release(tmp_path, key=FPR)
    assert maintainer.verified, "nothing was verified"
    assert {keyring for _names, keyring in maintainer.verified} == {tmp_path / KEY_FILE}
    (tmp_path / KEY_FILE).write_text(fake_public_key(STRANGER), encoding="utf-8")
    failures = verify_release(tmp_path, key=FPR)
    assert len(failures) == 3, failures  # two DIGESTS and SHA256SUMS


def test_verify_release_reports_every_signature_that_fails_naming_its_file(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R6.5: two files changed after signing -> two failures, each naming its file;
    the untouched DIGESTS is not among them."""
    isos = release_tree(tmp_path)
    sign_release(tmp_path, key=FPR)
    tampered = tmp_path / f"{isos[0]}.DIGESTS"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with (tmp_path / "SHA256SUMS").open("a", encoding="utf-8") as sums:
        sums.write("0" * 64 + "  extra.iso\n")
    failures = verify_release(tmp_path, key=FPR)
    assert len(failures) == 2, failures
    assert any(tampered.name in f for f in failures), failures
    assert any("SHA256SUMS" in f and ".DIGESTS" not in f for f in failures), failures
    assert not any(isos[1] in f for f in failures), failures


def test_a_digests_that_cannot_be_signed_leaves_no_sha256sums_asc(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R6.6: the failure surfaces, and SHA256SUMS is never signed after it."""
    isos = release_tree(tmp_path)
    maintainer.fail_on.add(f"{isos[0]}.DIGESTS")
    with pytest.raises(GpgError):
        sign_release(tmp_path, key=FPR)
    assert not (tmp_path / "SHA256SUMS.asc").exists()


def test_a_sha256sums_signature_that_fails_halfway_leaves_no_sha256sums_asc(
    tmp_path: Path, maintainer: FakeMaintainerGpg
) -> None:
    """R6.6: gpg died while writing SHA256SUMS.asc (half a file is on disk)."""
    release_tree(tmp_path)
    maintainer.fail_on.add("SHA256SUMS")
    with pytest.raises(GpgError):
        sign_release(tmp_path, key=FPR)
    assert maintainer.signed[-1:] == ["SHA256SUMS"]
    assert not (tmp_path / "SHA256SUMS.asc").exists()


# --- 4.4: summary lines and exit code ---------------------------------------------------------


def outcome(arch: str, init: str, status: str, **fields: Any) -> Any:
    base: dict[str, Any] = {
        "arch": arch,
        "init": init,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "isos": [iso_name("minimal", init, arch), iso_name("kde", init, arch)]
        if status == "ok"
        else [],
        "failed_images": [],
        "message": "",
        "toolchain": None,
    }
    base.update(fields)
    return BuildOutcome.model_validate(base)


def result(arch: str, init: str, status: str, publish_line: str, **fields: Any) -> Any:
    return VariantResult(
        variant=Variant(arch, init),
        outcome=outcome(arch, init, status, **fields),
        publish=publish_line,
    )


def published(arch: str) -> str:
    return f"published /var/cache/shidashi/publish/{arch}/{GEN} (2144 finals)"


SUCCESSOR = f"/var/cache/shidashi/binpkgs/v3/{GEN}-gcc16"
REFUSED = {
    "exit_code": 1,
    "message": "belongs to another generation",
    "toolchain": {
        "differences": ["gcc 15.2.1 -> 16.1.0"],
        "pkgdir": f"/var/cache/shidashi/binpkgs/v3/{GEN}",
        "successor": SUCCESSOR,
    },
}


def same_upstream(*inits: str) -> list[Any]:
    return [UpstreamStage3(init=i, pinned=GEN, latest=GEN, note="") for i in inits]


def lines_of(lines: Sequence[str], arch: str, init: str) -> list[str]:
    """The summary lines that are about this variant."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(arch)}\W+{re.escape(init)}(?![\w-])")
    return [line for line in lines if pattern.search(line)]


def line_of(lines: Sequence[str], arch: str, init: str) -> str:
    """The one summary line of this variant."""
    mine = lines_of(lines, arch, init)
    assert len(mine) == 1, (arch, init, lines)
    return mine[0]


def run_summary(
    results: Sequence[Any],
    upstream: Sequence[Any],
    *,
    ok: bool = True,
    failures: Sequence[str] = (),
) -> tuple[list[str], int]:
    lines, code = summary(results, upstream, signed=ok, verify_failures=list(failures))
    return list(lines), code


def test_summary_is_0_only_when_every_variant_built_and_published() -> None:
    """R8.1, R8.3: one line per variant, its ISOs and its arch's publish result."""
    results = [
        result("v3", "openrc", "ok", published("v3")),
        result("v3", "systemd", "ok", published("v3")),
    ]
    lines, code = run_summary(results, same_upstream("openrc", "systemd"))
    assert code == 0, lines
    for arch, init in (("v3", "openrc"), ("v3", "systemd")):
        mine = line_of(lines, arch, init)
        assert "ok" in mine and "published" in mine
        assert "2" in mine or iso_name("kde", init, arch) in mine


def test_summary_of_a_worker_owned_arch_skipped_is_still_0() -> None:
    """R4.2, R8.3: a skip is not a failure; the owner is named."""
    results = [result("znver5", "systemd", "ok", "skipped: znver5 is owned by bentoo-lab")]
    lines, code = run_summary(results, same_upstream("systemd"))
    assert code == 0, lines
    assert "bentoo-lab" in line_of(lines, "znver5", "systemd")


def test_summary_is_1_when_a_publish_failed_and_names_why() -> None:
    results = [result("v3", "systemd", "ok", "failed: the gpg-agent extra socket is gone")]
    lines, code = run_summary(results, same_upstream("systemd"))
    assert code == 1
    assert "extra socket is gone" in line_of(lines, "v3", "systemd")


@pytest.mark.parametrize(
    ("status", "fields", "named"),
    [
        pytest.param(
            "images-failed", {"failed_images": ["gnome", "kde"]}, ["gnome", "kde"], id="images"
        ),
        pytest.param(
            "error", {"message": "no outcome file: build exited 137"}, ["137"], id="error"
        ),
        pytest.param("toolchain-refused", REFUSED, ["gcc", "15", "16", SUCCESSOR], id="toolchain"),
    ],
)
def test_summary_of_a_variant_that_did_not_build_is_1_and_its_line_says_why(
    status: str, fields: dict[str, Any], named: list[str]
) -> None:
    """R8.1, R8.2, R8.4."""
    results = [
        result("v3", "systemd", "ok", published("v3")),
        result("v3", "openrc", status, published("v3"), **fields),
    ]
    lines, code = run_summary(results, same_upstream("openrc", "systemd"))
    assert code == 1, lines
    line = line_of(lines, "v3", "openrc")
    assert status in line
    for text in named:
        assert text in line, (text, line)


def test_summary_puts_the_successor_on_the_refused_line_only() -> None:
    """Hostile: two variants of one arch; the refusal is openrc's, not systemd's."""
    results = [
        result("v3", "openrc", "toolchain-refused", "not run: build failed", **REFUSED),
        result("v3", "systemd", "ok", published("v3")),
    ]
    lines, _code = run_summary(results, same_upstream("openrc", "systemd"))
    assert SUCCESSOR in line_of(lines, "v3", "openrc")
    assert SUCCESSOR not in line_of(lines, "v3", "systemd")


def test_summary_is_1_when_the_checksums_were_left_unattested() -> None:
    """R8.4: every variant built, but the release has no valid SHA256SUMS.asc."""
    results = [result("v3", "systemd", "ok", published("v3"))]
    _lines, code = run_summary(results, same_upstream("systemd"), ok=False)
    assert code == 1


def test_summary_names_each_failed_check_and_is_1() -> None:
    """R6.5, R8.4."""
    results = [result("v3", "systemd", "ok", published("v3"))]
    failures = [
        f"{iso_name('kde', 'systemd', 'v3')}.DIGESTS.asc: BAD signature",
        "SHA256SUMS.asc: BAD signature",
    ]
    lines, code = run_summary(results, same_upstream("systemd"), failures=failures)
    assert code == 1
    text = "\n".join(lines)
    assert all(f.split(":")[0] in text for f in failures), lines


def test_summary_names_a_newer_upstream_stage3_and_the_pin() -> None:
    """R7.2: both snapshots; never a reason to fail."""
    upstream = [UpstreamStage3(init="systemd", pinned=GEN, latest=LATEST, note="")]
    lines, code = run_summary([result("v3", "systemd", "ok", published("v3"))], upstream)
    assert code == 0
    assert [line for line in lines if LATEST in line and GEN in line], lines


def test_summary_never_calls_an_older_or_equal_upstream_newer() -> None:
    """Hostile: an upstream equal to or older than the pin is not news."""
    older = "20260101T000000Z"
    upstream = [
        UpstreamStage3(init="systemd", pinned=GEN, latest=GEN, note=""),
        UpstreamStage3(init="openrc", pinned=GEN, latest=older, note=""),
    ]
    results = [
        result("v3", "systemd", "ok", published("v3")),
        result("v3", "openrc", "ok", published("v3")),
    ]
    lines, code = run_summary(results, upstream)
    assert code == 0
    line_of(lines, "v3", "systemd")
    line_of(lines, "v3", "openrc")
    assert not [line for line in lines if "newer" in line.lower()], lines


def test_summary_reports_an_unknown_upstream_with_its_reason_and_stays_0() -> None:
    """R7.4, R7.6."""
    upstream = [UpstreamStage3(init="systemd", pinned=GEN, latest=None, note="--no-download")]
    lines, code = run_summary([result("v3", "systemd", "ok", published("v3"))], upstream)
    assert code == 0
    assert [line for line in lines if "unknown" in line.lower() and "--no-download" in line], lines
