from unittest.mock import AsyncMock, MagicMock

import pytest

from codegraphcontext.tools.indexing import repo_init
from codegraphcontext.tools.indexing.persistence.bulk_kuzu import BulkCollector
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.repo_init import RepoAlreadyIndexedError, initialize_repository
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


@pytest.fixture
def allowed_root(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.delenv("CGC_ALLOW_ALL_PATHS", raising=False)
    monkeypatch.setenv("CGC_ALLOWED_ROOTS", str(root))
    return root


def _builder(has_repository=False, backend="kuzudb"):
    session = MagicMock()
    session.run.return_value.single.return_value = {"n": 1 if has_repository else 0}
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    builder = MagicMock()
    builder.db_manager.get_driver.return_value = driver
    builder.db_manager.get_backend_type.return_value = backend
    builder.parsers = {"python": "x"}
    return builder, session


@pytest.mark.asyncio
async def test_non_directory_path_raises_value_error(allowed_root):
    file_path = allowed_root / "a.py"
    file_path.write_text("x = 1\n", encoding="utf-8")
    builder, _ = _builder()

    with pytest.raises(ValueError, match="Not a directory"):
        await initialize_repository(builder, file_path)
    builder.db_manager.get_driver.assert_not_called()


@pytest.mark.asyncio
async def test_path_outside_allowed_roots_raises_value_error(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.delenv("CGC_ALLOW_ALL_PATHS", raising=False)
    monkeypatch.setenv("CGC_ALLOWED_ROOTS", str(root))
    builder, _ = _builder()

    with pytest.raises(ValueError, match="outside the allowed roots"):
        await initialize_repository(builder, outside)
    builder.db_manager.get_driver.assert_not_called()


@pytest.mark.asyncio
async def test_existing_repository_node_raises_already_indexed(allowed_root, monkeypatch):
    pipeline = AsyncMock()
    monkeypatch.setattr(repo_init, "run_tree_sitter_index_async", pipeline)
    builder, session = _builder(has_repository=True)

    with pytest.raises(RepoAlreadyIndexedError, match="cgc index"):
        await initialize_repository(builder, allowed_root)

    query = session.run.call_args.args[0]
    assert "MATCH (r:Repository" in query
    assert session.run.call_args.kwargs == {"path": allowed_root.resolve().as_posix()}
    pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_happy_path_runs_pipeline_with_bulk_enabled_writer(allowed_root, monkeypatch):
    pipeline = AsyncMock()
    monkeypatch.setattr(repo_init, "run_tree_sitter_index_async", pipeline)
    builder, _ = _builder(has_repository=False)

    result = await initialize_repository(builder, allowed_root, cgcignore_path=None, is_dependency=True, job_id="job-1")

    pipeline.assert_awaited_once()
    args = pipeline.await_args.args
    assert args[0] == allowed_root.resolve()
    assert args[1] is True          # is_dependency
    assert args[2] == "job-1"       # job_id
    assert args[3] is None          # cgcignore_path

    writer = args[4]
    assert isinstance(writer, GraphWriter)
    assert isinstance(writer._bulk, BulkCollector)
    assert isinstance(writer._symbol_index, SymbolIndex)

    assert args[5] is builder.job_manager
    assert args[6] is builder.parsers
    assert result == {"path": allowed_root.resolve().as_posix(), "symbols": 0, "summary": {}}
