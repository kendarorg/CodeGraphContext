from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_metaclass_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _index():
    index = SymbolIndex()
    index.register("Class", "Child", "/repo/a.py", 3, key="child-key")
    index.register("Class", "Meta", "/repo/b.py", 1, key="meta-key")
    return index


def _row(**overrides):
    row = {
        "child_name": "Child",
        "path": "/repo/a.py",
        "parent_name": "Meta",
        "resolved_parent_file_path": "/repo/b.py",
        "line_number": 3,
        "confidence_label": None,
    }
    row.update(overrides)
    return row


def test_metaclass_edge_resolves_both_classes():
    edges = build_metaclass_edges(_index(), [_row()])
    assert [(e["from"], e["to"], e["from_label"], e["to_label"]) for e in edges] == [
        ("child-key", "meta-key", "Class", "Class")
    ]


def test_missing_confidence_label_defaults_to_extracted():
    edges = build_metaclass_edges(_index(), [_row()])
    assert edges[0]["confidence_label"] == "EXTRACTED"


def test_repeated_pair_keeps_the_last_row_properties():
    rows = [_row(line_number=3, confidence_label="INFERRED"), _row(line_number=5, confidence_label="EXTRACTED")]
    edges = build_metaclass_edges(_index(), rows)
    assert len(edges) == 1
    assert edges[0]["line_number"] == 5
    assert edges[0]["confidence_label"] == "EXTRACTED"


def test_unknown_parent_writes_nothing():
    assert build_metaclass_edges(_index(), [_row(resolved_parent_file_path="/repo/x.py")]) == []
