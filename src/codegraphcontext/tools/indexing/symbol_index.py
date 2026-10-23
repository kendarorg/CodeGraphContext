# src/codegraphcontext/tools/indexing/symbol_index.py
"""In-memory record of the nodes written during a repo initialization.

Only the repo-init path builds one. The standard indexing path passes no index
and keeps querying the database. The writer registers every node it writes, so
by the time the link phases run, the index holds the same (label, name, path,
line_number) identities the database holds, and those phases can look nodes up
here instead of issuing MATCH queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class NodeEntry:
    """One written node, as the MATCH queries it answers would see it."""

    label: str
    line_number: Optional[int]
    context: str
    class_context: str
    module_context: str
    # Primary key in the backend (Kùzu uid, Module name, File path).
    key: Optional[str]
    name: str = ""
    path: str = ""
    # Variable nodes only: the initializer text (the `value` property), used by phase 5
    # to read a receiver's declared type.
    value: str = ""


@dataclass(frozen=True)
class EdgeEntry:
    """One written relationship, with the properties the link phases read."""

    rel_type: str
    source: NodeEntry
    target: NodeEntry
    line_number: Optional[int] = None
    full_call_name: Optional[str] = None
    args: Any = None
    confidence: Any = None
    resolution_tier: Any = None


_CALL_REL_TYPES = ("CALLS", "HEURISTIC_CALLS")

# Labels whose call MATCH has no line_number predicate (mirrors bulk_kuzu).
_CALL_LABELS_WITHOUT_LINE = {"File", "Directory"}


def relationship_for_tier(tier: Any) -> str:
    """CALLS or HEURISTIC_CALLS, as write_function_call_groups chooses it."""
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        tier_int = -1
    return "HEURISTIC_CALLS" if tier_int >= 8 else "CALLS"


class SymbolIndex:
    """Lookup table of written nodes, keyed by (name, path), plus written edges."""

    def __init__(self) -> None:
        self._labels: set[str] = set()
        # (name, path) -> [NodeEntry] in write order
        self._by_name_path: Dict[Tuple[str, str], List[NodeEntry]] = {}
        # name -> [NodeEntry] in write order, for MATCHes that give no path
        self._by_name: Dict[str, List[NodeEntry]] = {}
        # MERGE identity -> EdgeEntry. A repeated identity replaces the earlier
        # properties, as the SET clauses after a MERGE did.
        self._edges: Dict[Tuple[Any, ...], EdgeEntry] = {}

    def register(
        self,
        label: str,
        name: str,
        path: str,
        line_number: Optional[int] = None,
        context: str = "",
        *,
        key: Optional[str] = None,
        class_context: str = "",
        module_context: str = "",
        value: str = "",
    ) -> None:
        """Record a node the writer has just written."""
        self._labels.add(label)
        entry = NodeEntry(
            label=label,
            line_number=line_number,
            context=context or "",
            class_context=class_context or "",
            module_context=module_context or "",
            key=key,
            name=name,
            path=path,
            value=value or "",
        )
        self._by_name_path.setdefault((name, path), []).append(entry)
        self._by_name.setdefault(name, []).append(entry)

    def add_edge(
        self,
        rel_type: str,
        source: NodeEntry,
        target: NodeEntry,
        *,
        line_number: Optional[int] = None,
        full_call_name: Optional[str] = None,
        args: Any = None,
        args_key: Optional[str] = None,
        confidence: Any = None,
        resolution_tier: Any = None,
    ) -> None:
        """Record a relationship the writer has just written (same identity as its MERGE)."""
        edge = EdgeEntry(
            rel_type=rel_type,
            source=source,
            target=target,
            line_number=line_number,
            full_call_name=full_call_name,
            args=args,
            confidence=confidence,
            resolution_tier=resolution_tier,
        )
        self._edges[(rel_type, source, target, line_number, full_call_name, args_key)] = edge

    def edges(self, rel_type: str) -> List[EdgeEntry]:
        """All written edges of one type, in write order."""
        return [e for e in self._edges.values() if e.rel_type == rel_type]

    def low_confidence_call_rows(
        self, repo_path_prefix: str, tiers: Tuple[int, ...] = (8, 9)
    ) -> List[Dict[str, Any]]:
        """Rows shaped like the phase-5 CALLS / HEURISTIC_CALLS read query.

        Phase 5 writes one MERGE per (caller, callee) pair without a line predicate,
        so when a caller has several low-confidence edges to one name, the row read
        last sets the call line. The database does not fix that order (two standard
        runs disagree on it), so rows are returned in write order.
        """
        rows: List[Dict[str, Any]] = []
        for edge in self._edges.values():
            if edge.rel_type not in _CALL_REL_TYPES or edge.resolution_tier not in tiers:
                continue
            src, dst = edge.source, edge.target
            if not (src.path.startswith(repo_path_prefix) or dst.path.startswith(repo_path_prefix)):
                continue
            rows.append({
                "caller_name": src.name,
                "caller_path": src.path,
                "caller_line": src.line_number,
                "called_name": dst.name,
                "call_line": edge.line_number,
                "full_call_name": edge.full_call_name,
                "args": edge.args,
            })
        return rows

    def class_method_candidate_rows(
        self, repo_path_prefix: str, names: List[str]
    ) -> List[Dict[str, Any]]:
        """Rows shaped like the phase-5 (Class)-[CONTAINS]->(Function) read query.

        One row per (class, function, parent) combination, or one row with no
        parent when the class has no INHERITS edge to a Class.
        """
        wanted = set(names)
        parents: Dict[NodeEntry, List[NodeEntry]] = {}
        for edge in self._edges.values():
            if edge.rel_type == "INHERITS" and edge.source.label == "Class" and edge.target.label == "Class":
                parents.setdefault(edge.source, []).append(edge.target)
        rows: List[Dict[str, Any]] = []
        for edge in self._edges.values():
            if edge.rel_type != "CONTAINS":
                continue
            cls, fn = edge.source, edge.target
            if cls.label != "Class" or fn.label != "Function":
                continue
            if fn.name not in wanted or not fn.path.startswith(repo_path_prefix):
                continue
            for parent in parents.get(cls) or [None]:
                rows.append({
                    "queried_name": fn.name,
                    "path": fn.path,
                    "line_number": fn.line_number,
                    "class_name": cls.name,
                    "class_qname": None,
                    "parent_name": parent.name if parent is not None else None,
                    "parent_qname": None,
                })
        return rows

    def entries_with_label(self, label: str) -> List[NodeEntry]:
        """All nodes with this label, whatever their name or path."""
        return [e for entries in self._by_name_path.values() for e in entries if e.label == label]

    def entries(self, label: str, name: str, path: str) -> List[NodeEntry]:
        """All nodes with this label at (name, path), in write order."""
        return [e for e in self._by_name_path.get((name, path), []) if e.label == label]

    def entries_at(self, name: str, path: str) -> List[NodeEntry]:
        """All nodes at (name, path), whatever their label, in write order.

        The MATCH on ``{name, path}`` with no label that phase 5 uses for its caller.
        """
        return list(self._by_name_path.get((name, path), []))

    def entries_named(self, label: str, name: str) -> List[NodeEntry]:
        """All nodes with this label and name, whatever their path, in write order."""
        return [e for e in self._by_name.get(name, []) if e.label == label]

    def file_entry(self, path: str) -> Optional[NodeEntry]:
        """The File node for an absolute path, or None.

        File nodes are unique per path, so this is the MATCH on ``{path: ...}``.
        """
        for entry in self._by_name_path.get(self._file_key(path), []):
            if entry.label == "File":
                return entry
        return None

    @staticmethod
    def _file_key(path: str) -> Tuple[str, str]:
        # File entries are registered under (basename, path); look them up the same way.
        return (path.rsplit("/", 1)[-1], path)

    def has_label(self, label: str) -> bool:
        """True when at least one node with this label was written."""
        return label in self._labels

    def labels_for(self, name: str, path: str) -> List[str]:
        """Distinct labels of the nodes at (name, path), in write order."""
        seen: List[str] = []
        for entry in self._by_name_path.get((name, path), []):
            if entry.label not in seen:
                seen.append(entry.label)
        return seen

    def has_node(
        self,
        label: str,
        name: str,
        path: str,
        line_number: Optional[int] = None,
        context: Optional[str] = None,
    ) -> bool:
        """True when a node with this label and (name, path) exists.

        ``line_number`` and ``context`` are checked only when given, mirroring
        the optional predicates in the equivalent MATCH queries.
        """
        for entry in self._by_name_path.get((name, path), []):
            if entry.label != label:
                continue
            if line_number is not None and entry.line_number != line_number:
                continue
            if context is not None and context != "" and entry.context != context:
                continue
            return True
        return False

    def __len__(self) -> int:
        return sum(len(entries) for entries in self._by_name_path.values())


def _context_matches(called_label: str, entry: NodeEntry, called_context: str) -> bool:
    """The context predicate of write_function_call_groups (see _called_context_clause)."""
    if called_label == "Function":
        return called_context in (entry.context, entry.class_context, entry.module_context)
    if called_label == "Variable":
        return called_context == entry.context
    return True


def add_call_edges(
    index: SymbolIndex,
    rows: List[Dict[str, Any]],
    caller_label: str = "Function",
    called_label: str = "Function",
) -> None:
    """Record the CALLS / HEURISTIC_CALLS edges a call row group creates.

    Endpoints are matched the way the MATCH in write_function_call_groups matches
    them, so only edges the database would also hold are recorded.
    """
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
            if called_context and not _context_matches(called_label, e, called_context):
                continue
            callees.append(e)

        rel_type = relationship_for_tier(row.get("resolution_tier"))
        for caller in callers:
            for callee in callees:
                index.add_edge(
                    rel_type,
                    caller,
                    callee,
                    line_number=row.get("line_number"),
                    full_call_name=row.get("full_call_name"),
                    args=row.get("args"),
                    args_key=row.get("args_key"),
                    confidence=row.get("confidence"),
                    resolution_tier=row.get("resolution_tier"),
                )


def add_contains_edges(index: SymbolIndex, class_fn_rows: List[Dict[str, Any]], file_path: str) -> None:
    """Record Class-[:CONTAINS]->Function edges for one file's class_fn rows."""
    for row in class_fn_rows:
        func_line = row.get("func_line")
        if func_line is None:
            continue
        class_line = row.get("class_line", -1)
        classes = [
            c for c in index.entries("Class", row["class_name"], file_path)
            if class_line is None or class_line < 0 or c.line_number == class_line
        ]
        functions = [f for f in index.entries("Function", row["func_name"], file_path) if f.line_number == func_line]
        for cls in classes:
            for fn in functions:
                index.add_edge("CONTAINS", cls, fn)


