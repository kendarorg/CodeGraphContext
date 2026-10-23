"""Phase 5 (inheritance re-resolution) reading from the repo-init SymbolIndex."""

from unittest.mock import MagicMock

from codegraphcontext.tools.indexing.resolution import post_resolution
from codegraphcontext.tools.indexing.symbol_index import (
    SymbolIndex,
    add_call_edges,
    add_contains_edges,
    add_inherit_edges,
)

REPO = "/repo"


def _call_row(**overrides):
    row = {
        "caller_name": "main",
        "caller_file_path": "/repo/app.ts",
        "caller_line_number": 10,
        "called_name": "push",
        "called_file_path": "/repo/app.ts",
        "called_line_number": 0,
        "called_context": "",
        "line_number": 12,
        "full_call_name": "stack.push",
        "args": ["x"],
        "args_key": '["x"]',
        "confidence": 0.4,
        "resolution_tier": 8,
        "confidence_label": "HEURISTIC",
    }
    row.update(overrides)
    return row


def _graph():
    """Two classes in two files; Base is a parent of Stack, Stack.push is the candidate."""
    index = SymbolIndex()
    index.register("File", "app.ts", "/repo/app.ts", key="/repo/app.ts")
    index.register("Function", "main", "/repo/app.ts", 10, key="main10")
    index.register("Function", "push", "/repo/app.ts", 3, key="push3")
    index.register("Class", "Stack", "/repo/app.ts", 2, key="Stack2")
    index.register("Class", "Base", "/repo/base.ts", 1, key="Base1")
    index.register("File", "base.ts", "/repo/base.ts", key="/repo/base.ts")
    index.register("Function", "push", "/repo/base.ts", 4, key="push4")
    index.register("Class", "Other", "/repo/other.ts", 1, key="Other1")
    index.register("Function", "push", "/repo/other.ts", 5, key="push5")
    return index


def test_low_confidence_rows_are_tier_8_9_within_repo_prefix():
    index = _graph()
    add_call_edges(index, [_call_row(resolution_tier=8)], "Function", "Function")
    add_call_edges(index, [_call_row(resolution_tier=9, called_line_number=0, line_number=20)], "Function", "Function")
    add_call_edges(index, [_call_row(resolution_tier=3, line_number=30)], "Function", "Function")
    rows = index.low_confidence_call_rows("/repo/")
    assert sorted(r["call_line"] for r in rows) == [12, 20]
    row = next(r for r in rows if r["call_line"] == 12)
    assert row == {
        "caller_name": "main",
        "caller_path": "/repo/app.ts",
        "caller_line": 10,
        "called_name": "push",
        "call_line": 12,
        "full_call_name": "stack.push",
        "args": ["x"],
    }


def test_low_confidence_rows_respect_repo_prefix():
    index = _graph()
    add_call_edges(index, [_call_row()], "Function", "Function")
    assert index.low_confidence_call_rows("/elsewhere/") == []


def test_call_edges_use_relationship_type_from_tier():
    index = _graph()
    add_call_edges(index, [_call_row(resolution_tier=8)], "Function", "Function")
    add_call_edges(index, [_call_row(resolution_tier=5, line_number=99)], "Function", "Function")
    assert [e.line_number for e in index.edges("HEURISTIC_CALLS")] == [12]
    assert [e.line_number for e in index.edges("CALLS")] == [99]


def test_call_edge_context_filter_matches_function_scope():
    index = SymbolIndex()
    index.register("Function", "caller", "/repo/a.ts", 1)
    index.register("Function", "run", "/repo/a.ts", 5, class_context="Stack", key="run5")
    index.register("Function", "run", "/repo/a.ts", 9, class_context="Queue", key="run9")
    add_call_edges(
        index,
        [_call_row(caller_name="caller", caller_file_path="/repo/a.ts", caller_line_number=1,
                   called_name="run", called_file_path="/repo/a.ts", called_context="Queue")],
        "Function", "Function",
    )
    assert [e.target.line_number for e in index.edges("HEURISTIC_CALLS")] == [9]


def test_call_edge_skipped_when_caller_line_missing():
    index = _graph()
    add_call_edges(index, [_call_row(caller_line_number=None)], "Function", "Function")
    assert index.edges("HEURISTIC_CALLS") == []


def test_repeated_call_identity_keeps_last_properties():
    index = _graph()
    add_call_edges(index, [_call_row(confidence=0.4)], "Function", "Function")
    add_call_edges(index, [_call_row(confidence=0.9)], "Function", "Function")
    edges = index.edges("HEURISTIC_CALLS")
    assert len(edges) == 1
    assert edges[0].confidence == 0.9


