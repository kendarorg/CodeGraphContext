from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_partial_of_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_partial_of_links_the_two_parts_with_inferred_confidence():
    index = SymbolIndex()
    index.register("Class", "Thing", "/repo/a.cs", 3, key="part-a")
    index.register("Class", "Thing", "/repo/b.cs", 3, key="part-b")
    row = {"child_name": "Thing", "child_label": "Class", "path": "/repo/b.cs",
           "parent_name": "Thing", "parent_label": "Class", "resolved_parent_file_path": "/repo/a.cs",
           "confidence_label": None}
    edges = build_partial_of_edges(index, [row])
    assert [(e["from"], e["to"], e["confidence_label"]) for e in edges] == [("part-b", "part-a", "INFERRED")]
