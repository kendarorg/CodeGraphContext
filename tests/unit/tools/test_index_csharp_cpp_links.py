from codegraphcontext.tools.indexing.symbol_index import (
    SymbolIndex,
    add_cpp_class_function_edges,
    add_csharp_inherit_edges,
)


CS_PATH = "/repo/App/Models.cs"


def _pairs(index, rel):
    return sorted(
        (e.source.label, e.source.name, e.target.label, e.target.name, e.target.path)
        for e in index.edges(rel)
    )


def _cs_file(classes=(), interfaces=(), structs=(), records=()):
    return {
        "lang": "c_sharp",
        "path": CS_PATH,
        "classes": list(classes),
        "interfaces": list(interfaces),
        "structs": list(structs),
        "records": list(records),
    }


def _cs_index(*extra):
    index = SymbolIndex()
    index.register("Class", "Child", CS_PATH, 3)
    index.register("Class", "Base", CS_PATH, 1)
    index.register("Interface", "IThing", CS_PATH, 2)
    for label, name, path in extra:
        index.register(label, name, path, 1)
    return index


def test_csharp_first_class_base_is_recorded_as_inherits():
    index = _cs_index()
    file_data = _cs_file(classes=[{"name": "Child", "bases": ["Base"]}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert _pairs(index, "INHERITS") == [("Class", "Child", "Class", "Base", CS_PATH)]


def test_csharp_interface_base_is_not_recorded_as_inherits():
    index = _cs_index()
    file_data = _cs_file(
        classes=[{"name": "Child", "bases": ["IThing"]}],
        interfaces=[{"name": "IThing", "bases": []}],
    )

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert index.edges("INHERITS") == []


def test_csharp_non_first_base_of_class_is_not_recorded_as_inherits():
    index = _cs_index()
    index.register("Class", "Other", CS_PATH, 9)
    file_data = _cs_file(classes=[{"name": "Child", "bases": ["Base", "Other"]}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert _pairs(index, "INHERITS") == [("Class", "Child", "Class", "Base", CS_PATH)]


def test_csharp_generic_base_name_is_stripped_before_matching():
    index = _cs_index()
    file_data = _cs_file(classes=[{"name": "Child", "bases": ["Base<int>"]}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert _pairs(index, "INHERITS") == [("Class", "Child", "Class", "Base", CS_PATH)]


def test_csharp_parent_is_matched_by_name_across_paths():
    other = "/repo/Lib/Base.cs"
    index = _cs_index(("Class", "Base", other))
    file_data = _cs_file(classes=[{"name": "Child", "bases": ["Base"]}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert _pairs(index, "INHERITS") == [
        ("Class", "Child", "Class", "Base", CS_PATH),
        ("Class", "Child", "Class", "Base", other),
    ]


def test_csharp_struct_child_is_not_recorded():
    # The standard Cypher matches child labels Class, Record, Interface, Mixin, Extension only.
    index = _cs_index()
    index.register("Struct", "Point", CS_PATH, 5)
    file_data = _cs_file(structs=[{"name": "Point", "bases": ["Base"]}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert index.edges("INHERITS") == []


def test_csharp_child_is_matched_only_in_its_own_file():
    index = _cs_index()
    index.register("Class", "Child", "/repo/Other.cs", 3)
    file_data = _cs_file(classes=[{"name": "Child", "bases": ["Base"]}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    sources = {(e.source.path) for e in index.edges("INHERITS")}
    assert sources == {CS_PATH}


def test_csharp_no_bases_records_nothing():
    index = _cs_index()
    file_data = _cs_file(classes=[{"name": "Child", "bases": []}])

    add_csharp_inherit_edges(index, file_data, CS_PATH)

    assert index.edges("INHERITS") == []


def test_entries_with_label_returns_every_path():
    index = SymbolIndex()
    index.register("Function", "run", "/repo/a.py", 1)
    index.register("Function", "run", "/repo/b.py", 2)
    index.register("Class", "run", "/repo/a.py", 3)

    assert [(e.path, e.line_number) for e in index.entries_with_label("Function")] == [
        ("/repo/a.py", 1),
        ("/repo/b.py", 2),
    ]


CPP_REPO = "/repo/lib"


def _cpp_index():
    index = SymbolIndex()
    index.register("Class", "Widget", f"{CPP_REPO}/widget.h", 4)
    index.register("Struct", "Vec", f"{CPP_REPO}/vec.h", 2)
    index.register("Module", "Util", f"{CPP_REPO}/util.h", 1)
    index.register(
        "Function", "draw", f"{CPP_REPO}/widget.cpp", 10,
        class_context="Widget",
    )
    index.register(
        "Function", "len", f"{CPP_REPO}/vec.cpp", 6,
        class_context="Vec",
    )
    index.register(
        "Function", "helper", f"{CPP_REPO}/util.cc", 3,
        class_context="Util",
    )
    return index


def _cpp_pairs(index):
    return sorted(
        (e.source.label, e.source.name, e.source.path, e.target.name, e.target.path)
        for e in index.edges("CONTAINS")
    )


def test_cpp_out_of_line_method_links_to_class_in_another_file():
    index = _cpp_index()

    add_cpp_class_function_edges(index, CPP_REPO)

    assert ("Class", "Widget", f"{CPP_REPO}/widget.h", "draw", f"{CPP_REPO}/widget.cpp") in _cpp_pairs(index)


def test_cpp_struct_and_module_containers_are_linked():
    index = _cpp_index()

    add_cpp_class_function_edges(index, CPP_REPO)

    labels = {(e.source.label, e.target.name) for e in index.edges("CONTAINS")}
    assert ("Struct", "len") in labels
    assert ("Module", "helper") in labels


def test_cpp_function_without_class_context_is_skipped():
    index = _cpp_index()
    index.register("Function", "free", f"{CPP_REPO}/widget.cpp", 20, class_context="")

    add_cpp_class_function_edges(index, CPP_REPO)

    assert all(e.target.name != "free" for e in index.edges("CONTAINS"))


def test_cpp_non_cpp_extension_is_skipped():
    index = SymbolIndex()
    index.register("Class", "Widget", f"{CPP_REPO}/widget.h", 4)
    index.register("Function", "draw", f"{CPP_REPO}/widget.py", 10, class_context="Widget")

    add_cpp_class_function_edges(index, CPP_REPO)

    assert index.edges("CONTAINS") == []


def test_cpp_container_outside_repo_is_not_linked():
    index = SymbolIndex()
    index.register("Class", "Widget", "/elsewhere/widget.h", 4)
    index.register("Function", "draw", f"{CPP_REPO}/widget.cpp", 10, class_context="Widget")

    add_cpp_class_function_edges(index, CPP_REPO)

    assert index.edges("CONTAINS") == []


def test_cpp_function_outside_repo_is_not_linked():
    index = SymbolIndex()
    index.register("Class", "Widget", f"{CPP_REPO}/widget.h", 4)
    index.register("Function", "draw", "/elsewhere/widget.cpp", 10, class_context="Widget")

    add_cpp_class_function_edges(index, CPP_REPO)

    assert index.edges("CONTAINS") == []


def test_cpp_all_cpp_extensions_are_accepted():
    index = SymbolIndex()
    index.register("Class", "K", f"{CPP_REPO}/k.h", 1)
    for i, ext in enumerate((".cpp", ".cc", ".cxx", ".c++", ".C")):
        index.register("Function", f"m{i}", f"{CPP_REPO}/k{ext}", 2 + i, class_context="K")

    add_cpp_class_function_edges(index, CPP_REPO)

    assert len(index.edges("CONTAINS")) == 5
