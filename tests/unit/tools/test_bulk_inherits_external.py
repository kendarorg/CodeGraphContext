from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_external_inherit_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _index():
    index = SymbolIndex()
    index.register("Class", "Child", "/repo/a.py", 3, key="child-key")
    index.register("Class", "Other", "/repo/a.py", 9, key="other-key")
    return index


def _row(**overrides):
    row = {
        "child_name": "Child",
        "path": "/repo/a.py",
        "parent_name": "ParentClass",
        "resolved_parent_file_path": "__external__",
        "confidence_label": None,
    }
    row.update(overrides)
    return row


def test_external_edge_points_at_parent_name_key():
    edges, names = build_external_inherit_edges(_index(), [_row()], "Class")
    assert [(e["from"], e["to"]) for e in edges] == [("child-key", "ParentClass")]
    assert names == {"ParentClass"}


def test_missing_confidence_label_defaults_to_inferred():
    edges, _ = build_external_inherit_edges(_index(), [_row()], "Class")
    assert edges[0]["confidence_label"] == "INFERRED"


def test_repeated_pair_keeps_the_last_confidence_label():
    rows = [_row(confidence_label="EXTRACTED"), _row(confidence_label="INFERRED")]
    edges, _ = build_external_inherit_edges(_index(), rows, "Class")
    assert len(edges) == 1
    assert edges[0]["confidence_label"] == "INFERRED"


def test_row_without_child_creates_no_parent_and_no_edge():
    edges, names = build_external_inherit_edges(_index(), [_row(child_name="Missing")], "Class")
    assert edges == []
    assert names == set()


def test_child_label_must_match_the_index_entry():
    edges, names = build_external_inherit_edges(_index(), [_row()], "Interface")
    assert edges == []
    assert names == set()


def test_each_child_with_the_same_name_gets_an_edge():
    index = _index()
    index.register("Class", "Child", "/repo/a.py", 20, key="child-key-2")
    edges, names = build_external_inherit_edges(index, [_row()], "Class")
    assert sorted(e["from"] for e in edges) == ["child-key", "child-key-2"]
    assert names == {"ParentClass"}