def test_file_caller_matches_by_path():
    index = _graph()
    add_call_edges(
        index,
        [_call_row(caller_name="app.ts", caller_line_number=None)],
        "File", "Function",
    )
    edges = index.edges("HEURISTIC_CALLS")
    assert len(edges) == 1
    assert edges[0].source.label == "File"
    assert edges[0].target.key == "push3"


def test_candidate_rows_one_per_parent_and_none_without_parent():
    index = _graph()
    add_contains_edges(
        index,
        [
            {"class_name": "Stack", "class_line": -1, "func_name": "push", "func_line": 3},
        ],
        "/repo/app.ts",
    )
    add_inherit_edges(index, [
        {"child_name": "Stack", "path": "/repo/app.ts", "parent_name": "Base",
         "resolved_parent_file_path": "/repo/base.ts"},
    ])
    rows = index.class_method_candidate_rows("/repo/", ["push"])
    assert len(rows) == 1
    assert rows[0]["class_name"] == "Stack"
    assert rows[0]["parent_name"] == "Base"
    assert rows[0]["line_number"] == 3

    # A class without a parent yields one row with a null parent.
    index.register("Class", "Solo", "/repo/app.ts", 40, key="Solo40")
    index.register("Function", "push", "/repo/app.ts", 41, key="push41")
    add_contains_edges(index, [{"class_name": "Solo", "class_line": 40, "func_name": "push", "func_line": 41}], "/repo/app.ts")
    solo = [r for r in index.class_method_candidate_rows("/repo/", ["push"]) if r["class_name"] == "Solo"]
    assert solo == [{
        "queried_name": "push", "path": "/repo/app.ts", "line_number": 41,
        "class_name": "Solo", "class_qname": None, "parent_name": None, "parent_qname": None,
    }]


def test_contains_requires_matching_class_line():
    index = _graph()
    add_contains_edges(index, [{"class_name": "Stack", "class_line": 99, "func_name": "push", "func_line": 3}], "/repo/app.ts")
    assert index.edges("CONTAINS") == []


def test_inherit_edges_skip_external_and_non_class_parents():
    index = _graph()
    add_inherit_edges(index, [
        {"child_name": "Stack", "path": "/repo/app.ts", "parent_name": "Ext",
         "resolved_parent_file_path": "__external__"},
        {"child_name": "Stack", "path": "/repo/app.ts", "parent_name": "Base",
         "resolved_parent_file_path": "/repo/base.ts", "parent_label": "Interface"},
    ])
    assert index.edges("INHERITS") == []


def _fake_driver():
    driver = MagicMock()
    driver.get_backend_type = lambda: "neo4j"
    return driver


def _fail(*args, **kwargs):
    raise AssertionError("unexpected database access")


def test_reresolve_from_index_writes_single_candidate(monkeypatch):
    index = _graph()
    # Only Base.push is a class method outside app.ts, so it is the one candidate.
    add_contains_edges(index, [{"class_name": "Base", "class_line": 1, "func_name": "push", "func_line": 4}], "/repo/base.ts")
    add_call_edges(index, [_call_row()], "Function", "Function")

    writes = []

    def fake_write(driver, backend, work):
        session = MagicMock()
        session.run.side_effect = lambda query, **kw: writes.append(kw.get("batch"))
        return work(session)

    monkeypatch.setattr(post_resolution, "execute_write_operation", fake_write)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)

    improved = post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index)
    assert improved == 1
    (batch,) = writes
    assert batch == [{
        "caller_path": "/repo/app.ts",
        "caller_name": "main",
        "caller_line": 10,
        "called_name": "push",
        "call_line": 12,
        "new_called_path": "/repo/base.ts",
        "new_called_line": 4,
        "confidence": 0.78,
        "resolution_tier": 10,
    }]


def test_reresolve_from_index_returns_zero_without_low_confidence_edges(monkeypatch):
    index = _graph()
    monkeypatch.setattr(post_resolution, "execute_write_operation", _fail)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)
    assert post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index) == 0


def test_standard_path_still_queries_database(monkeypatch):
    seen = []

    def fake_read(driver, backend, work):
        seen.append(backend)
        return []

    monkeypatch.setattr(post_resolution, "execute_read_operation", fake_read)
    assert post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None) == 0
    assert seen == ["neo4j"]
