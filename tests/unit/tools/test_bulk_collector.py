import re

import pyarrow.parquet as pq

from codegraphcontext.tools.indexing.persistence.bulk_kuzu import BulkCollector

A = "/repo/a.py"

_COPY_RE = re.compile(r'COPY `(\w+)` FROM "([^"]+)"(?: \(from="(\w+)", to="(\w+)"\))?$')

SCHEMAS = {
    "Function": [
        ("name", "STRING"), ("path", "STRING"), ("line_number", "INT64"),
        ("occurrence_index", "INT64"), ("docstring", "STRING"), ("uid", "STRING"),
    ],
    "Class": [
        ("name", "STRING"), ("path", "STRING"), ("line_number", "INT64"),
        ("occurrence_index", "INT64"), ("uid", "STRING"),
    ],
    "Parameter": [
        ("name", "STRING"), ("path", "STRING"), ("function_line_number", "INT64"), ("uid", "STRING"),
    ],
    "Module": [("name", "STRING"), ("lang", "STRING"), ("full_import_name", "STRING")],
}


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def data(self):
        return list(self._rows)


class FakeSession:
    """Answers table_info from SCHEMAS and records every other statement.

    COPY files are read while they still exist (flush deletes its temp directory).
    """

    def __init__(self):
        self.statements = []

    def run(self, query, **params):
        q = query.strip()
        if q.startswith("CALL table_info"):
            label = re.search(r"'(\w+)'", q).group(1)
            return _Result([{"name": n, "type": t} for n, t in SCHEMAS[label]])
        match = _COPY_RE.match(q)
        if match:
            label, path, src, dst = match.groups()
            rows = pq.read_table(path).to_pylist()
            self.statements.append(("copy", label, src, dst, rows))
            return _Result()
        self.statements.append(("cypher", q, params))
        return _Result()

    def tags(self):
        """Short name per statement, in execution order."""
        out = []
        for s in self.statements:
            if s[0] == "copy":
                out.append(s[1])
            elif "CONTAINS" in s[1]:
                out.append("CONTAINS")
            else:
                out.append(s[1])
        return out

    def copy_rows(self, label):
        """Rows of every COPY into label, concatenated."""
        return [row for s in self.statements if s[0] == "copy" and s[1] == label for row in s[4]]


def _fn(name, line, occ=0, path=A, **extra):
    return {"name": name, "path": path, "line_number": line, "occurrence_index": occ, **extra}


def _param(arg, func, line=3, occ=0):
    return {"arg_name": arg, "func_name": func, "line_number": line, "occurrence_index": occ}


def _other(name, line=1, imported="*", alias=None, lang="python", full=None):
    return {
        "name": name,
        "line_number": line,
        "imported_name": imported,
        "alias": alias,
        "lang": lang,
        "full_import_name": full or name,
    }


def test_flush_runs_node_tables_then_contains_then_parameters_imports_and_deferred_work():
    c = BulkCollector()
    c.defer(lambda s: s.run("DEFERRED_WORK"))
    c.add_nodes("Function", [_fn("f", 3)], A)
    c.add_imports(A, [], [_other("os")])
    c.add_parameters(A, [_param("x", "f")])
    c.add_nodes("Class", [{"name": "C", "path": A, "line_number": 9, "occurrence_index": 0}], A)

    session = FakeSession()
    c.flush(session)

    assert session.tags() == [
        "Function",            # node tables, in order of first insertion
        "Module",
        "Parameter",
        "Class",
        "CONTAINS",            # one MERGE per label, after all node tables
        "CONTAINS",
        "HAS_PARAMETER",
        "IMPORTS",
        "DEFERRED_WORK",       # deferred work last
    ]


def test_empty_relationship_tables_are_not_copied():
    c = BulkCollector()
    c.add_nodes("Function", [_fn("f", 3)], A)

    session = FakeSession()
    c.flush(session)

    assert session.copy_rows("HAS_PARAMETER") == []
    assert session.copy_rows("IMPORTS") == []
    assert not any(s[0] == "copy" and s[1] in ("HAS_PARAMETER", "IMPORTS") for s in session.statements)


def test_rows_without_primary_key_are_dropped():
    c = BulkCollector()
    c.add_nodes("Module", [{"name": None, "lang": "python"}], A)

    session = FakeSession()
    c.flush(session)

    assert session.copy_rows("Module") == []


