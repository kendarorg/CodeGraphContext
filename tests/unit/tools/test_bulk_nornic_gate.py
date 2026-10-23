from types import SimpleNamespace

from codegraphcontext.tools.indexing.persistence.bulk_neo4j import Neo4jBulkCollector
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _writer(backend, symbol_index=None):
    db_manager = SimpleNamespace(get_backend_type=lambda: backend)
    return GraphWriter(driver=SimpleNamespace(), db_manager=db_manager, symbol_index=symbol_index)


def test_enable_bulk_selects_neo4j_collector_for_nornic():
    writer = _writer("nornic")
    assert writer.enable_bulk() is True
    assert isinstance(writer._bulk, Neo4jBulkCollector)


def test_enable_bulk_still_selects_neo4j_collector_for_neo4j():
    writer = _writer("neo4j")
    assert writer.enable_bulk() is True
    assert isinstance(writer._bulk, Neo4jBulkCollector)


def test_neo4j_bulk_writes_on_for_nornic_with_symbol_index():
    writer = _writer("nornic", symbol_index=SymbolIndex())
    assert writer._use_neo4j_bulk("nornic") is True


def test_neo4j_bulk_writes_off_for_nornic_without_symbol_index():
    writer = _writer("nornic")
    assert writer._use_neo4j_bulk("nornic") is False


def test_neo4j_bulk_writes_off_for_falkordb_with_symbol_index():
    writer = _writer("falkordb", symbol_index=SymbolIndex())
    assert writer._use_neo4j_bulk("falkordb") is False
