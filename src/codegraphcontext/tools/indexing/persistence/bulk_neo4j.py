# src/codegraphcontext/tools/indexing/persistence/bulk_neo4j.py
"""Bulk node and edge loading for Neo4j through driver-side UNWIND batches.

Repo initialization collects the same rows as the KùzuDB path (see
:class:`BulkCollector`) and writes them here with ``UNWIND $batch AS row MERGE ...``
queries, in batches. Nothing is written to CSV and no offline import is used.

Merge keys follow the standard MERGE writers, not the Kùzu primary key: positional
labels merge on (name, path, line_number, occurrence_index), name-only labels on
name, Parameter on (name, path, function_line_number). The ``uid`` the collector
adds for Kùzu is dropped, because Neo4j nodes never carry it.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

from ....utils.debug_log import info_logger
from .bulk_kuzu import BulkCollector

BATCH_SIZE = 1000

# Mirrors writer._NAME_ONLY_MERGE_LABELS (a test keeps the two in step).
NAME_ONLY_LABELS = frozenset({"Module", "DbTable", "ExternalClass"})

_UID_FIELD = "uid"


def merge_keys(label: str) -> Tuple[str, ...]:
    """Property names that identify one node of ``label`` in the standard MERGE queries."""
    if label in NAME_ONLY_LABELS:
        return ("name",)
    if label == "Parameter":
        return ("name", "path", "function_line_number")
    return ("name", "path", "line_number", "occurrence_index")


def _clean(row: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the Kùzu-only uid and null values. SET n += row with a null removes the
    property, and the standard writers never send nulls (they pop them), so skipping
    them keeps existing values as the standard path does."""
    return {k: v for k, v in row.items() if k != _UID_FIELD and v is not None}


def _batches(rows: List[Any]) -> Iterator[List[Any]]:
    for start in range(0, len(rows), BATCH_SIZE):
        yield rows[start:start + BATCH_SIZE]


class Neo4jBulkCollector(BulkCollector):
    """BulkCollector whose flush writes through UNWIND batches instead of COPY."""

    def __init__(self) -> None:
        super().__init__()
        # (function identity, parameter identity) pairs for HAS_PARAMETER, as property maps
        self._param_links: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    def add_parameters(self, file_path: str, params: List[Dict[str, Any]]) -> None:
        super().add_parameters(file_path, params)
        for p in params:
            fn = {
                "name": p["func_name"],
                "path": file_path,
                "line_number": p["line_number"],
                "occurrence_index": p["occurrence_index"],
            }
            par = {"name": p["arg_name"], "path": file_path, "function_line_number": p["line_number"]}
            self._param_links.append((fn, par))

    def flush(self, session: Any) -> None:
        """Same order as BulkCollector.flush: nodes per label, CONTAINS, HAS_PARAMETER,
        IMPORTS, then the deferred per-file work."""
        for label, table in self._nodes.items():
            _merge_nodes(session, label, [_clean(r) for r in table.values()])
            info_logger(f"[BULK] Merged {len(table)} {label} node(s) (Neo4j)")
        _write_contains(session, self._contains, self._nodes)
        _write_has_parameter(session, self._param_links)
        _write_imports(session, self._import_edges)
        for work in self._deferred:
            work(session)


