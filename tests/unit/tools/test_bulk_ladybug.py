from types import SimpleNamespace

from codegraphcontext.tools.indexing.persistence.bulk_kuzu import BulkCollector
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _writer(backend, symbol_index=None):
    db_manager = SimpleNamespace(get_backend_type=lambda: backend)
    return GraphWriter(driver=SimpleNamespace(), db_manager=db_manager, symbol_index=symbol_index)


def test_enable_bulk_selects_kuzu_collector_for_ladybugdb():
    writer = _writer("ladybugdb")
    assert writer.enable_bulk() is True
    assert isinstance(writer._bulk, BulkCollector)


def test_enable_bulk_still_selects_kuzu_collector_for_kuzudb():
    writer = _writer("kuzudb")
    assert writer.enable_bulk() is True
    assert isinstance(writer._bulk, BulkCollector)


def test_enable_bulk_rejects_falkordb():
    writer = _writer("falkordb")
    assert writer.enable_bulk() is False
    assert writer.bulk_enabled is False


def test_index_writes_on_for_ladybugdb_with_symbol_index():
    writer = _writer("ladybugdb", symbol_index=SymbolIndex())
    assert writer._use_index_writes("ladybugdb") is True


def test_index_writes_on_for_kuzudb_with_symbol_index():
    writer = _writer("kuzudb", symbol_index=SymbolIndex())
    assert writer._use_index_writes("kuzudb") is True


def test_index_writes_off_for_ladybugdb_without_symbol_index():
    writer = _writer("ladybugdb")
    assert writer._use_index_writes("ladybugdb") is False


def test_index_writes_off_for_other_backends_with_symbol_index():
    writer = _writer("neo4j", symbol_index=SymbolIndex())
    assert writer._use_index_writes("neo4j") is False
    assert writer._use_index_writes("falkordb") is False