def add_inherit_edges(index: SymbolIndex, rows: List[Dict[str, Any]]) -> None:
    """Record Class-[:INHERITS]->Class edges for internal-parent inheritance rows.

    Only Class to Class pairs are recorded: the phase-5 read is the only consumer.
    External parents (ExternalClass) and other labels are not recorded.
    """
    for row in rows:
        if row.get("resolved_parent_file_path") == "__external__":
            continue
        if (row.get("child_label") or "Class") != "Class" or (row.get("parent_label") or "Class") != "Class":
            continue
        children = index.entries("Class", row["child_name"], row["path"])
        if not children:
            continue
        parents = index.entries("Class", row["parent_name"], row["resolved_parent_file_path"])
        for child in children:
            for parent in parents:
                index.add_edge("INHERITS", child, parent)


_CSHARP_INHERIT_CHILD_LABELS = ("Class", "Record", "Interface", "Mixin", "Extension")
_CSHARP_INHERIT_PARENT_LABELS = ("Class", "Record", "Interface", "Mixin", "Extension")
_CSHARP_TYPE_LISTS = (
    ("classes", "Class"),
    ("structs", "Struct"),
    ("records", "Record"),
    ("interfaces", "Interface"),
)


def add_csharp_inherit_edges(index: SymbolIndex, file_data: Dict[str, Any], file_path: str) -> None:
    """Record the INHERITS edges of one C# file, as _create_csharp_inheritance_and_interfaces MERGEs them.

    Mirrors that method's branching: a base that names a local interface, or a
    non-first base of a class, becomes IMPLEMENTS (not recorded here); every other
    base becomes INHERITS. The child is matched by (name, path), the parent by name
    over every path, as the Cypher MATCHes do.
    """
    interface_names = {iface["name"] for iface in file_data.get("interfaces", [])}
    for type_list_name, type_label in _CSHARP_TYPE_LISTS:
        for type_item in file_data.get(type_list_name, []):
            bases = type_item.get("bases")
            if not bases:
                continue
            for base_str in bases:
                base_name = base_str.split("<")[0].strip()
                base_index = bases.index(base_str)
                if base_name in interface_names or (base_index > 0 and type_label == "Class"):
                    continue
                for child_label in _CSHARP_INHERIT_CHILD_LABELS:
                    children = index.entries(child_label, type_item["name"], file_path)
                    if not children:
                        continue
                    for parent_label in _CSHARP_INHERIT_PARENT_LABELS:
                        for parent in index.entries_named(parent_label, base_name):
                            for child in children:
                                index.add_edge("INHERITS", child, parent)


