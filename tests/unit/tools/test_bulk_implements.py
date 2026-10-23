from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_implements_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _index():
    index = SymbolIndex()
    index.register("Struct", "Impl", "/repo/a.go", 3, key="impl-key")
    index.register("Interface", "Iface", "/repo/b.go", 1, key="iface-key")
    return index


def _row(**overrides):
    row = {
        "child_name": "Impl",
        "path": "/repo/a.go",
        "parent_name": "Iface",
        "resolved_parent_file_path": "/repo/b.go",
        "confidence_label": None,
    }
    row.update(overrides)
    return row


def test_implements_edge_uses_default_labels():
    edges = build_implements_edges(_index(), [_row()])
    assert [(e["from"], e["to"], e["from_label"], e["to_label"]) for e in edges] == [
        ("impl-key", "iface-key", "Struct", "Interface")
    ]


def test_missing_confidence_label_defaults_to_inferred():
    assert build_implements_edges(_index(), [_row()])[0]["confidence_label"] == "INFERRED"


def test_row_labels_override_the_defaults():
    index = _index()
    index.register("Class", "Impl", "/repo/a.go", 9, key="class-key")
    edges = build_implements_edges(index, [_row(child_label="Class", parent_label="Interface")])
    assert [(e["from"], e["from_label"]) for e in edges] == [("class-key", "Class")]


def test_unknown_parent_writes_nothing():
    assert build_implements_edges(_index(), [_row(resolved_parent_file_path="/repo/x.go")]) == []