def test_same_primary_key_in_two_batches_yields_one_row_with_later_values_winning():
    c = BulkCollector()
    c.add_nodes("Function", [_fn("f", 3, docstring="first")], A)
    c.add_nodes("Function", [_fn("f", 3, docstring="second")], A)

    session = FakeSession()
    c.flush(session)

    rows = session.copy_rows("Function")
    assert len(rows) == 1
    assert rows[0]["docstring"] == "second"


def test_later_row_keeps_keys_it_does_not_set():
    c = BulkCollector()
    c.add_nodes("Function", [_fn("f", 3, docstring="kept")], A)
    c.add_nodes("Function", [_fn("f", 3)], A)  # no docstring key: SET n += row leaves it alone

    session = FakeSession()
    c.flush(session)

    assert session.copy_rows("Function")[0]["docstring"] == "kept"


def test_retried_batch_adds_no_second_node_row():
    c = BulkCollector()
    batch = [_fn("f", 3)]
    c.add_nodes("Function", batch, A)
    c.add_nodes("Function", batch, A)

    session = FakeSession()
    c.flush(session)

    assert len(session.copy_rows("Function")) == 1


def test_duplicate_parameter_gives_one_node_and_one_has_parameter_edge():
    c = BulkCollector()
    c.add_nodes("Function", [_fn("f", 3)], A)
    c.add_parameters(A, [_param("x", "f"), _param("x", "f")])

    session = FakeSession()
    c.flush(session)

    assert len(session.copy_rows("Parameter")) == 1
    assert session.copy_rows("HAS_PARAMETER") == [{"from": "f/repo/a.py30", "to": "x/repo/a.py3"}]


def test_has_parameter_is_skipped_when_the_function_was_not_loaded():
    c = BulkCollector()
    c.add_parameters(A, [_param("x", "missing")])

    session = FakeSession()
    c.flush(session)

    assert len(session.copy_rows("Parameter")) == 1
    assert session.copy_rows("HAS_PARAMETER") == []


def test_module_name_is_one_node_across_repeated_imports():
    c = BulkCollector()
    c.add_imports(A, [], [_other("os", line=1), _other("os", line=2, imported="path")])

    session = FakeSession()
    c.flush(session)

    rows = session.copy_rows("Module")
    assert [r["name"] for r in rows] == ["os"]


def test_module_keeps_first_non_null_metadata_value():
    c = BulkCollector()
    c.add_imports(
        A,
        [],
        [
            _other("os", lang=None, full=None),
            _other("os", lang="python", full="os"),
            _other("os", lang="go", full="other"),
        ],
    )

    session = FakeSession()
    c.flush(session)

    row = session.copy_rows("Module")[0]
    assert row["lang"] == "python"
    assert row["full_import_name"] == "os"


def test_imports_edge_merges_on_line_and_imported_name_with_last_alias_winning():
    c = BulkCollector()
    c.add_imports(A, [], [_other("os", imported="path", alias="p1"), _other("os", imported="path", alias="p2")])

    session = FakeSession()
    c.flush(session)

    rows = session.copy_rows("IMPORTS")
    assert len(rows) == 1
    assert rows[0]["alias"] == "p2"


def test_imports_edges_differing_in_imported_name_stay_separate():
    c = BulkCollector()
    c.add_imports(A, [], [_other("os", imported="path"), _other("os", imported="sep")])

    session = FakeSession()
    c.flush(session)

    assert sorted(r["imported_name"] for r in session.copy_rows("IMPORTS")) == ["path", "sep"]


def test_javascript_imports_merge_on_module_and_line_only():
    c = BulkCollector()
    js = [
        {"module_name": "react", "line_number": 1, "imported_name": "useState", "alias": "", "lang": "javascript"},
        {"module_name": "react", "line_number": 1, "imported_name": "useEffect", "alias": "e", "lang": "javascript"},
    ]
    c.add_imports(A, js, [])

    session = FakeSession()
    c.flush(session)

    rows = session.copy_rows("IMPORTS")
    assert len(rows) == 1
    assert rows[0]["imported_name"] == "useEffect"
    assert rows[0]["alias"] == "e"
    assert [r["name"] for r in session.copy_rows("Module")] == ["react"]
