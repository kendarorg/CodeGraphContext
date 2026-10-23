# src/codegraphcontext/tools/indexing/persistence/bulk_kuzu.py
"""Bulk node loading for KùzuDB through COPY FROM Parquet.

Repo initialization collects every node row in memory, then writes each node table
with one COPY per table, instead of one MERGE per file and label. Parquet is used
because Kùzu's CSV reader cannot represent list elements that contain commas,
brackets or quotes; Parquet keeps list columns native.

Primary keys follow the Kùzu schema: ``uid`` for code nodes (see ``kuzu_uid``),
``name`` for Module, ``path`` for File and Directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..symbol_index import kuzu_uid
from ....utils.debug_log import info_logger

_NAME_KEYED = {"Module"}
_PATH_KEYED = {"File", "Directory", "Repository"}

_ARROW_TYPES = {
    "STRING": "string",
    "INT64": "int64",
    "INT32": "int32",
    "BOOL": "bool_",
    "BOOLEAN": "bool_",
    "DOUBLE": "float64",
    "FLOAT": "float32",
    "STRING[]": ("list", "string"),
    "DOUBLE[]": ("list", "float64"),
    "INT64[]": ("list", "int64"),
}


def pyarrow_available() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


def primary_key_field(label: str) -> str:
    if label in _NAME_KEYED:
        return "name"
    if label in _PATH_KEYED:
        return "path"
    return "uid"


def primary_key_value(label: str, props: Dict[str, Any]) -> Optional[str]:
    field = primary_key_field(label)
    if field == "uid":
        return kuzu_uid(label, props)
    value = props.get(field)
    return None if value is None else str(value)


def _arrow_type(kuzu_type: str):
    import pyarrow as pa

    spec = _ARROW_TYPES.get(kuzu_type)
    if spec is None:
        raise NotImplementedError(f"No Parquet mapping for Kùzu type {kuzu_type!r}")
    if isinstance(spec, tuple):
        return pa.list_(getattr(pa, spec[1])())
    return getattr(pa, spec)()


def _coerce(value: Any, kuzu_type: str) -> Any:
    if value is None:
        return None
    if kuzu_type.endswith("[]"):
        if not isinstance(value, (list, tuple)):
            return None
        inner = kuzu_type[:-2]
        if inner == "STRING":
            return [str(x) for x in value]
        return list(value)
    if kuzu_type in ("BOOL", "BOOLEAN"):
        return bool(value)
    if kuzu_type in ("INT64", "INT32"):
        return int(value)
    if kuzu_type in ("DOUBLE", "FLOAT"):
        return float(value)
    return str(value)


class BulkCollector:
    """Accumulates node rows and deferred per-file work during one repo initialization."""

    def __init__(self) -> None:
        # label -> primary key -> merged row (later rows overwrite keys they set, like SET n += row)
        self._nodes: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # (file_path, label, primary key) for the File -> node CONTAINS edges
        self._contains: List[Tuple[str, str, str]] = []
        # Work to run after the node tables are loaded, in file order
        self._deferred: List[Callable[[Any], None]] = []
        # (Function key, Parameter key) pairs for HAS_PARAMETER
        self._has_parameter: set = set()
        # IMPORTS edges keyed as the MERGE patterns key them (see add_imports)
        self._import_edges: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        # relationship label -> (from label, from key, to label, to key) -> edge
        self._rel_edges: Dict[str, Dict[Tuple[str, str, str, str], Dict[str, Any]]] = {}
        # relationship label -> property name -> Kùzu type, for the COPY of its edges
        self._rel_props: Dict[str, Dict[str, str]] = {}

    def add_rel_edges(self, rel_label: str, edges: List[Dict[str, Any]], properties: Dict[str, str]) -> None:
        """Collect edges for one relationship; one edge per endpoint pair, last one wins.

        Each edge carries from, to, from_label and to_label, and its property values by name.
        The MERGE these replace was keyed on the endpoints, so a repeated pair is kept once.
        """
        store = self._rel_edges.setdefault(rel_label, {})
        self._rel_props.setdefault(rel_label, {}).update(properties)
        for edge in edges:
            store[(edge["from_label"], edge["from"], edge["to_label"], edge["to"])] = edge

    def add_directory_links(
        self,
        nodes: List[Tuple[str, str]],
        links: List[Tuple[str, str, str, str]],
    ) -> None:
        """Collect Directory nodes ((path, name) pairs) and their CONTAINS links for one file.

        Links run Repository or Directory to Directory or File.
        """
        table = self._nodes.setdefault("Directory", {})
        for path, name in nodes:
            table[path] = {"path": path, "name": name}
        self.add_rel_edges(
            "CONTAINS",
            [
                {"from": from_path, "to": to_path, "from_label": from_label, "to_label": to_label}
                for from_label, from_path, to_label, to_path in links
            ],
            {},
        )

    def add_inclusions(self, edges: List[Dict[str, Any]], module_names: set) -> None:
        """Collect INCLUDES edges (Class to Module) and the Module nodes they point at.

        Module is keyed by name; a Module that already has properties from an import keeps them.
        """
        modules = self._nodes.setdefault("Module", {})
        for name in sorted(module_names):
            modules.setdefault(name, {"name": name})
        self.add_rel_edges("INCLUDES", edges, {})

    def add_imports(
        self,
        file_path: str,
        js_rows: List[Dict[str, Any]],
        other_rows: List[Dict[str, Any]],
    ) -> None:
        """Apply one file's import rows, in the order the per-file MERGE queries ran.

        Module nodes follow the import writer: a name-only MERGE for JavaScript, and
        `SET m.lang = coalesce(m.lang, row.lang)` (and the same for full_import_name)
        for the rest, so an existing non-null value is kept. ``other_rows`` must already
        be in metadata order. IMPORTS edges are merged on their MERGE keys, last row winning.
        """
        modules = self._nodes.setdefault("Module", {})
        for r in js_rows:
            modules.setdefault(r["module_name"], {"name": r["module_name"]})
            key = ("js", file_path, r["module_name"], r["line_number"])
            self._import_edges[key] = {
                "from": file_path,
                "to": r["module_name"],
                "line_number": r["line_number"],
                "imported_name": r["imported_name"],
                "alias": r["alias"],
                "lang": r["lang"],
                "full_import_name": None,
            }
        for r in other_rows:
            node = modules.setdefault(r["name"], {"name": r["name"]})
            for k in ("lang", "full_import_name"):
                if node.get(k) is None:
                    node[k] = r.get(k)
            key = ("other", file_path, r["name"], r["line_number"], r["imported_name"])
            self._import_edges[key] = {
                "from": file_path,
                "to": r["name"],
                "line_number": r["line_number"],
                "imported_name": r["imported_name"],
                "alias": r.get("alias") or "",
                "full_import_name": r["full_import_name"],
                "lang": r["lang"],
            }

    def add_parameters(self, file_path: str, params: List[Dict[str, Any]]) -> None:
        """Collect Parameter nodes and HAS_PARAMETER pairs for one file's parameter rows."""
        table = self._nodes.setdefault("Parameter", {})
        for p in params:
            props = {"name": p["arg_name"], "path": file_path, "function_line_number": p["line_number"]}
            key = kuzu_uid("Parameter", props)
            if key not in table:
                table[key] = {**props, "uid": key}
            fn_key = kuzu_uid(
                "Function",
                {
                    "name": p["func_name"],
                    "path": file_path,
                    "line_number": p["line_number"],
                    "occurrence_index": p["occurrence_index"],
                },
            )
            self._has_parameter.add((fn_key, key))

    def add_nodes(self, label: str, batch: List[Dict[str, Any]], file_path: str) -> None:
        table = self._nodes.setdefault(label, {})
        for row in batch:
            key = primary_key_value(label, row)
            if key is None:
                continue
            # Code nodes carry no `uid` from the parser; the table needs the computed key.
            stored = dict(row)
            stored[primary_key_field(label)] = key
            merged = table.get(key)
            if merged is None:
                table[key] = stored
            else:
                merged.update(stored)
            self._contains.append((file_path, label, key))

    def defer(self, work: Callable[[Any], None]) -> None:
        self._deferred.append(work)

    @property
    def is_empty(self) -> bool:
        return not self._nodes and not self._deferred and not any(self._rel_edges.values())

    def flush(self, session: Any) -> None:
        """COPY every node table, create CONTAINS edges, then run the deferred work."""
        tmp_dir = tempfile.mkdtemp(prefix="cgc-bulk-")
        try:
            for label, table in self._nodes.items():
                copy_node_table(session, label, list(table.values()), tmp_dir)
                info_logger(f"[BULK] Loaded {len(table)} {label} node(s)")
            self._write_contains(session)
            self._write_rel_edges(session)
            self._write_has_parameter(session)
            copy_rel_rows(
                session,
                "IMPORTS",
                "File",
                "Module",
                list(self._import_edges.values()),
                {
                    "alias": "STRING",
                    "full_import_name": "STRING",
                    "imported_name": "STRING",
                    "line_number": "INT64",
                    "lang": "STRING",
                },
            )
            for work in self._deferred:
                work(session)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _write_rel_edges(self, session: Any) -> None:
        # Runs after the node tables, so every endpoint of the collected edges is loaded.
        for rel_label, store in self._rel_edges.items():
            if store:
                copy_grouped_edges(
                    session,
                    rel_label,
                    list(store.values()),
                    self._rel_props.get(rel_label, {}),
                    declared_bindings(session, rel_label),
                )

    def _write_has_parameter(self, session: Any) -> None:
        # Only pairs whose Function node was loaded: the MATCH it replaces required both ends.
        function_keys = set(self._nodes.get("Function", {}))
        edges = [{"from": fn, "to": param} for fn, param in sorted(self._has_parameter) if fn in function_keys]
        copy_rel_rows(session, "HAS_PARAMETER", "Function", "Parameter", edges, {})

    def _write_contains(self, session: Any) -> None:
        by_label: Dict[str, List[Dict[str, str]]] = {}
        for file_path, label, key in self._contains:
            by_label.setdefault(label, []).append({"file_path": file_path, "key": key})
        for label, batch in by_label.items():
            field = primary_key_field(label)
            session.run(
                f"""
                UNWIND $batch AS row
                MATCH (f:File {{path: row.file_path}})
                MATCH (n:`{label}` {{{field}: row.key}})
                MERGE (f)-[:CONTAINS]->(n)
                """,
                batch=batch,
            )


