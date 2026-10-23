from codegraphcontext.tools.indexing.persistence.bulk_kuzu import BulkCollector
from codegraphcontext.tools.indexing.persistence.writer import _directory_chain


REPO = "/repo"


def test_root_level_file_links_repository_to_file():
    nodes, links = _directory_chain(REPO, (), "/repo/main.py")
    assert nodes == []
    assert links == [("Repository", "/repo", "File", "/repo/main.py")]


def test_nested_file_builds_one_directory_per_part():
    nodes, links = _directory_chain(REPO, ("src", "pkg"), "/repo/src/pkg/a.py")
    assert nodes == [("/repo/src", "src"), ("/repo/src/pkg", "pkg")]
    assert links == [
        ("Repository", "/repo", "Directory", "/repo/src"),
        ("Directory", "/repo/src", "Directory", "/repo/src/pkg"),
        ("Directory", "/repo/src/pkg", "File", "/repo/src/pkg/a.py"),
    ]


def test_collector_keeps_one_directory_node_and_one_link_per_pair():
    collector = BulkCollector()
    for path in ("/repo/src/a.py", "/repo/src/b.py"):
        nodes, links = _directory_chain(REPO, ("src",), path)
        collector.add_directory_links(nodes, links)
    table = collector._nodes["Directory"]
    assert table == {"/repo/src": {"path": "/repo/src", "name": "src"}}
    # Repository->Directory once, Directory->File once per file
    assert len(collector._rel_edges["CONTAINS"]) == 3


def test_collector_link_entries_carry_endpoint_labels():
    collector = BulkCollector()
    nodes, links = _directory_chain(REPO, ("src",), "/repo/src/a.py")
    collector.add_directory_links(nodes, links)
    edges = sorted(collector._rel_edges["CONTAINS"].values(), key=lambda e: e["to_label"])
    assert edges[0] == {
        "from": "/repo", "to": "/repo/src", "from_label": "Repository", "to_label": "Directory",
    }
    assert edges[1]["from_label"] == "Directory" and edges[1]["to_label"] == "File"
