from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_companion_of_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_companion_matches_object_and_owner_by_line():
    index = SymbolIndex()
    index.register("Object", "Companion", "/repo/a.kt", 4, key="companion")
    index.register("Class", "Owner", "/repo/a.kt", 1, key="owner")
    index.register("Class", "Owner", "/repo/a.kt", 9, key="other-owner")
    row = {"companion_name": "Companion", "companion_path": "/repo/a.kt", "companion_line": 4,
           "owner_name": "Owner", "owner_path": "/repo/a.kt", "owner_line": 1}
    edges = build_companion_of_edges(index, [row])
    assert [(e["from"], e["to"], e["from_label"], e["to_label"]) for e in edges] == [
        ("companion", "owner", "Object", "Class")
    ]


def test_companion_with_no_owner_at_that_line_writes_nothing():
    index = SymbolIndex()
    index.register("Object", "Companion", "/repo/a.kt", 4, key="companion")
    index.register("Class", "Owner", "/repo/a.kt", 9, key="other-owner")
    row = {"companion_name": "Companion", "companion_path": "/repo/a.kt", "companion_line": 4,
           "owner_name": "Owner", "owner_path": "/repo/a.kt", "owner_line": 1}
    assert build_companion_of_edges(index, [row]) == []
