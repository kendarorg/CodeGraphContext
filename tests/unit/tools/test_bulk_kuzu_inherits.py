from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_inherit_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _index():
    index = SymbolIndex()
    index.register("Class", "Child", "/repo/a.py", 3, key="child-key")
    index.register("Class", "Base", "/repo/b.py", 1, key="base-key")
    return index


def _row(**overrides):
    row = {
        "child_name": "Child",
        "path": "/repo/a.py",
        "parent_name": "Base",
        "resolved_parent_file_path": "/repo/b.py",
        "confidence_label": None,
    }
    row.update(overrides)
    return row


def test_inherits_edge_resolves_child_and_parent_by_name_and_path():
    edges = build_inherit_edges(_index(), [_row()], "Class", "Class")
    assert [(e["from"], e["to"]) for e in edges] == [("child-key", "base-key")]


def test_missing_confidence_label_defaults_to_extracted():
    edges = build_inherit_edges(_index(), [_row()], "Class", "Class")
    assert edges[0]["confidence_label"] == "EXTRACTED"


def test_repeated_pair_keeps_the_last_confidence_label():
    rows = [_row(confidence_label="INFERRED"), _row(confidence_label="EXTRACTED")]
    edges = build_inherit_edges(_index(), rows, "Class", "Class")
    assert len(edges) == 1
    assert edges[0]["confidence_label"] == "EXTRACTED"


def test_unresolved_parent_writes_nothing():
    assert build_inherit_edges(_index(), [_row(resolved_parent_file_path="/repo/x.py")], "Class", "Class") == []
