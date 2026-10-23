from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_inclusion_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_inclusion_links_class_in_this_file_to_module():
    index = SymbolIndex()
    index.register("Class", "Session", "/repo/s.rb", 1, key="session")
    rows = [{"class": "Session", "module": "Mixins"}]
    edges, modules = build_inclusion_edges(index, rows, "/repo/s.rb")
    assert [(e["from"], e["to"], e["from_label"], e["to_label"]) for e in edges] == [
        ("session", "Mixins", "Class", "Module")
    ]
    assert modules == {"Mixins"}


def test_no_module_is_created_when_the_class_is_missing():
    index = SymbolIndex()
    rows = [{"class": "Session", "module": "Mixins"}]
    edges, modules = build_inclusion_edges(index, rows, "/repo/s.rb")
    assert edges == []
    assert modules == set()
