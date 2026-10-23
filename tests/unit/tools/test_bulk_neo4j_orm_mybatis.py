from types import SimpleNamespace

from codegraphcontext.tools.indexing.persistence import bulk_neo4j
from codegraphcontext.tools.indexing.persistence.bulk_neo4j import (
    BATCH_SIZE,
    write_function_table_edges,
    write_maps_to,
)
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex

A = "/repo/User.java"


class FakeSession:
    """Records (query, batch) for every run. execute_write runs the work function at once,
    as the Neo4j driver does inside a managed transaction."""

    def __init__(self):
        self.calls = []

    def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return SimpleNamespace()

    def execute_write(self, work_fn):
        return work_fn(self)

    def queries_containing(self, text):
        return [(q, p) for q, p in self.calls if text in q]


class FakeDriver:
    def __init__(self):
        self.session_obj = FakeSession()

    def session(self):
        return _Ctx(self.session_obj)


class _Ctx:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


def _class_table(cls, table, datastore="mysql", line=7, path=A):
    return {"kind": "class_table", "class_name": cls, "class_path": path,
            "orm_table": table, "datastore": datastore, "line_number": line}


def _query(method, table, operation="READS", line=12, path=A):
    return {"kind": "method_query", "method_name": method, "method_path": path,
            "class_name": "UserRepository", "db_tables": [table], "operation": operation,
            "line_number": line}


def _edge(method, table, operation, line=None, path=A):
    return {"method_name": method, "method_path": path, "table_name": table,
            "operation": operation, "line_number": line}


def _writer(session_driver, symbol_index=None, backend="neo4j"):
    db_manager = SimpleNamespace(get_backend_type=lambda: backend)
    return GraphWriter(driver=session_driver, db_manager=db_manager, symbol_index=symbol_index)


def test_maps_to_projects_class_table_rows_and_keeps_order():
    s = FakeSession()
    write_maps_to(s, [
        _class_table("User", "users", "mysql", 7),
        {"kind": "method_query", "method_name": "x"},
        _class_table("User", "audit", "cassandra", 9),
    ])
    (query, params), = s.calls
    assert "UNWIND $batch AS m" in query
    assert "MERGE (tbl:DbTable {fqn: m.orm_table})" in query
    assert "MERGE (c)-[:MAPS_TO {datastore: m.datastore, line_number: m.line_number}]->(tbl)" in query
    assert params["batch"] == [
        {"class_name": "User", "class_path": A, "orm_table": "users", "datastore": "mysql", "line_number": 7},
        {"class_name": "User", "class_path": A, "orm_table": "audit", "datastore": "cassandra", "line_number": 9},
    ]


def test_maps_to_batches_large_input():
    s = FakeSession()
    write_maps_to(s, [_class_table(f"C{i}", f"t{i}") for i in range(BATCH_SIZE + 3)])
    assert [len(p["batch"]) for _, p in s.calls] == [BATCH_SIZE, 3]


def test_query_edges_keep_line_number_in_merge_as_standard():
    s = FakeSession()
    write_function_table_edges(
        s,
        [_edge("find", "users", "READS", 12), _edge("save", "users", "WRITES", 20)],
        with_line_number=True,
    )
    reads = s.queries_containing("[:READS {line_number: q.line_number}]")
    writes = s.queries_containing("[:WRITES {line_number: q.line_number}]")
    assert len(reads) == 1 and len(writes) == 1
    assert reads[0][1]["batch"] == [{"method_name": "find", "method_path": A, "table_name": "users", "line_number": 12}]
    assert writes[0][1]["batch"] == [{"method_name": "save", "method_path": A, "table_name": "users", "line_number": 20}]
    assert "datasource_name = COALESCE(tbl.datasource_name, 'mysql')" in reads[0][0]


def test_mybatis_edges_merge_without_line_number_as_standard():
    s = FakeSession()
    write_function_table_edges(
        s,
        [_edge("find", "users", "READS"), _edge("save", "users", "WRITES")],
        with_line_number=False,
    )
    assert len(s.calls) == 2
    for query, params in s.calls:
        assert "line_number" not in query
        assert all("line_number" not in row for row in params["batch"])
    assert s.queries_containing("MERGE (fn)-[:READS]->(tbl)")
    assert s.queries_containing("MERGE (fn)-[:WRITES]->(tbl)")


def test_skips_operations_other_than_reads_and_writes():
    s = FakeSession()
    write_function_table_edges(s, [_edge("find", "users", "CALLS", 3)], with_line_number=True)
    assert s.calls == []


def test_writer_orm_mappings_use_unwind_on_neo4j_repo_init():
    driver = FakeDriver()
    writer = _writer(driver, symbol_index=SymbolIndex())
    writer.write_orm_mappings([_class_table("User", "users")])
    queries = driver.session_obj.queries_containing("MAPS_TO {datastore: m.datastore")
    assert len(queries) == 1
    assert queries[0][1]["batch"][0]["orm_table"] == "users"


def test_writer_query_links_use_unwind_on_neo4j_repo_init():
    driver = FakeDriver()
    writer = _writer(driver, symbol_index=SymbolIndex())
    writer.write_query_links([_query("find", "users", "READS", 12), _query("save", "users", "WRITES", 20)])
    assert len(driver.session_obj.queries_containing("[:READS {line_number: q.line_number}]")) == 1
    assert len(driver.session_obj.queries_containing("[:WRITES {line_number: q.line_number}]")) == 1


def test_writer_mybatis_links_use_unwind_on_neo4j_repo_init():
    driver = FakeDriver()
    writer = _writer(driver, symbol_index=SymbolIndex())
    writer.write_mybatis_links([{
        "method_name": "find", "method_path": A, "db_tables": ["users"], "operation": "READS",
    }])
    (query, params), = driver.session_obj.queries_containing("MERGE (fn)-[:READS]->(tbl)")
    assert params["batch"] == [{"method_name": "find", "method_path": A, "table_name": "users"}]


def test_writer_mybatis_skips_unresolved_operations():
    driver = FakeDriver()
    writer = _writer(driver, symbol_index=SymbolIndex())
    writer.write_mybatis_links([{"method_name": "find", "method_path": None,
                                 "db_tables": ["users"], "operation": "READS"}])
    assert driver.session_obj.calls == []


def test_neo4j_gate_needs_repo_init_index():
    driver = FakeDriver()
    writer = _writer(driver, symbol_index=None)
    assert writer._use_neo4j_bulk("neo4j") is False
    writer = _writer(driver, symbol_index=SymbolIndex())
    assert writer._use_neo4j_bulk("neo4j") is True
    assert writer._use_neo4j_bulk("kuzudb") is False


def test_module_exposes_writers_used_by_graph_writer():
    assert callable(bulk_neo4j.write_maps_to)
    assert callable(bulk_neo4j.write_function_table_edges)
