from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_inject_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_injected_class_is_matched_by_name_across_paths():
    index = SymbolIndex()
    index.register("Class", "Service", "/repo/a.java", 2, key="service")
    index.register("Class", "Repo", "/repo/b.java", 2, key="repo")
    row = {"injector_class": "Service", "injector_path": "/repo/a.java", "injected_class": "Repo",
           "field_name": "repo", "inject_line": 5}
    edges = build_inject_edges(index, [row])
    assert [(e["from"], e["to"], e["field_name"], e["inject_line"], e["confidence_label"]) for e in edges] == [
        ("service", "repo", "repo", 5, "EXTRACTED")
    ]


def test_missing_injected_class_writes_nothing():
    index = SymbolIndex()
    index.register("Class", "Service", "/repo/a.java", 2, key="service")
    row = {"injector_class": "Service", "injector_path": "/repo/a.java", "injected_class": "Repo",
           "field_name": "repo", "inject_line": 5}
    assert build_inject_edges(index, [row]) == []
