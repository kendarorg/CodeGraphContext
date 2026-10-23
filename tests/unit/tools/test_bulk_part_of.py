from codegraphcontext.tools.indexing.persistence.bulk_kuzu import build_part_of_edges
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def test_part_of_links_two_registered_files():
    index = SymbolIndex()
    index.register("File", "lib.dart", "/repo/lib.dart", key="/repo/lib.dart")
    index.register("File", "part.dart", "/repo/part.dart", key="/repo/part.dart")
    rows = [{"child_path": "/repo/part.dart", "parent_path": "/repo/lib.dart"}]
    assert [(e["from"], e["to"], e["from_label"], e["to_label"]) for e in build_part_of_edges(index, rows)] == [
        ("/repo/part.dart", "/repo/lib.dart", "File", "File")
    ]


def test_part_of_skips_a_file_that_was_not_written():
    index = SymbolIndex()
    index.register("File", "lib.dart", "/repo/lib.dart", key="/repo/lib.dart")
    rows = [{"child_path": "/repo/missing.dart", "parent_path": "/repo/lib.dart"}]
    assert build_part_of_edges(index, rows) == []