def _merge_nodes(session: Any, label: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    keys = merge_keys(label)
    match = ", ".join(f"{k}: row.{k}" for k in keys)
    query = f"""
        UNWIND $batch AS row
        MERGE (n:`{label}` {{{match}}})
        SET n += row
    """
    for batch in _batches(rows):
        session.run(query, batch=batch)


def _write_contains(
    session: Any,
    contains: List[Tuple[str, str, str]],
    nodes: Dict[str, Dict[str, Dict[str, Any]]],
) -> None:
    """File -> node CONTAINS edges. The collector keys nodes by Kùzu uid, so each edge's
    end is looked up through the stored node and matched on its Neo4j merge keys."""
    by_label: Dict[str, List[Dict[str, Any]]] = {}
    for file_path, label, key in contains:
        node = nodes[label][key]
        row = {"file_path": file_path}
        row.update({k: node.get(k) for k in merge_keys(label)})
        by_label.setdefault(label, []).append(row)
    for label, rows in by_label.items():
        match = ", ".join(f"{k}: row.{k}" for k in merge_keys(label))
        query = f"""
            UNWIND $batch AS row
            MATCH (f:File {{path: row.file_path}})
            MATCH (n:`{label}` {{{match}}})
            MERGE (f)-[:CONTAINS]->(n)
        """
        for batch in _batches(rows):
            session.run(query, batch=batch)


def _write_has_parameter(session: Any, links: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
    rows = [
        {
            "fn_name": fn["name"], "fn_path": fn["path"],
            "fn_line": fn["line_number"], "fn_occurrence": fn["occurrence_index"],
            "p_name": par["name"], "p_path": par["path"], "p_line": par["function_line_number"],
        }
        for fn, par in links
    ]
    query = """
        UNWIND $batch AS row
        MATCH (fn:Function {name: row.fn_name, path: row.fn_path,
                               line_number: row.fn_line, occurrence_index: row.fn_occurrence})
        MATCH (p:Parameter {name: row.p_name, path: row.p_path, function_line_number: row.p_line})
        MERGE (fn)-[:HAS_PARAMETER]->(p)
    """
    for batch in _batches(rows):
        session.run(query, batch=batch)


def _write_imports(session: Any, import_edges: Dict[Tuple[Any, ...], Dict[str, Any]]) -> None:
    """IMPORTS edges with the same MERGE patterns and SET clauses as _emit_side_rows:
    JavaScript rows merge on line_number only, the other rows on line_number and
    imported_name."""
    js_rows: List[Dict[str, Any]] = []
    other_rows: List[Dict[str, Any]] = []
    for key, edge in import_edges.items():
        row = {
            "file_path": edge["from"],
            "module_name": edge["to"],
            "line_number": edge["line_number"],
            "imported_name": edge["imported_name"],
            "alias": edge["alias"],
            "full_import_name": edge["full_import_name"],
            "lang": edge["lang"],
        }
        (js_rows if key[0] == "js" else other_rows).append(row)

    js_query = """
        UNWIND $batch AS row
        MATCH (f:File {path: row.file_path})
        MERGE (m:Module {name: row.module_name})
        MERGE (f)-[r:IMPORTS {line_number: row.line_number}]->(m)
        SET r.imported_name = row.imported_name,
            r.alias = row.alias,
            r.lang = row.lang
    """
    other_query = """
        UNWIND $batch AS row
        MATCH (f:File {path: row.file_path})
        MATCH (m:Module {name: row.module_name})
        MERGE (f)-[r:IMPORTS {line_number: row.line_number, imported_name: row.imported_name}]->(m)
        SET r.alias = coalesce(row.alias, ""),
            r.full_import_name = row.full_import_name,
            r.lang = row.lang
    """
    for batch in _batches(js_rows):
        session.run(js_query, batch=batch)
    for batch in _batches(other_rows):
        session.run(other_query, batch=batch)


# ── ORM, query and MyBatis links ─────────────────────────────────────────────
# These run after the node flush, so their endpoints already exist. The queries are
# the ones the standard writers run (writer.write_orm_mappings, write_query_links,
# write_mybatis_links), with the same MATCH and MERGE keys, in UNWIND batches.

_MAPS_TO_QUERY = """
    UNWIND $batch AS m
    MATCH (c:Class {name: m.class_name, path: m.class_path})
    MERGE (tbl:DbTable {fqn: m.orm_table})
    SET tbl.name = m.orm_table,
        tbl.datasource_name = COALESCE(tbl.datasource_name, m.datastore)
    MERGE (c)-[:MAPS_TO {datastore: m.datastore, line_number: m.line_number}]->(tbl)
"""

# READS / WRITES from @Query methods: the standard MERGE carries line_number.
_QUERY_EDGE_QUERY = """
    UNWIND $batch AS q
    MATCH (fn:Function {name: q.method_name, path: q.method_path})
    MERGE (tbl:DbTable {fqn: q.table_name})
    SET tbl.name = q.table_name,
        tbl.datasource_name = COALESCE(tbl.datasource_name, 'mysql')
    MERGE (fn)-[:%s {line_number: q.line_number}]->(tbl)
"""

# READS / WRITES from MyBatis mappers: the standard MERGE has no line_number, so an
# existing edge of the same type between the function and the table is reused.
_MYBATIS_EDGE_QUERY = """
    UNWIND $batch AS q
    MATCH (fn:Function {name: q.method_name, path: q.method_path})
    MERGE (tbl:DbTable {fqn: q.table_name})
    SET tbl.name = q.table_name,
        tbl.datasource_name = COALESCE(tbl.datasource_name, 'mysql')
    MERGE (fn)-[:%s]->(tbl)
"""


def write_maps_to(session: Any, class_table_rows: List[Dict[str, Any]]) -> None:
    """MAPS_TO edges Class -> DbTable from class_table ORM rows."""
    rows = [
        {
            "class_name": r.get("class_name"),
            "class_path": r.get("class_path"),
            "orm_table": r.get("orm_table"),
            "datastore": r.get("datastore"),
            "line_number": r.get("line_number"),
        }
        for r in class_table_rows
        if r.get("kind") == "class_table"
    ]
    for batch in _batches(rows):
        session.run(_MAPS_TO_QUERY, batch=batch)


def write_function_table_edges(
    session: Any,
    edges: List[Dict[str, Any]],
    *,
    with_line_number: bool,
) -> None:
    """READS / WRITES edges Function -> DbTable. ``edges`` are the dicts the standard
    writers build: method_name, method_path, table_name, operation, line_number."""
    for operation in ("READS", "WRITES"):
        op_rows = [
            {
                "method_name": e["method_name"],
                "method_path": e["method_path"],
                "table_name": e["table_name"],
                **({"line_number": e.get("line_number", 0)} if with_line_number else {}),
            }
            for e in edges
            if e["operation"] == operation
        ]
        if not op_rows:
            continue
        template = _QUERY_EDGE_QUERY if with_line_number else _MYBATIS_EDGE_QUERY
        query = template % operation
        for batch in _batches(op_rows):
            session.run(query, batch=batch)