# Relationship properties written by write_function_call_groups, in table order.
# Mirrors the CALLS / HEURISTIC_CALLS schema in core/database_embedded_kuzu.py.
CALL_REL_PROPERTIES: Dict[str, str] = {
    "line_number": "INT64",
    "args": "STRING[]",
    "full_call_name": "STRING",
    "args_key": "STRING",
    "confidence": "DOUBLE",
    "resolution_tier": "INT64",
    "confidence_label": "STRING",
    "source": "STRING",
    "resolution_method": "STRING",
    "called_name": "STRING",
}


# Labels whose MATCH in write_function_call_groups has no line_number predicate.
_CALL_LABELS_WITHOUT_LINE = {"File", "Directory"}


def build_call_edges(
    index: Any,
    rows: List[Dict[str, Any]],
    caller_label: str = "Function",
    called_label: str = "Function",
) -> List[Dict[str, Any]]:
    """Resolve call rows to edges between two labels, using the index instead of MATCH.

    Matching follows the Cypher the writer used before:
      caller:   a File matches by path; a Function matches by name, path and line_number.
      called:   name and path equal; line_number matches when called_line_number > 0
                (not for File); context matches context, class_context or module_context
                when called_context is set (Function only).
    One row can match several callers or callees; each pair gets an edge, as MERGE did.
    Edges with the same (caller, callee, line_number, full_call_name, args_key) are
    merged, the last row winning, as the SET clauses did.
    """
    edges: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        if caller_label == "File":
            caller_entry = index.file_entry(row["caller_file_path"])
            callers = [caller_entry] if caller_entry is not None else []
        else:
            caller_line = row.get("caller_line_number")
            if caller_line is None:
                continue
            callers = [
                e for e in index.entries(caller_label, row["caller_name"], row["caller_file_path"])
                if e.line_number == caller_line
            ]
        if not callers:
            continue

        called_line = row.get("called_line_number") or 0
        called_context = row.get("called_context") or ""
        callees = []
        for e in index.entries(called_label, row["called_name"], row["called_file_path"]):
            if called_label not in _CALL_LABELS_WITHOUT_LINE and called_line > 0 and e.line_number != called_line:
                continue
            if called_label == "Function" and called_context and called_context not in (
                e.context, e.class_context, e.module_context,
            ):
                continue
            callees.append(e)

        for caller in callers:
            for callee in callees:
                key = (
                    caller.key,
                    callee.key,
                    row.get("line_number"),
                    row.get("full_call_name"),
                    row.get("args_key"),
                )
                args = row.get("args")
                edges[key] = {
                    "from": caller.key,
                    "to": callee.key,
                    "line_number": row.get("line_number"),
                    "args": [str(a) for a in args] if isinstance(args, list) else None,
                    "full_call_name": row.get("full_call_name"),
                    "args_key": row.get("args_key"),
                    "confidence": row.get("confidence"),
                    "resolution_tier": row.get("resolution_tier"),
                    "confidence_label": row.get("confidence_label"),
                    "source": None,
                    "resolution_method": None,
                    "called_name": None,
                }
    return list(edges.values())


