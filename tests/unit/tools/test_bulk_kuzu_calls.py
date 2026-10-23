from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_call_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _call_row(**overrides):
    row = {
        "caller_name": "main",
        "caller_file_path": "/repo/a.py",
        "caller_line_number": 1,
        "called_name": "run",
        "called_file_path": "/repo/b.py",
        "called_line_number": 0,
        "called_context": "",
        "line_number": 3,
        "full_call_name": "run",
        "args_key": "[]",
        "args": [],
        "confidence": 1.0,
        "resolution_tier": 1,
        "confidence_label": "EXTRACTED",
    }
    row.update(overrides)
    return row


def _index():
    index = SymbolIndex()
    index.register("Function", "main", "/repo/a.py", 1, key="main/repo/a.py10")
    index.register("Function", "run", "/repo/b.py", 5, key="run/repo/b.py50")
    return index


def test_one_row_becomes_one_edge_between_resolved_keys():
    edges = build_call_edges(_index(), [_call_row()])
    assert [(e["from"], e["to"]) for e in edges] == [("main/repo/a.py10", "run/repo/b.py50")]


def test_callee_line_filter_applies_only_when_positive():
    index = _index()
    assert build_call_edges(index, [_call_row(called_line_number=6)]) == []
    assert len(build_call_edges(index, [_call_row(called_line_number=5)])) == 1


def test_context_matches_any_of_context_class_context_or_module_context():
    index = SymbolIndex()
    index.register("Function", "main", "/repo/a.py", 1, key="k-main")
    index.register("Function", "run", "/repo/b.py", 5, key="k-run", class_context="Service")
    edges = build_call_edges(index, [_call_row(called_context="Service")])
    assert [e["to"] for e in edges] == ["k-run"]
    assert build_call_edges(index, [_call_row(called_context="Other")]) == []


def test_missing_caller_line_matches_nothing_like_a_null_comparison():
    assert build_call_edges(_index(), [_call_row(caller_line_number=None)]) == []


def test_same_edge_key_is_merged_with_the_last_row_winning():
    first = _call_row(confidence=0.4)
    last = _call_row(confidence=0.9)
    edges = build_call_edges(_index(), [first, last])
    assert len(edges) == 1
    assert edges[0]["confidence"] == 0.9


def test_distinct_call_sites_stay_separate_edges():
    rows = [_call_row(line_number=3), _call_row(line_number=7)]
    edges = build_call_edges(_index(), rows)
    assert sorted(e["line_number"] for e in edges) == [3, 7]


def test_file_caller_matches_by_path_without_line():
    index = _index()
    index.register("File", "a.py", "/repo/a.py", key="/repo/a.py")
    index.register("Class", "Service", "/repo/b.py", 5, key="cls-service")
    rows = [_call_row(called_name="Service", called_line_number=5)]
    edges = build_call_edges(index, rows, caller_label="File", called_label="Class")
    assert [(e["from"], e["to"]) for e in edges] == [("/repo/a.py", "cls-service")]


def test_file_callee_has_no_line_or_context_filter():
    index = SymbolIndex()
    index.register("Function", "main", "/repo/a.py", 1, key="k-main")
    index.register("File", "b.py", "/repo/b.py", key="/repo/b.py")
    rows = [_call_row(called_name="b.py", called_file_path="/repo/b.py", called_line_number=99, called_context="x")]
    edges = build_call_edges(index, rows, caller_label="Function", called_label="File")
    assert [e["to"] for e in edges] == ["/repo/b.py"]
