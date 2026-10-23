from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_embeds_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_embeds_links_structs_and_defaults_line_to_zero():
    index = SymbolIndex()
    index.register("Struct", "Outer", "/repo/a.go", 2, key="outer")
    index.register("Struct", "Inner", "/repo/b.go", 1, key="inner")
    row = {"child_name": "Outer", "path": "/repo/a.go", "parent_name": "Inner",
           "resolved_parent_file_path": "/repo/b.go"}
    edges = build_embeds_edges(index, [row])
    assert [(e["from"], e["to"], e["line_number"]) for e in edges] == [("outer", "inner", 0)]
