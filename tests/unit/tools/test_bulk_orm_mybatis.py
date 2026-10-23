from codegraphcontext.tools.indexing.persistence import bulk_kuzu
from codegraphcontext.tools.indexing.persistence.bulk_kuzu import (
    build_function_table_edges,
    build_maps_to_edges,
    load_db_tables,
)
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex


def _index():
    index = SymbolIndex()
    index.register("Class", "User", "/repo/User.java", 7, key="user")
    index.register("Function", "findNameById", "/repo/UserMapper.java", 3, key="find")
    index.register("Function", "insertUser", "/repo/UserMapper.java", 5, key="insert")
    index.register("Function", "searchByName", "/repo/UserRepository.java", 8, key="search")
    return index


def test_maps_to_links_class_to_table_and_keeps_first_datastore_for_the_table():
    rows = [
        {"kind": "class_table", "class_name": "User", "class_path": "/repo/User.java",
         "orm_table": "users", "datastore": "mysql", "line_number": 7},
        {"kind": "class_table", "class_name": "User", "class_path": "/repo/User.java",
         "orm_table": "users", "datastore": "cassandra", "line_number": 7},
    ]
    edges, tables = build_maps_to_edges(_index(), rows)
    assert [(e["from"], e["to"], e["from_label"], e["to_label"], e["datastore"]) for e in edges] == [
        ("user", "users", "Class", "DbTable", "mysql"),
        ("user", "users", "Class", "DbTable", "cassandra"),
    ]
    assert tables == [("users", "users", "mysql"), ("users", "users", "cassandra")]


def test_maps_to_without_a_class_creates_no_table():
    rows = [{"kind": "class_table", "class_name": "Missing", "class_path": "/repo/User.java",
             "orm_table": "ghosts", "datastore": "mysql", "line_number": 1}]
    edges, tables = build_maps_to_edges(_index(), rows)
    assert edges == []
    assert tables == []


def test_function_table_edges_split_by_operation_and_skip_unnamed_methods():
    matches = [
        ("findNameById", "/repo/UserMapper.java", "users", "READS", None),
        ("insertUser", "/repo/UserMapper.java", "users", "WRITES", None),
        ("searchByName", "/repo/UserRepository.java", "users", "READS", 8),
        (None, "/repo/UserRepository.java", "other", "READS", 1),
        ("noSuchMethod", "/repo/UserRepository.java", "ghosts", "READS", 2),
    ]
    by_operation, tables = build_function_table_edges(_index(), matches)
    assert [(e["from"], e["to"], e["line_number"]) for e in by_operation["READS"]] == [
        ("find", "users", None),
        ("search", "users", 8),
    ]
    assert [(e["from"], e["to"]) for e in by_operation["WRITES"]] == [("insert", "users")]
    assert tables == [("users", "users", "mysql")] * 3


def test_function_table_edges_repeat_pair_on_same_line_is_kept_once():
    matches = [
        ("searchByName", "/repo/UserRepository.java", "users", "READS", 8),
        ("searchByName", "/repo/UserRepository.java", "users", "READS", 8),
    ]
    by_operation, _ = build_function_table_edges(_index(), matches)
    assert len(by_operation["READS"]) == 1


class _FakeSession:
    def __init__(self, existing):
        self.existing = existing

    def run(self, query):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def data(self):
                return self._rows

        return _Result([{"fqn": fqn} for fqn in self.existing])


def test_load_db_tables_creates_only_missing_tables_once(monkeypatch):
    loaded = []

    def fake_copy(session, label, rows, tmp_dir):
        loaded.append((label, [r["fqn"] for r in rows], [r["datasource_name"] for r in rows]))

    monkeypatch.setattr(bulk_kuzu, "copy_node_table", fake_copy)
    session = _FakeSession(existing={"orders"})
    load_db_tables(session, [
        ("users", "users", "mysql"),
        ("users", "users", "cassandra"),
        ("orders", "orders", "mysql"),
    ])
    assert loaded == [("DbTable", ["users"], ["mysql"])]


def test_load_db_tables_with_nothing_to_add_copies_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(bulk_kuzu, "copy_node_table", lambda *a, **k: calls.append(a))
    load_db_tables(_FakeSession(existing={"users"}), [("users", "users", "mysql")])
    load_db_tables(_FakeSession(existing=set()), [])
    assert calls == []
