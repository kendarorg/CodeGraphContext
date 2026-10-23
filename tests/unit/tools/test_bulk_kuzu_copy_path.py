"""COPY statements take paths with forward slashes on Windows, and unchanged elsewhere."""
import pytest

from codegraphcontext.tools.indexing.persistence import bulk_kuzu


@pytest.mark.parametrize("platform", ["win32"])
def test_windows_paths_use_forward_slashes(monkeypatch, platform):
    monkeypatch.setattr(bulk_kuzu.sys, "platform", platform)
    path = r"C:\Users\me\AppData\Local\Temp\cgc-bulk-abc\Directory.parquet"
    assert bulk_kuzu.copy_path(path) == "C:/Users/me/AppData/Local/Temp/cgc-bulk-abc/Directory.parquet"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_paths_are_unchanged(monkeypatch, platform):
    monkeypatch.setattr(bulk_kuzu.sys, "platform", platform)
    path = "/tmp/cgc-bulk-abc/Directory.parquet"
    assert bulk_kuzu.copy_path(path) == path