def copy_path(path: str) -> str:
    """A file path as written inside a COPY statement.

    On Windows the separators become "/": LadybugDB reads backslashes in the
    quoted literal as escape sequences and rejects the statement. Other
    platforms get the path unchanged.
    """
    if sys.platform == "win32":
        return path.replace("\\", "/")
    return path


def copy_node_table(session: Any, label: str, rows: List[Dict[str, Any]], tmp_dir: str) -> None:
    """COPY node rows into one node table, typed by the table's own columns."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = session.run(f"CALL table_info('{label}') RETURN name, type").data()
    arrays = []
    names = []
    for col in columns:
        name, kuzu_type = col["name"], col["type"]
        values = [_coerce(row.get(name), kuzu_type) for row in rows]
        arrays.append(pa.array(values, type=_arrow_type(kuzu_type)))
        names.append(name)
    path = os.path.join(tmp_dir, f"{label}.parquet")
    pq.write_table(pa.table(dict(zip(names, arrays))), path)
    try:
        session.run(f"COPY `{label}` FROM \"{copy_path(path)}\"")
    except Exception as exc:
        raise RuntimeError(f"Bulk COPY into {label} failed ({len(rows)} rows): {exc}") from exc


def load_external_classes(session: Any, names: set) -> None:
    """Create ExternalClass nodes (keyed by name) that do not exist yet.

    The MERGE it replaces created each missing name once and kept existing ones.
    """
    if not names:
        return
    existing = {
        r["name"] for r in session.run("MATCH (n:ExternalClass) RETURN n.name AS name").data()
    }
    missing = sorted(n for n in names if n not in existing)
    if not missing:
        return
    tmp_dir = tempfile.mkdtemp(prefix="cgc-ext-")
    try:
        copy_node_table(session, "ExternalClass", [{"name": n} for n in missing], tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    info_logger(f"[BULK] Loaded {len(missing)} ExternalClass node(s)")


def copy_rel_rows(
    session: Any,
    rel_label: str,
    from_label: str,
    to_label: str,
    rows: List[Dict[str, Any]],
    properties: Dict[str, str],
) -> None:
    """COPY relationship rows into one FROM..TO binding of a relationship table."""
    if not rows:
        return
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrays: Dict[str, Any] = {
        "from": pa.array([str(r["from"]) for r in rows], pa.string()),
        "to": pa.array([str(r["to"]) for r in rows], pa.string()),
    }
    for name, kuzu_type in properties.items():
        values = [_coerce(r.get(name), kuzu_type) for r in rows]
        arrays[name] = pa.array(values, type=_arrow_type(kuzu_type))

    tmp_dir = tempfile.mkdtemp(prefix="cgc-rel-")
    try:
        path = os.path.join(tmp_dir, f"{rel_label}.parquet")
        pq.write_table(pa.table(arrays), path)
        try:
            session.run(
                f"COPY `{rel_label}` FROM \"{copy_path(path)}\" (from=\"{from_label}\", to=\"{to_label}\")"
            )
        except Exception as exc:
            raise RuntimeError(
                f"Bulk COPY into {rel_label} {from_label}->{to_label} failed ({len(rows)} rows): {exc}"
            ) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def declared_bindings(session: Any, rel_label: str) -> set:
    """(from label, to label) pairs declared for a relationship table or group."""
    rows = session.run(
        f"CALL show_connection('{rel_label}') RETURN `source table name` AS src, "
        "`destination table name` AS dst"
    ).data()
    return {(r["src"], r["dst"]) for r in rows}


def build_inherit_edges(
    index: Any,
    rows: List[Dict[str, Any]],
    child_label: str,
    parent_label: str,
) -> List[Dict[str, Any]]:
    """INHERITS edges for one (child label, parent label) group, resolved from the index.

    Matches the old MERGE: child by name and path, parent by name and the resolved
    parent file path. The edge carries confidence_label (default EXTRACTED); a repeated
    (child, parent) pair keeps the last row's value, as SET did.
    """
    edges: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for row in rows:
        children = index.entries(child_label, row["child_name"], row["path"])
        if not children:
            continue
        parents = index.entries(parent_label, row["parent_name"], row["resolved_parent_file_path"])
        for child in children:
            for parent in parents:
                edges[(child.key, parent.key)] = {
                    "from": child.key,
                    "to": parent.key,
                    "confidence_label": row.get("confidence_label") or "EXTRACTED",
                }
    return list(edges.values())


def build_external_inherit_edges(
    index: Any,
    rows: List[Dict[str, Any]],
    child_label: str,
) -> Tuple[List[Dict[str, Any]], set]:
    """INHERITS edges from one child label to ExternalClass parents, and the parent names.

    The child matches by name and path. An ExternalClass is keyed by name, so the parent
    key is the parent name. confidence_label defaults to INFERRED (as coalesce did in the
    MERGE it replaces); a repeated (child, parent) pair keeps the last row's value.
    Parent names are returned for every row whose child exists, because the MERGE that
    created ExternalClass nodes ran for those rows whether or not the edge binds.
    """
    edges: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    parent_names: set = set()
    for row in rows:
        children = index.entries(child_label, row["child_name"], row["path"])
        if not children:
            continue
        parent = str(row["parent_name"])
        parent_names.add(parent)
        label_value = row.get("confidence_label")
        confidence = "INFERRED" if label_value is None else label_value
        for child in children:
            edges[(child.key, parent)] = {
                "from": child.key,
                "to": parent,
                "confidence_label": confidence,
            }
    return list(edges.values()), parent_names


def copy_grouped_edges(
    session: Any,
    rel_label: str,
    edges: List[Dict[str, Any]],
    properties: Dict[str, str],
    bindings: set,
) -> None:
    """COPY edges that carry from_label / to_label, one COPY per declared binding."""
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for edge in edges:
        groups.setdefault((edge["from_label"], edge["to_label"]), []).append(edge)
    for (from_label, to_label), rows in groups.items():
        if (from_label, to_label) in bindings:
            copy_rel_rows(session, rel_label, from_label, to_label, rows, properties)


def _pair_edge(source: Any, target: Any, props: Dict[str, Any]) -> Dict[str, Any]:
    edge = {"from": source.key, "to": target.key, "from_label": source.label, "to_label": target.label}
    edge.update(props)
    return edge


def load_db_tables(session: Any, tables: List[Tuple[str, str, str]]) -> None:
    """Create DbTable nodes (keyed by fqn) that do not exist yet.

    ``tables`` holds (fqn, name, datasource_name) in the order the MERGEs ran. The
    MERGE these replace kept the datasource_name it saw first (COALESCE), so the first
    row for an fqn wins.
    """
    if not tables:
        return
    existing = {r["fqn"] for r in session.run("MATCH (t:DbTable) RETURN t.fqn AS fqn").data()}
    new: Dict[str, Dict[str, Any]] = {}
    for fqn, name, datasource in tables:
        if fqn not in existing and fqn not in new:
            new[fqn] = {"fqn": fqn, "name": name, "datasource_name": datasource}
    if not new:
        return
    tmp_dir = tempfile.mkdtemp(prefix="cgc-tbl-")
    try:
        copy_node_table(session, "DbTable", list(new.values()), tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    info_logger(f"[BULK] Loaded {len(new)} DbTable node(s)")


def build_maps_to_edges(
    index: Any,
    rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str, str]]]:
    """MAPS_TO edges (Class to DbTable) for class_table rows, and the DbTable rows they need.

    The MATCH these replace needs a Class with the row's name and path, so a row without
    one creates no table. One edge per (class, table, datastore, line_number), the keys of
    the MERGE pattern.
    """
    edges: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    tables: List[Tuple[str, str, str]] = []
    for row in rows:
        classes = index.entries("Class", row["class_name"], row["class_path"])
        if not classes:
            continue
        fqn = row["orm_table"]
        datastore = row.get("datastore")
        line_number = row.get("line_number")
        tables.append((fqn, fqn, datastore))
        for cls in classes:
            edges[(cls.key, fqn, datastore, line_number)] = {
                "from": cls.key,
                "to": fqn,
                "from_label": "Class",
                "to_label": "DbTable",
                "datastore": datastore,
                "line_number": line_number,
            }
    return list(edges.values()), tables


def build_function_table_edges(
    index: Any,
    matches: List[Tuple[Any, Any, str, str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Tuple[str, str, str]]]:
    """READS / WRITES edges from Function to DbTable, and the DbTable rows they need.

    ``matches`` holds (method name, method path, table fqn, operation, line number). The
    MATCH these replace needs a Function with that name and path; a table is created only
    for a matched function, and a row with no method name matches nothing. Edges are
    returned per operation, one per (operation, function, table, line_number).
    """
    edges: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    tables: List[Tuple[str, str, str]] = []
    for method_name, method_path, table, operation, line_number in matches:
        if not method_name:
            continue
        functions = index.entries("Function", method_name, method_path)
        if not functions:
            continue
        tables.append((table, table, "mysql"))
        for fn in functions:
            edges[(operation, fn.key, table, line_number)] = {
                "from": fn.key,
                "to": table,
                "from_label": "Function",
                "to_label": "DbTable",
                "line_number": line_number,
            }
    by_operation: Dict[str, List[Dict[str, Any]]] = {}
    for (operation, *_), edge in edges.items():
        by_operation.setdefault(operation, []).append(edge)
    return by_operation, tables


def build_decorated_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DECORATED_BY edges, resolved from the index.

    Decorated node: Function (name, path, line_number, and context when set) or Class
    (name, path, line_number). Decorator: Function by name and path. Labels are tried
    in order Function, then Class; the first label that matches stops the row, as in
    write_decorated_by_links.
    """
    edges: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for row in rows:
        decorated_line = row.get("decorated_line")
        if decorated_line is None:
            continue
        decorators = index.entries("Function", row["decorator_name"], row["decorator_path"])
        if not decorators:
            continue
        props = {"line_number": row.get("line_number", decorated_line)}
        for label in ("Function", "Class"):
            if label == "Function":
                context = row.get("decorated_context", "")
                if context is None:
                    continue
            else:
                context = ""
            decorated = [
                e for e in index.entries(label, row["decorated_name"], row["decorated_path"])
                if e.line_number == decorated_line and (not context or e.context == context)
            ]
            if not decorated:
                continue
            for node in decorated:
                for decorator in decorators:
                    edges[(node.key, decorator.key)] = _pair_edge(node, decorator, props)
            break
    return list(edges.values())


