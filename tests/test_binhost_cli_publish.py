"""``shidashi binhost publish <arch> --key FPR`` (story 029, task 5.2; R5.9, R5.10, R5.7,
R2.8).

Through the real CLI with the lab, the fake toolbox and the fake host ``gpg``/``gpgconf``
of tests/test_publish_pool.py. Here only ffmpeg build 3 is a recorded final, so the
command publishes 1 instance and leaves 3 unrecorded ones out (curl, plain, old): two
different numbers, so each can be told in the output.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from shidashi import config, workers
from shidashi.workers import WorkerEntry
from tests.test_binhost_cli_gc import FakeTools, path_of
from tests.test_binhost_cli_show import (
    FFMPEG,
    GEN,
    OTHER_GEN,
    Lab,
    binhost,
    per_init_generation,
)
from tests.test_publish_pool import ENTRIES, FINAL_FFMPEG, FPR, extra_socket, publish_lab


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lab]:
    with extra_socket() as agent:
        lab = publish_lab(tmp_path, monkeypatch, agent, *ENTRIES)
        lab.record(finals={"minimal": [(FFMPEG, 3)]}, transients={"kde": [(FFMPEG, 2)]})
        yield lab


def _numbers(output: str, *drop: str) -> list[int]:
    """The numbers the output says, once the paths and the arch are taken out of it."""
    flat = "".join(output.split())
    for text in sorted(drop, key=len, reverse=True):
        flat = flat.replace("".join(text.split()), " ")
    flat = flat.replace("v3", " ")
    return [int(n) for n in re.findall(r"\d+", flat)]


def test_publish_prints_the_pool_the_published_and_the_unrecorded_left_out(lab: Lab) -> None:
    """R5.10, the default destination ``<cache>/publish/<arch>/<generation>``."""
    dest = lab.cache / "publish" / "v3" / GEN
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 0, result.output
    assert "".join(str(dest).split()) in "".join(result.output.split())
    numbers = _numbers(result.output, str(dest), str(lab.root), FPR)
    assert 1 in numbers and 3 in numbers, (numbers, result.output)
    assert "unrecorded" in result.output.lower()
    assert (dest / path_of(FINAL_FFMPEG)).is_file()


def test_publish_into_another_destination(lab: Lab) -> None:
    dest = lab.root / "elsewhere" / "pool"
    result = binhost("publish", "v3", "--key", FPR, "--dest", str(dest))
    assert result.exit_code == 0, result.output
    assert (dest / path_of(FINAL_FFMPEG)).is_file()
    assert not (lab.cache / "publish").exists()


def test_publish_of_a_worker_owned_arch_is_refused_naming_the_way_back(
    lab: Lab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5.9: the key never goes to a worker; nothing is copied or signed."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = config.workers_dir() / "workers.json"
    path.parent.mkdir(parents=True)
    workers.save_registry(
        path,
        {
            "bentoo-lab": WorkerEntry(
                name="bentoo-lab",
                host_key="ssh-ed25519 AAAA",
                host_key_fingerprint="SHA256:x",
                paired_at="2026-10-17T00:00:00Z",
                cache_archs=("v3",),
            )
        },
    )
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "shidashi worker cache-owner bentoo-lab --arch v3 --off" in flat
    assert not (lab.cache / "publish").exists()
    assert FakeTools.calls("gpkg-sign") == []


def test_publish_with_no_recorded_final_exits_1_naming_the_factory(lab: Lab) -> None:
    """R5.7 seen from the command: an error message, no traceback."""
    (lab.pkgdir() / ".shidashi-pool.json").unlink()
    result = binhost("publish", "v3", "--key", FPR)
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "factory" in result.output
    assert not (lab.cache / "publish" / "v3" / GEN).exists()


def test_publish_takes_the_pkgdir_and_toolbox_of_its_init(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.8: ``--init openrc`` publishes openrc's generation (another one here) into its
    own default pool, with openrc's toolbox; systemd's pool is not made."""
    per_init_generation(monkeypatch)
    other = lab.pool(*ENTRIES, generation=OTHER_GEN)
    (other / ".shidashi-pool.json").write_bytes((lab.pkgdir() / ".shidashi-pool.json").read_bytes())
    result = binhost("publish", "v3", "--key", FPR, "--init", "openrc")
    assert result.exit_code == 0, result.output
    assert (lab.cache / "publish" / "v3" / OTHER_GEN / path_of(FINAL_FFMPEG)).is_file()
    assert not (lab.cache / "publish" / "v3" / GEN).exists()
    assert FakeTools.resolved and {init for _a, init in FakeTools.resolved} == {"openrc"}