_CPP_EXTS = (".cpp", ".cc", ".cxx", ".c++", ".C")
_CPP_CONTAINER_LABELS = ("Class", "Struct", "Module")


def add_cpp_class_function_edges(index: SymbolIndex, repo_path: str) -> None:
    """Record the Class-[:CONTAINS]->Function edges of write_cpp_class_function_links.

    A C++ Function under the repo with a class_context, in a C++ source file, is
    linked to every Class, Struct or Module of that name under the repo (any file),
    as the Cypher MATCHes do. ``repo_path`` is the normalized repo path, matched as a
    prefix exactly as STARTS WITH does.
    """
    for fn in index.entries_with_label("Function"):
        if not fn.class_context or not fn.path.startswith(repo_path) or not fn.path.endswith(_CPP_EXTS):
            continue
        for container_label in _CPP_CONTAINER_LABELS:
            for container in index.entries_named(container_label, fn.class_context):
                if container.path.startswith(repo_path):
                    index.add_edge("CONTAINS", container, fn)


# Identity fields the Kùzu wrapper concatenates into `uid` for code nodes. Mirrors
# `uid_map` in core/database_embedded_kuzu.py; the concatenation has no separator.
KUZU_UID_PARTS: Dict[str, Tuple[str, ...]] = {
    "Function": ("name", "path", "line_number", "occurrence_index"),
    "Class": ("name", "path", "line_number", "occurrence_index"),
    "Variable": ("name", "path", "line_number", "occurrence_index"),
    "Trait": ("name", "path", "line_number", "occurrence_index"),
    "Interface": ("name", "path", "line_number", "occurrence_index"),
    "Macro": ("name", "path", "line_number", "occurrence_index"),
    "Struct": ("name", "path", "line_number", "occurrence_index"),
    "Enum": ("name", "path", "line_number", "occurrence_index"),
    "EnumMember": ("name", "path", "line_number", "occurrence_index"),
    "Union": ("name", "path", "line_number", "occurrence_index"),
    "Annotation": ("name", "path", "line_number"),
    "Record": ("name", "path", "line_number", "occurrence_index"),
    "Property": ("name", "path", "line_number", "occurrence_index"),
    "Parameter": ("name", "path", "function_line_number"),
    "Mixin": ("name", "path", "line_number", "occurrence_index"),
    "Extension": ("name", "path", "line_number", "occurrence_index"),
    "Object": ("name", "path", "line_number", "occurrence_index"),
}


def kuzu_uid(label: str, props: Dict[str, Any]) -> Optional[str]:
    """Kùzu primary key for a code node, or None when the label is keyed by name/path.

    ``occurrence_index`` defaults to 0 when absent, as in the Kùzu wrapper.
    """
    parts = KUZU_UID_PARTS.get(label)
    if parts is None:
        return None
    return "".join(str(props.get(p, 0 if p == "occurrence_index" else "")) for p in parts)