def _coalesce(value: Any, default: Any) -> Any:
    """Cypher coalesce(value, default): only a null value takes the default."""
    return default if value is None else value


def _last_wins(matches: List[Tuple[Any, Any, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Edges for (source, target, props) matches; one edge per endpoint pair, last props win.

    A MERGE on the endpoints keeps one relationship per pair, and the SET after it runs
    once per row, so the last row's properties are the ones stored.
    """
    edges: Dict[Tuple[Any, Any, str, str], Dict[str, Any]] = {}
    for source, target, props in matches:
        edges[(source.key, target.key, source.label, target.label)] = _pair_edge(source, target, props)
    return list(edges.values())


def build_row_pair_edges(
    index: Any,
    rows: List[Dict[str, Any]],
    child_default: str,
    parent_default: str,
    props_of: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Edges for rows that name both endpoints by (name, path) and carry their own labels.

    Child is (child_name, path) and parent is (parent_name, resolved_parent_file_path);
    the labels are the row's child_label / parent_label, else the defaults. Pairs the
    schema does not bind are dropped later by copy_grouped_edges.
    """
    matches = []
    for row in rows:
        child_label = row.get("child_label", child_default)
        parent_label = row.get("parent_label", parent_default)
        props = props_of(row)
        for child in index.entries(child_label, row["child_name"], row["path"]):
            for parent in index.entries(parent_label, row["parent_name"], row["resolved_parent_file_path"]):
                matches.append((child, parent, props))
    return _last_wins(matches)


def build_implements_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """IMPLEMENTS edges, defaults Struct to Interface; confidence_label defaults to INFERRED."""
    return build_row_pair_edges(
        index,
        rows,
        "Struct",
        "Interface",
        lambda row: {"confidence_label": _coalesce(row.get("confidence_label"), "INFERRED")},
    )


def _at_line(entries: List[Any], line: Any) -> List[Any]:
    """Entries whose line_number equals ``line`` (the MATCH line predicate)."""
    return [e for e in entries if e.line_number == line]


def build_companion_of_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """COMPANION_OF edges: Object (name, path, line) to Class (name, path, line), no properties."""
    matches = []
    for row in rows:
        companions = _at_line(
            index.entries("Object", row["companion_name"], row["companion_path"]), row["companion_line"]
        )
        owners = _at_line(index.entries("Class", row["owner_name"], row["owner_path"]), row["owner_line"])
        for companion in companions:
            for owner in owners:
                matches.append((companion, owner, {}))
    return _last_wins(matches)


def build_previews_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PREVIEWS edges: Function (name, path, preview line) to Function (name, path).

    line_number defaults to the preview line when the row has none (as the query did).
    """
    matches = []
    for row in rows:
        previews = _at_line(
            index.entries("Function", row["preview_name"], row["preview_path"]), row["preview_line"]
        )
        composables = index.entries("Function", row["composable_name"], row["composable_path"])
        props = {"line_number": row.get("line_number", row["preview_line"])}
        for preview in previews:
            for composable in composables:
                matches.append((preview, composable, props))
    return _last_wins(matches)


def build_inclusion_edges(
    index: Any,
    rows: List[Dict[str, Any]],
    file_path: str,
) -> Tuple[List[Dict[str, Any]], set]:
    """INCLUDES edges from classes in one file to modules, and the module names.

    The class matches by name and this file's path. A module is created only for a row
    whose class exists (the MERGE sat after the MATCH it replaces).
    """
    edges: List[Dict[str, Any]] = []
    modules: set = set()
    for row in rows:
        classes = index.entries("Class", row["class"], file_path)
        if not classes:
            continue
        module = str(row["module"])
        modules.add(module)
        for cls in classes:
            edges.append({"from": cls.key, "to": module, "from_label": "Class", "to_label": "Module"})
    return edges, modules


def build_part_of_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PART_OF edges: File (child_path) to File (parent_path); no properties."""
    matches = []
    for row in rows:
        child = index.file_entry(row["child_path"])
        parent = index.file_entry(row["parent_path"])
        if child is not None and parent is not None:
            matches.append((child, parent, {}))
    return _last_wins(matches)


def build_inject_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """INJECTS edges: injector Class (name, path) to injected Class matched by name only.

    The query this replaces matched the injected class by name across the whole graph,
    so every Class with that name gets an edge, as it does here.
    """
    matches = []
    for row in rows:
        props = {
            "field_name": row.get("field_name"),
            "inject_line": row.get("inject_line"),
            "confidence_label": "EXTRACTED",
        }
        injectors = index.entries("Class", row["injector_class"], row["injector_path"])
        injecteds = index.entries_named("Class", row["injected_class"])
        for injector in injectors:
            for injected in injecteds:
                matches.append((injector, injected, props))
    return _last_wins(matches)


def build_embeds_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """EMBEDS edges: Struct (name, path) to Struct (name, resolved parent path); line_number defaults to 0."""
    matches = []
    for row in rows:
        props = {"line_number": row.get("line_number", 0)}
        for child in index.entries("Struct", row["child_name"], row["path"]):
            for parent in index.entries("Struct", row["parent_name"], row["resolved_parent_file_path"]):
                matches.append((child, parent, props))
    return _last_wins(matches)


def build_partial_of_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PARTIAL_OF edges, defaults Class to Class; confidence_label defaults to INFERRED."""
    return build_row_pair_edges(
        index,
        rows,
        "Class",
        "Class",
        lambda row: {"confidence_label": _coalesce(row.get("confidence_label"), "INFERRED")},
    )


def build_metaclass_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """METACLASS edges: Class (name, path) to Class (name, resolved parent path)."""
    matches = []
    for row in rows:
        props = {
            "line_number": row.get("line_number", 0),
            "confidence_label": _coalesce(row.get("confidence_label"), "EXTRACTED"),
        }
        children = index.entries("Class", row["child_name"], row["path"])
        parents = index.entries("Class", row["parent_name"], row["resolved_parent_file_path"])
        for child in children:
            for parent in parents:
                matches.append((child, parent, props))
    return _last_wins(matches)


_BINDS_PAIRS = (("Interface", "Class"), ("Class", "Class"), ("Interface", "Interface"))


def build_binds_edges(index: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """BINDS edges, resolved from the index.

    Source and target match by name and path. The label pairs are tried in the order
    of write_binds_links; the first pair that matches both endpoints stops the row.
    """
    edges: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for row in rows:
        props = {
            "line_number": row.get("line_number"),
            "provider": row.get("provider"),
            "confidence_label": row.get("confidence_label") or "EXTRACTED",
        }
        for source_label, target_label in _BINDS_PAIRS:
            sources = index.entries(source_label, row["source_name"], row["source_path"])
            targets = index.entries(target_label, row["target_name"], row["target_path"])
            if not sources or not targets:
                continue
            for source in sources:
                for target in targets:
                    edges[(source.key, target.key)] = _pair_edge(source, target, props)
            break
    return list(edges.values())


# Phase 5 (inheritance re-resolution) on KùzuDB. The Cypher it replaces matched the
# caller by name, path and line (any label), and the new callee as a Function by name,
# path and line. A line predicate applies only when the value is an int.
INHERIT_RESOLVE_METHOD = "inheritance"
_CALL_REL_TYPES = ("CALLS", "HEURISTIC_CALLS")


def _inherit_callers(index: Any, name: str, path: str, line: Any) -> List[Any]:
    entries = index.entries_at(name, path)
    if isinstance(line, int):
        entries = [e for e in entries if e.line_number == line]
    return entries


def _inherit_callees(index: Any, name: str, path: str, line: Any) -> List[Any]:
    entries = index.entries("Function", name, path)
    if isinstance(line, int):
        entries = [e for e in entries if e.line_number == line]
    return entries


def build_inherit_reresolve_edges(
    index: Any,
    improvements: List[Dict[str, Any]],
) -> Tuple[List[Tuple[str, str, str, str, str]], List[Dict[str, Any]]]:
    """Plan phase 5 for Kùzu: the edges to delete and the tier 10/11 CALLS rows to COPY.

    Each improvement replaces the tier 8/9 CALLS / HEURISTIC_CALLS edges from its caller to
    any node named ``called_name`` (all of them, as the Cypher DELETE did), then adds one
    CALLS edge per (caller, new callee). Only improvements with a caller and a callee
    count. A repeated (caller, callee, called_name) keeps the last row's values, as the
    MERGE with SET did.

    Returns (deletes, edges). deletes holds (rel_label, from_label, from_key, to_label,
    to_key) for the old edges. edges are CALLS rows shaped like build_call_edges output.
    """
    old_by_source: Dict[Any, List[Tuple[str, Any]]] = {}
    for rel in _CALL_REL_TYPES:
        for edge in index.edges(rel):
            if edge.resolution_tier in (8, 9):
                old_by_source.setdefault(edge.source, []).append((rel, edge.target))

    deletes: Dict[Tuple[str, str, str, str, str], None] = {}
    edges: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for imp in improvements:
        callers = _inherit_callers(index, imp["caller_name"], imp["caller_path"], imp.get("caller_line"))
        callees = _inherit_callees(index, imp["called_name"], imp["new_called_path"], imp.get("new_called_line"))
        if not callers or not callees:
            continue
        called_name = imp["called_name"]
        for caller in callers:
            if caller.key is None:
                continue
            for rel, target in old_by_source.get(caller, []):
                if target.name == called_name and target.key is not None:
                    deletes[(rel, caller.label, caller.key, target.label, target.key)] = None
            for callee in callees:
                if callee.key is None:
                    continue
                key = (caller.label, caller.key, callee.label, callee.key, called_name)
                edges[key] = {
                    "from": caller.key,
                    "to": callee.key,
                    "from_label": caller.label,
                    "to_label": callee.label,
                    "line_number": imp.get("call_line"),
                    "args": None,
                    "full_call_name": None,
                    "args_key": None,
                    "confidence": imp["confidence"],
                    "resolution_tier": imp["resolution_tier"],
                    "confidence_label": None,
                    "source": None,
                    "resolution_method": INHERIT_RESOLVE_METHOD,
                    "called_name": called_name,
                }
    return list(deletes), list(edges.values())


def _delete_rel_pairs(
    session: Any,
    rel_label: str,
    predicate: str,
    pairs: List[Tuple[str, str, str, str]],
) -> None:
    """DELETE the rel_label edges between (from_label, from_key, to_label, to_key) pairs that match predicate.

    One UNWIND statement per (from label, to label) binding; endpoints match on their primary key.
    """
    if not pairs:
        return
    bindings = declared_bindings(session, rel_label)
    groups: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for from_label, from_key, to_label, to_key in pairs:
        if (from_label, to_label) in bindings:
            groups.setdefault((from_label, to_label), []).append({"from": from_key, "to": to_key})
    for (from_label, to_label), batch in groups.items():
        from_field = primary_key_field(from_label)
        to_field = primary_key_field(to_label)
        session.run(
            f"""
            UNWIND $batch AS row
            MATCH (a:`{from_label}` {{{from_field}: row.from}})-[r:`{rel_label}`]->(b:`{to_label}` {{{to_field}: row.to}})
            WHERE {predicate}
            DELETE r
            """,
            batch=batch,
        )


def apply_inherit_reresolve(session: Any, index: Any, improvements: List[Dict[str, Any]]) -> None:
    """Apply phase 5 on KùzuDB: DELETE the old tier 8/9 edges, then COPY the tier 10/11 CALLS rows.

    Retry-safe: before the COPY, CALLS edges this phase wrote earlier (resolution_method
    'inheritance') are removed for the same endpoint pairs, so running it twice leaves one
    edge per pair, as the MERGE did.
    """
    deletes, edges = build_inherit_reresolve_edges(index, improvements)
    if not edges:
        return
    info_logger(f"[INHERIT-RESOLVE] Kùzu bulk: {len(deletes)} old edge(s) removed, {len(edges)} new edge(s)")
    for rel in _CALL_REL_TYPES:
        pairs = [(fl, fk, tl, tk) for r, fl, fk, tl, tk in deletes if r == rel]
        _delete_rel_pairs(session, rel, "r.resolution_tier IN [8, 9]", pairs)
    owned = [(e["from_label"], e["from"], e["to_label"], e["to"]) for e in edges]
    _delete_rel_pairs(session, "CALLS", f"r.resolution_method = '{INHERIT_RESOLVE_METHOD}'", owned)
    copy_grouped_edges(session, "CALLS", edges, CALL_REL_PROPERTIES, declared_bindings(session, "CALLS"))


def _maven_uid(group_id: Any, artifact_id: Any) -> str:
    """MavenModule and ExternalLibrary primary key: group_id and artifact_id, no separator."""
    return f"{group_id or ''}{artifact_id or ''}"


def load_maven_graph(
    session: Any,
    modules: List[Dict[str, Any]],
    child_relations: List[Dict[str, Any]],
    inter_module_deps: List[Dict[str, Any]],
    external_libs: List[Dict[str, Any]],
    repo_path: str,
) -> None:
    """COPY the Maven build graph: MavenModule and ExternalLibrary nodes, then CHILD_MODULE,
    MODULE_DEPENDS_ON and USES_LIBRARY edges.

    Same semantics as the MERGE queries in write_maven_build_graph: nodes are keyed by
    (group_id, artifact_id) with the last row winning; edges match modules by artifact_id
    alone (every matching pair is linked); an ExternalLibrary node is created for every
    external dependency, and its edge only when the source module exists.
    """
    module_rows: Dict[str, Dict[str, Any]] = {}
    by_artifact: Dict[str, List[str]] = {}
    for m in modules:
        uid = _maven_uid(m.get("group_id"), m.get("artifact_id"))
        module_rows[uid] = {
            "uid": uid,
            "group_id": m.get("group_id"),
            "artifact_id": m.get("artifact_id"),
            "version": m.get("version"),
            "packaging": m.get("packaging"),
            "pom_path": m.get("pom_path"),
            "path": m.get("pom_path"),
            "repo_path": repo_path,
        }
        uids = by_artifact.setdefault(m.get("artifact_id"), [])
        if uid not in uids:
            uids.append(uid)

    lib_rows: Dict[str, Dict[str, Any]] = {}
    for lib in external_libs:
        uid = _maven_uid(lib.get("group_id"), lib.get("artifact_id"))
        lib_rows[uid] = {
            "uid": uid,
            "group_id": lib.get("group_id"),
            "artifact_id": lib.get("artifact_id"),
            "version": lib.get("version"),
        }

    child_edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rel in child_relations:
        for p in by_artifact.get(rel.get("parent_artifact_id"), []):
            for c in by_artifact.get(rel.get("child_artifact_id"), []):
                child_edges[(p, c)] = {"from": p, "to": c}

    dep_edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for dep in inter_module_deps:
        for s in by_artifact.get(dep.get("src_artifact_id"), []):
            for t in by_artifact.get(dep.get("tgt_artifact_id"), []):
                dep_edges[(s, t)] = {"from": s, "to": t, "configuration": None, "scope": dep.get("scope")}

    lib_edges: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for lib in external_libs:
        lib_uid = _maven_uid(lib.get("group_id"), lib.get("artifact_id"))
        for s in by_artifact.get(lib.get("src_artifact_id"), []):
            lib_edges[(s, lib_uid)] = {"from": s, "to": lib_uid, "configuration": None, "scope": lib.get("scope")}

    tmp_dir = tempfile.mkdtemp(prefix="cgc-maven-")
    try:
        if module_rows:
            copy_node_table(session, "MavenModule", list(module_rows.values()), tmp_dir)
        if lib_rows:
            copy_node_table(session, "ExternalLibrary", list(lib_rows.values()), tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    copy_rel_rows(session, "CHILD_MODULE", "MavenModule", "MavenModule", list(child_edges.values()), {})
    copy_rel_rows(
        session, "MODULE_DEPENDS_ON", "MavenModule", "MavenModule", list(dep_edges.values()),
        {"configuration": "STRING", "scope": "STRING"},
    )
    copy_rel_rows(
        session, "USES_LIBRARY", "MavenModule", "ExternalLibrary", list(lib_edges.values()),
        {"configuration": "STRING", "scope": "STRING"},
    )
