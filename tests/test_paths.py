from __future__ import annotations

from pathlib import Path

import pytest

from planner.paths import (
    DATA_SUBDIRS,
    CloudSyncedPathError,
    Layout,
    is_cloud_synced,
    layout,
)


def test_layout_creates_every_data_subdir(planner_home: Path) -> None:
    lay = layout()
    assert lay.root == planner_home.resolve()
    lay.ensure()
    for sub in DATA_SUBDIRS:
        assert (lay.data / sub).is_dir()
    assert lay.out.is_dir()
    assert lay.lock_file.parent == lay.data


@pytest.mark.parametrize(
    "parts",
    [
        ("Users", "jp", "OneDrive", "Planner"),
        ("Dropbox", "x"),
        ("Google Drive", "My Drive"),
    ],
)
def test_cloud_sync_paths_are_detected(tmp_path: Path, parts: tuple[str, ...]) -> None:
    p = tmp_path.joinpath(*parts)
    assert is_cloud_synced(p)


def test_local_path_is_not_cloud_synced(tmp_path: Path) -> None:
    assert not is_cloud_synced(tmp_path / "Planner")


def test_ensure_refuses_cloud_synced_root(tmp_path: Path) -> None:
    root = tmp_path / "OneDrive" / "Planner"
    root.mkdir(parents=True)
    with pytest.raises(CloudSyncedPathError):
        Layout(root).ensure()
    assert not (root / "data").exists()
