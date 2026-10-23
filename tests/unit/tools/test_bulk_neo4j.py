from types import SimpleNamespace

from codegraphcontext.tools.indexing.persistence.bulk_neo4j import (
    BATCH_SIZE,
    NAME_ONLY_LABELS,
    Neo4jBulkCollector,
    merge_keys,
)
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter, _NAME_ONLY_MERGE_LABELS

A = "/repo/a.py"


class FakeSession:
    """Records (query, batch) for every run; batched calls carry a list."""

    def __init__(self):
        self.calls = []

    def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return SimpleNamespace()

    def queries_containing(self, text):
        return [(q, p) for q, p in self.calls if text in q]


def _function(name, line, occurrence=0, **extra):
    return {"name": name, "path": A, "line_number": line, "occurrence_index": occurrence, **extra}


def test_name_only_labels_match_writer():
    assert NAME_ONLY_LABELS == set(_NAME_ONLY_MERGE_LABELS)


def test_merge_keys_by_label_family():
    assert merge_keys("Module") == ("name",)
    assert merge_keys("Parameter") == ("name", "path", "function_line_number")
    assert merge_keys("Function") == ("name", "path", "line_number", "occurrence_index")


def test_flush_merges_nodes_without_uid_or_nulls():
    c = Neo4jBulkCollector()
    c.add_nodes("Function", [_function("f", 3, docstring=None)], A)
    s = FakeSession()
    c.flush(s)
    merges = s.queries_containing("MERGE (n:`Function`")
    assert len(merges) == 1
    row = merges[0][1]["batch"][0]
    assert "uid" not in row
    assert "docstring" not in row
    assert row["name"] == "f" and row["line_number"] == 3 and row["occurrence_index"] == 0


def test_flush_batches_large_label():
    c = Neo4jBulkCollector()
    rows = [_function(f"f{i}", i + 1) for i in range(BATCH_SIZE + 5)]
    c.add_nodes("Function", rows, A)
    s = FakeSession()
    c.flush(s)
    merges = s.queries_containing("MERGE (n:`Function`")
    assert [len(p["batch"]) for _, p in merges] == [BATCH_SIZE, 5]


def test_contains_uses_node_identity_not_uid():
    c = Neo4jBulkCollector()
    c.add_nodes("Function", [_function("f", 3)], A)
    c.add_nodes("Module", [{"name": "os"}], A)
    s = FakeSession()
    c.flush(s)
    contains = s.queries_containing("MERGE (f)-[:CONTAINS]->(n)")
    by_label = {}
    for q, p in contains:
        by_label[q] = p["batch"]
    rows = sorted((r for b in by_label.values() for r in b), key=lambda r: r["file_path"])
    assert {"file_path": A, "name": "f", "path": A, "line_number": 3, "occurrence_index": 0} in rows
    assert {"file_path": A, "name": "os"} in rows


def test_flush_order_nodes_then_contains_then_parameters_then_imports():
    c = Neo4jBulkCollector()
    c.add_nodes("Function", [_function("f", 3)], A)
    c.add_parameters(A, [{"func_name": "f", "line_number": 3, "occurrence_index": 0, "arg_name": "x"}])
    c.add_imports(A, [], [{"name": "os", "line_number": 1, "imported_name": "os", "alias": None,
                           "full_import_name": "os", "lang": "python"}])
    s = FakeSession()
    c.flush(s)
    order = []
    for q, _ in s.calls:
        if "MERGE (n:" in q:
            order.append("nodes")
        elif "CONTAINS" in q:
            order.append("contains")
        elif "HAS_PARAMETER" in q:
            order.append("has_parameter")
        elif "IMPORTS" in q:
            order.append("imports")
    # Parameter nodes are merged with the nodes; each stage appears once, in this order.
    assert order.index("contains") > order.index("nodes")
    assert order.index("has_parameter") > order.index("contains")
    assert order.index("imports") > order.index("has_parameter")


def test_has_parameter_links_identity_pairs():
    c = Neo4jBulkCollector()
    c.add_nodes("Function", [_function("f", 3)], A)
    c.add_parameters(A, [{"func_name": "f", "line_number": 3, "occurrence_index": 0, "arg_name": "x"}])
    s = FakeSession()
    c.flush(s)
    (_, params), = s.queries_containing("MERGE (fn)-[:HAS_PARAMETER]->(p)")
    assert params["batch"] == [{
        "fn_name": "f", "fn_path": A, "fn_line": 3, "fn_occurrence": 0,
        "p_name": "x", "p_path": A, "p_line": 3,
    }]


def test_parameter_nodes_carry_only_merge_properties():
    c = Neo4jBulkCollector()
    c.add_parameters(A, [{"func_name": "f", "line_number": 3, "occurrence_index": 0, "arg_name": "x"}])
    s = FakeSession()
    c.flush(s)
    (_, params), = s.queries_containing("MERGE (n:`Parameter`")
    assert params["batch"] == [{"name": "x", "path": A, "function_line_number": 3}]


def test_imports_use_js_and_other_patterns():
    c = Neo4jBulkCollector()
    c.add_imports(
        A,
        [{"module_name": "react", "line_number": 1, "imported_name": "React", "alias": None, "lang": "js"}],
        [{"name": "os", "line_number": 2, "imported_name": "os", "alias": None,
          "full_import_name": "os", "lang": "python"}],
    )
    s = FakeSession()
    c.flush(s)
    (js_q, js), = s.queries_containing("MERGE (f)-[r:IMPORTS {line_number: row.line_number}]")
    (oth_q, oth), = s.queries_containing("imported_name: row.imported_name}]->(m)")
    assert js["batch"][0]["module_name"] == "react"
    assert "MATCH (m:Module {name: row.module_name})" in oth_q
    assert oth["batch"][0]["module_name"] == "os"
    assert oth["batch"][0]["alias"] == ""  # BulkCollector stores `alias or ""` for these rows


def test_deferred_work_runs_last_with_session():
    c = Neo4jBulkCollector()
    c.add_nodes("Function", [_function("f", 3)], A)
    seen = []
    c.defer(lambda s: seen.append(s))
    s = FakeSession()
    c.flush(s)
    assert seen == [s]


def test_enable_bulk_selects_neo4j_collector():
    db_manager = SimpleNamespace(get_backend_type=lambda: "neo4j")
    writer = GraphWriter(driver=SimpleNamespace(), db_manager=db_manager)
    assert writer.enable_bulk() is True
    assert isinstance(writer._bulk, Neo4jBulkCollector)


def test_enable_bulk_rejects_unknown_backend():
    db_manager = SimpleNamespace(get_backend_type=lambda: "falkordb")
    writer = GraphWriter(driver=SimpleNamespace(), db_manager=db_manager)
    assert writer.enable_bulk() is False
    assert writer.bulk_enabled is False
