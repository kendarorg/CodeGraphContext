from codegraphcontext.tools.indexing.symbol_index import SymbolIndex, kuzu_uid


def _index():
    index = SymbolIndex()
    index.register("Function", "run", "/repo/a.py", 10, context="")
    index.register("Class", "run", "/repo/a.py", 20)
    index.register("Function", "handler", "/repo/b.py", 5, context="Service")
    return index


def test_has_label_reflects_registered_labels_only():
    index = _index()
    assert index.has_label("Function")
    assert index.has_label("Class")
    assert not index.has_label("Interface")


def test_labels_for_returns_distinct_labels_in_write_order():
    index = _index()
    assert index.labels_for("run", "/repo/a.py") == ["Function", "Class"]
    assert index.labels_for("missing", "/repo/a.py") == []


def test_has_node_matches_label_name_and_path():
    index = _index()
    assert index.has_node("Function", "run", "/repo/a.py")
    assert not index.has_node("Interface", "run", "/repo/a.py")
    assert not index.has_node("Function", "run", "/repo/other.py")


def test_has_node_checks_line_number_only_when_given():
    index = _index()
    assert index.has_node("Function", "run", "/repo/a.py", 10)
    assert not index.has_node("Function", "run", "/repo/a.py", 11)


def test_has_node_context_predicate_mirrors_cypher_clause():
    index = _index()
    # Empty requested context matches any node, like `$ctx = "" OR n.context = $ctx`.
    assert index.has_node("Function", "handler", "/repo/b.py", context="")
    assert index.has_node("Function", "handler", "/repo/b.py", context="Service")
    assert not index.has_node("Function", "handler", "/repo/b.py", context="Other")


def test_len_counts_registered_nodes():
    assert len(_index()) == 3
    assert len(SymbolIndex()) == 0


def test_kuzu_uid_concatenates_identity_fields_without_separator():
    row = {"name": "advanced", "path": "/repo/a.py", "line_number": 40, "occurrence_index": 0}
    assert kuzu_uid("Function", row) == "advanced/repo/a.py400"


def test_kuzu_uid_defaults_missing_occurrence_index_to_zero():
    row = {"name": "run", "path": "/repo/a.py", "line_number": 7}
    assert kuzu_uid("Class", row) == "run/repo/a.py70"


def test_kuzu_uid_parameter_uses_function_line_number():
    row = {"name": "x", "path": "/repo/a.py", "function_line_number": 3}
    assert kuzu_uid("Parameter", row) == "x/repo/a.py3"


def test_kuzu_uid_is_none_for_labels_keyed_by_name_or_path():
    assert kuzu_uid("File", {"path": "/repo/a.py"}) is None
    assert kuzu_uid("Module", {"name": "os"}) is None
