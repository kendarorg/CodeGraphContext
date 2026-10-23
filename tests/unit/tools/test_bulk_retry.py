"""A failed bulk write of one file must not leave rows behind for the retry."""

from unittest.mock import MagicMock

import pytest

from codegraphcontext.tools.indexing.persistence import writer as writer_module
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex

REPO = "/repo"
PATH = "/repo/a.py"


def _writer(symbol_index=None):
    driver = MagicMock()
    driver.get_backend_type.return_value = "kuzudb"
    writer = GraphWriter(driver, symbol_index=symbol_index)
    assert writer.enable_bulk()
    return writer


def _file_data():
    return {
        "path": PATH,
        "repo_path": REPO,
        "lang": "python",
        "imports": [],
        "functions": [
            {"name": "x", "line_number": 1, "end_line": 2, "args": [], "cyclomatic_complexity": 1},
        ],
        "classes": [],
        "variables": [],
    }


def _write(writer):
    writer.add_file_to_graph(_file_data(), "repo", {}, repo_path_str=REPO)


@pytest.fixture
def fail_first_import_rows(monkeypatch):
    """Make the first _import_rows call raise, after the node rows were collected."""
    real = writer_module._import_rows
    calls = {"n": 0}

    def flaky(file_data, lang):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient write failure")
        return real(file_data, lang)

    monkeypatch.setattr(writer_module, "_import_rows", flaky)
    return calls


def _function_entries(index):
    return index.entries("Function", "x", PATH)


def test_failed_write_leaves_no_buffered_rows(fail_first_import_rows):
    index = SymbolIndex()
    writer = _writer(index)

    with pytest.raises(RuntimeError):
        _write(writer)

    assert writer._bulk.is_empty
    assert writer._bulk._contains == []
    assert _function_entries(index) == []


def test_retry_after_failed_write_matches_a_single_write(fail_first_import_rows):
    retried_index = SymbolIndex()
    retried = _writer(retried_index)
    with pytest.raises(RuntimeError):
        _write(retried)
    _write(retried)  # the pipeline's retry

    clean_index = SymbolIndex()
    clean = _writer(clean_index)
    _write(clean)

    assert retried._bulk._nodes == clean._bulk._nodes
    assert retried._bulk._contains == clean._bulk._contains
    assert retried._bulk._has_parameter == clean._bulk._has_parameter
    assert len(retried._bulk._deferred) == len(clean._bulk._deferred)
    assert len(_function_entries(retried_index)) == len(_function_entries(clean_index)) == 1
