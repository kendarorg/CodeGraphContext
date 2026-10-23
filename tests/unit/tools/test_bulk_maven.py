"""Maven build graph on the KùzuDB bulk path: row shapes and edge matching, COPY stubbed."""

from codegraphcontext.tools.indexing.persistence import bulk_kuzu


def _capture(monkeypatch):
    calls = {"nodes": [], "rels": []}

    def fake_nodes(session, label, rows, tmp_dir):
        calls["nodes"].append((label, rows))

    def fake_rels(session, rel, from_label, to_label, rows, properties):
        calls["rels"].append((rel, from_label, to_label, rows, properties))

    monkeypatch.setattr(bulk_kuzu, "copy_node_table", fake_nodes)
    monkeypatch.setattr(bulk_kuzu, "copy_rel_rows", fake_rels)
    return calls


MODULES = [
    {"group_id": "com.example", "artifact_id": "parent-app", "version": "1.0.0",
     "packaging": "pom", "pom_path": "/r/pom.xml"},
    {"group_id": "com.example", "artifact_id": "core", "version": "1.0.0",
     "packaging": "jar", "pom_path": "/r/core/pom.xml"},
    {"group_id": "com.example", "artifact_id": "api", "version": "1.0.0",
     "packaging": "jar", "pom_path": "/r/api/pom.xml"},
]


def test_maven_nodes_keyed_by_group_and_artifact(monkeypatch):
    calls = _capture(monkeypatch)
    bulk_kuzu.load_maven_graph(None, MODULES, [], [], [], "/r")
    label, rows = calls["nodes"][0]
    assert label == "MavenModule"
    by_uid = {r["uid"]: r for r in rows}
    assert set(by_uid) == {"com.example" + a for a in ("parent-app", "core", "api")}
    assert by_uid["com.examplecore"]["path"] == "/r/core/pom.xml"
    assert by_uid["com.examplecore"]["repo_path"] == "/r"


def test_child_and_dependency_edges_match_by_artifact_id(monkeypatch):
    calls = _capture(monkeypatch)
    child = [{"parent_artifact_id": "parent-app", "child_artifact_id": "core"},
             {"parent_artifact_id": "parent-app", "child_artifact_id": "api"}]
    deps = [{"src_artifact_id": "api", "tgt_artifact_id": "core", "scope": "compile"},
            {"src_artifact_id": "api", "tgt_artifact_id": "missing", "scope": "compile"}]
    bulk_kuzu.load_maven_graph(None, MODULES, child, deps, [], "/r")
    rels = {r[0]: r for r in calls["rels"]}
    assert {(r["from"], r["to"]) for r in rels["CHILD_MODULE"][3]} == {
        ("com.exampleparent-app", "com.examplecore"),
        ("com.exampleparent-app", "com.exampleapi"),
    }
    dep_rows = rels["MODULE_DEPENDS_ON"][3]
    assert [(r["from"], r["to"], r["scope"]) for r in dep_rows] == [
        ("com.exampleapi", "com.examplecore", "compile")
    ]


def test_external_library_node_and_edge(monkeypatch):
    calls = _capture(monkeypatch)
    libs = [
        {"src_artifact_id": "core", "group_id": "com.google.guava", "artifact_id": "guava",
         "version": "1.0", "scope": "compile"},
        {"src_artifact_id": "core", "group_id": "com.google.guava", "artifact_id": "guava",
         "version": "2.0", "scope": "test"},
        {"src_artifact_id": "ghost", "group_id": "junit", "artifact_id": "junit",
         "version": "4.13.2", "scope": "test"},
    ]
    bulk_kuzu.load_maven_graph(None, MODULES, [], [], libs, "/r")
    lib_nodes = {r["uid"]: r for label, rows in calls["nodes"] if label == "ExternalLibrary" for r in rows}
    assert set(lib_nodes) == {"com.google.guavaguava", "junitjunit"}
    assert lib_nodes["com.google.guavaguava"]["version"] == "2.0"
    uses = [r for r in calls["rels"] if r[0] == "USES_LIBRARY"][0]
    assert uses[1:3] == ("MavenModule", "ExternalLibrary")
    assert [(r["from"], r["to"], r["scope"]) for r in uses[3]] == [
        ("com.examplecore", "com.google.guavaguava", "test")
    ]
