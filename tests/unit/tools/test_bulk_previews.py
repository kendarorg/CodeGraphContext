from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_previews_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_previews_matches_preview_line_and_defaults_line_number():
    index = SymbolIndex()
    index.register("Function", "Preview", "/repo/p.kt", 10, key="preview-10")
    index.register("Function", "Preview", "/repo/p.kt", 30, key="preview-30")
    index.register("Function", "Card", "/repo/c.kt", 1, key="card")
    row = {"preview_name": "Preview", "preview_path": "/repo/p.kt", "preview_line": 10,
           "composable_name": "Card", "composable_path": "/repo/c.kt"}
    edges = build_previews_edges(index, [row])
    assert [(e["from"], e["to"], e["line_number"]) for e in edges] == [("preview-10", "card", 10)]
