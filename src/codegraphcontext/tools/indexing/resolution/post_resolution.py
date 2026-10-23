# src/codegraphcontext/tools/indexing/resolution/post_resolution.py
"""Post-resolution pass: tighten low-confidence CALLS / HEURISTIC_CALLS edges using INHERITS graph.

This module is called AFTER the initial CALLS graph is written.  It looks for
edges with confidence < 0.5 (tiers 8 and 9) where the call target has a
unique implementor that can be determined from the INHERITS graph.

Algorithm for each low-confidence CALLS / HEURISTIC_CALLS edge (caller → called_name → same_file):
  1. Find all Function nodes with name = called_name across the codebase
  2. Find which of those are in classes that INHERIT from a common base
  3. If the caller file imports exactly one of those base types, resolve to
     the matching implementor
  4. Update the CALLS / HEURISTIC_CALLS edge: set called_file_path, confidence, resolution_tier=10

Tier 10 = inheritance-graph resolved (confidence: 0.78)

This pass requires an active Neo4j driver.  It is called from pipeline.py
after `writer.write_function_call_groups()`.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ....utils.debug_log import info_logger
from ..persistence.utils import get_backend_type, execute_read_operation, execute_write_operation

# Tier 10: resolved via inheritance graph (better than tier 8 first-match bias)
_TIER_INHERIT_CONFIDENCE = 0.78
_TIER_INHERIT = 10

# Tier 11: inheritance + embedding similarity
_TIER_EMBED_CONFIDENCE = 0.82
_TIER_EMBED = 11

# Receiver of a call such as `numberQueue.enqueue(1)`: a plain identifier before the last dot.
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
# Initializer that constructs a type: `new Queue<number>()`, `Queue(3)`, `ns.Queue()`.
_CONSTRUCTED_TYPE = re.compile(r"\s*(?:new\s+)?([A-Za-z_$][\w$.]*)\s*(?:<.*>)?\s*\(")


def _receiver_of(full_call_name: Any) -> Optional[str]:
    """The receiver identifier of a call, or None (e.g. `this.q.push` or a bare call)."""
    if not full_call_name:
        return None
    head = str(full_call_name).split("(", 1)[0].strip()
    if "." not in head:
        return None
    receiver = head.rsplit(".", 1)[0].strip()
    return receiver if _IDENTIFIER.fullmatch(receiver) else None


def _constructed_type(value: Any) -> Optional[str]:
    """The class a variable initializer constructs, or None.

    Only a capitalised last name counts as a type, so `x = factory.make(1)` gives no type.
    """
    if not value:
        return None
    match = _CONSTRUCTED_TYPE.match(str(value))
    if not match:
        return None
    name = match.group(1).rsplit(".", 1)[-1]
    return name if name[:1].isupper() else None


def _receiver_type(row: Dict[str, Any], decls: Dict[Tuple[str, str], List[Tuple[int, str]]]) -> Optional[str]:
    """Declared type of the receiver of a low-confidence call, or None when unknown.

    ``decls`` maps (variable name, path) to [(line_number, value)] sorted by line. The
    declaration used is the last one at or before the call line (the nearest preceding one).
    """
    receiver = _receiver_of(row.get("full_call_name"))
    if receiver is None:
        return None
    entries = decls.get((receiver, row["caller_path"]))
    if not entries:
        return None
    call_line = row.get("call_line")
    chosen = None
    for line, value in entries:
        if call_line is None or line <= call_line:
            chosen = value
    return _constructed_type(chosen)


def _row_order_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    """Fixed order for phase-5 rows, independent of the backend's result order."""
    return (
        row["caller_path"] or "",
        row["caller_name"] or "",
        row["caller_line"] or 0,
        row["call_line"] or 0,
        row["called_name"] or "",
    )


def _candidate_order(impl: Dict[str, Any]) -> Tuple[Any, ...]:
    """Fixed order for candidate implementations: path, then line, then class."""
    line = impl.get("line_number")
    return (
        impl.get("path") or "",
        line if line is not None else -1,
        impl.get("class_name") or "",
        impl.get("parent_name") or "",
    )


def run_inheritance_reresolve(
    driver: Any,
    repo_path: str,
    vector_resolver: Optional[Any] = None,
    symbol_index: Optional[Any] = None,
) -> int:
    """Re-resolve low-confidence CALLS / HEURISTIC_CALLS edges using INHERITS graph + optional embeddings.

    With ``symbol_index`` (repo init), the two reads come from the in-memory index
    and the writes still go to ``driver``. Without it, both reads query the database.

    Returns the number of edges that were improved.
    """
    t0 = time.time()
    improved = 0

    # Normalise repo_path to a directory prefix so STARTS WITH is safe:
    # "/opt/repos/myapp" must not accidentally match "/opt/repos/myapp_extra".
    repo_path_prefix = repo_path.rstrip("/") + "/"

    # Step 1: find all low-confidence same-file CALLS / HEURISTIC_CALLS edges (tier 8 or 9)
    # These are edges where the resolver gave up and pointed to the caller's file.
    backend = get_backend_type(driver)
    if symbol_index is not None:
        low_confidence = symbol_index.low_confidence_call_rows(repo_path_prefix)
    else:
        def _read_work_1(session):
            return list(session.run(
                """
                MATCH (caller)-[c:CALLS|HEURISTIC_CALLS]->(called)
                WHERE (caller.path STARTS WITH $repo_path_prefix
                       OR called.path STARTS WITH $repo_path_prefix)
                  AND c.resolution_tier IN [8, 9]
                RETURN
                    caller.name AS caller_name,
                    caller.path AS caller_path,
                    caller.line_number AS caller_line,
                    called.name AS called_name,
                    c.line_number AS call_line,
                    c.full_call_name AS full_call_name,
                    c.args AS args
                """,
                repo_path_prefix=repo_path_prefix,
            ))
        low_confidence = execute_read_operation(driver, backend, _read_work_1)

    info_logger(f"[INHERIT-RESOLVE] Found {len(low_confidence)} low-confidence edges to re-examine")
    if not low_confidence:
        return 0

    # The last row applied sets the call line of the one CALLS edge per (caller, callee),
    # so the row order must not depend on the backend.
    low_confidence = sorted(low_confidence, key=_row_order_key)

    # Step 2: batch-fetch all candidate implementations in a single query.
    # Using UNWIND + IN instead of N separate round-trips prevents session
    # timeouts on large repos and is orders-of-magnitude faster.
    name_to_impls: Dict[str, List[Dict[str, Any]]] = {}

    # Guard: skip edges whose called_name is null/empty (e.g. calls to external Modules)
    unique_names: Set[str] = {
        row["called_name"] for row in low_confidence if row["called_name"]
    }

    _NAMES_BATCH = 500  # stay well under Cypher parameter-list limits
    unique_names_list = list(unique_names)
    if symbol_index is not None:
        for row in symbol_index.class_method_candidate_rows(repo_path_prefix, unique_names_list):
            name_to_impls.setdefault(row["queried_name"], []).append(dict(row))
    else:
        backend = get_backend_type(driver)
        def _read_work_2(session):
            local_impls = {}
            for _i in range(0, len(unique_names_list), _NAMES_BATCH):
                chunk = unique_names_list[_i : _i + _NAMES_BATCH]
                result = session.run(
                    """
                    UNWIND $names AS name
                    MATCH (cls:Class)-[:CONTAINS]->(fn:Function {name: name})
                    WHERE fn.path STARTS WITH $repo_path_prefix
                    OPTIONAL MATCH (cls)-[:INHERITS]->(parent:Class)
                    RETURN fn.name AS queried_name, fn.path AS path,
                           fn.line_number AS line_number,
                           cls.name AS class_name, cls.qualified_name AS class_qname,
                           parent.name AS parent_name, parent.qualified_name AS parent_qname
                    """,
                    names=chunk,
                    repo_path_prefix=repo_path_prefix,
                )
                for row in result:
                    local_impls.setdefault(row["queried_name"], []).append(dict(row))
            return local_impls

        name_to_impls.update(execute_read_operation(driver, backend, _read_work_2))

    # Candidates in a fixed order, whatever order the backend returned them in.
    for impls in name_to_impls.values():
        impls.sort(key=_candidate_order)

    # Step 2b: declared receiver types. For each (receiver, caller file) pair named by a
    # low-confidence call, read the Variable declarations of that name in that file.
    receiver_keys: Set[Tuple[str, str]] = set()
    for row in low_confidence:
        receiver = _receiver_of(row.get("full_call_name"))
        if receiver is not None and row["caller_path"]:
            receiver_keys.add((receiver, row["caller_path"]))

    decls: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
    if symbol_index is not None:
        for name, path in receiver_keys:
            for entry in symbol_index.entries("Variable", name, path):
                decls.setdefault((name, path), []).append((entry.line_number or 0, entry.value))
    elif receiver_keys:
        backend = get_backend_type(driver)
        receiver_pairs = [{"name": name, "path": path} for name, path in sorted(receiver_keys)]

        def _read_work_var(session):
            local_decls: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
            for _i in range(0, len(receiver_pairs), _NAMES_BATCH):
                chunk = receiver_pairs[_i : _i + _NAMES_BATCH]
                result = session.run(
                    """
                    UNWIND $pairs AS p
                    MATCH (v:Variable {name: p.name, path: p.path})
                    RETURN v.name AS name, v.path AS path,
                           v.line_number AS line_number, v.value AS value
                    """,
                    pairs=chunk,
                )
                for row in result:
                    local_decls.setdefault((row["name"], row["path"]), []).append(
                        (row["line_number"] or 0, row["value"] or "")
                    )
            return local_decls

        decls.update(execute_read_operation(driver, backend, _read_work_var))
    for entries in decls.values():
        entries.sort()

    # Step 3: for each low-confidence edge, check if INHERITS narrows candidates
    # Strategy:
    #   a) If only 1 candidate exists across the entire repo → use it (tier 10)
    #   b) If multiple exist → filter to those in a class that INHERITS from something
    #      (prefer polymorphic implementations over utility helpers)
    #   c) If vector_resolver available → use embedding similarity to pick the best
    improvements: List[Dict[str, Any]] = []

    for row in low_confidence:
        called_name = row["called_name"]
        if not called_name:  # skip null/empty call targets (external module refs etc.)
            continue
        caller_path = row["caller_path"]
        impls = name_to_impls.get(called_name, [])

        # Skip if no candidates or all candidates are the caller itself
        candidates = [impl for impl in impls if impl["path"] != caller_path]
        if not candidates:
            continue

        best_path = None
        best_line = None
        confidence = _TIER_INHERIT_CONFIDENCE
        tier = _TIER_INHERIT

        receiver_type = _receiver_type(row, decls)
        typed = [c for c in candidates if receiver_type and c.get("class_name") == receiver_type]

        if typed:
            # The receiver's declared type names the class: `numberQueue` built with
            # `new Queue()` takes Queue.enqueue, not another class's enqueue.
            best_path = typed[0]["path"]
            best_line = typed[0].get("line_number")
        elif len(candidates) == 1:
            # Unambiguous: single implementation outside the caller file
            best_path = candidates[0]["path"]
            best_line = candidates[0].get("line_number")
        else:
            # Multiple candidates — prefer those that are part of an INHERITS hierarchy
            inheriting = [c for c in candidates if c.get("parent_name")]
            pool = inheriting if inheriting else candidates

            if len(pool) == 1:
                best_path = pool[0]["path"]
                best_line = pool[0].get("line_number")
            elif vector_resolver is not None:
                # Use embedding similarity to disambiguate
                vec_path = vector_resolver.resolve(
                    called_name=called_name,
                    caller_qualified_name=None,
                    candidate_paths=[c["path"] for c in pool],
                    repo_path=repo_path,
                )
                if vec_path:
                    best_path = vec_path
                    best_line = next((c.get("line_number") for c in pool if c["path"] == vec_path), None)
                    confidence = _TIER_EMBED_CONFIDENCE
                    tier = _TIER_EMBED
            # else: too ambiguous without vector — skip

        if best_path is None:
            continue

        improvements.append({
            "caller_path": caller_path,
            "caller_name": row["caller_name"],
            "caller_line": row["caller_line"],
            "called_name": called_name,
            "call_line": row["call_line"],
            "new_called_path": best_path,
            "new_called_line": best_line,
            "confidence": confidence,
            "resolution_tier": tier,
        })

    if not improvements:
        info_logger(f"[INHERIT-RESOLVE] No improvable edges found in {time.time()-t0:.1f}s")
        return 0

    info_logger(f"[INHERIT-RESOLVE] Re-resolving {len(improvements)} edges...")

    backend = get_backend_type(driver)

    # Repo init on KùzuDB: the Cypher MERGE below cannot create a CALLS edge with
    # unlabelled endpoints, so the edges are deleted and COPYed in bulk instead.
    if symbol_index is not None and backend in ("kuzudb", "ladybugdb"):
        from ..persistence.bulk_kuzu import apply_inherit_reresolve, pyarrow_available

        if pyarrow_available():
            def _bulk_write_work(session):
                apply_inherit_reresolve(session, symbol_index, improvements)

            execute_write_operation(driver, backend, _bulk_write_work)
            info_logger(
                f"[INHERIT-RESOLVE] Improved {len(improvements)} CALLS / HEURISTIC_CALLS edges in {time.time()-t0:.1f}s"
            )
            return len(improvements)

    batch_size = 500

    def _make_write_query(caller_has_line: bool, called_has_line: bool) -> str:
        # Every literal Cypher map here needs doubled braces — this is an
        # f-string, so a single `{name: ...}` is parsed as a replacement field
        # and raised NameError before the query ever reached the driver, which
        # the caller logged as "Post-resolution failed (skipping)".
        #
        # The line predicates are baked in per bucket rather than tested at
        # runtime against a possibly-null row value, which is what the
        # caller_has_line / called_has_line bucketing exists to enable.
        caller_line = ", line_number: row.caller_line" if caller_has_line else ""
        called_line = ", line_number: row.new_called_line" if called_has_line else ""
        return f"""
                UNWIND $batch AS row
                MATCH (caller {{name: row.caller_name, path: row.caller_path{caller_line}}})
                MATCH (new_called:Function {{name: row.called_name, path: row.new_called_path{called_line}}})
                OPTIONAL MATCH (caller)-[old_edge:CALLS|HEURISTIC_CALLS]->(old_called {{name: row.called_name}})
                  WHERE old_edge.resolution_tier IN [8, 9]
                DELETE old_edge
                WITH caller, new_called, row
                MERGE (caller)-[c:CALLS {{called_name: row.called_name}}]->(new_called)
                SET c.line_number = coalesce(row.call_line, c.line_number),
                    c.confidence = row.confidence,
                    c.resolution_tier = row.resolution_tier,
                    c.resolution_method = 'inheritance'
                """

    def _write_work(session):
        local_improved = 0
        for i in range(0, len(improvements), batch_size):
            batch = improvements[i : i + batch_size]
            # Partition into 4 groups based on what line numbers are available
            buckets = {(True, True): [], (True, False): [], (False, True): [], (False, False): []}
            for row in batch:
                key = (
                    row.get("caller_line") is not None and isinstance(row.get("caller_line"), int),
                    row.get("new_called_line") is not None and isinstance(row.get("new_called_line"), int),
                )
                buckets[key].append(row)
            for (caller_has_line, called_has_line), sub_batch in buckets.items():
                if not sub_batch:
                    continue
                session.run(_make_write_query(caller_has_line, called_has_line), batch=sub_batch)
            local_improved += len(batch)
        return local_improved
    improved += execute_write_operation(driver, backend, _write_work)

    info_logger(
        f"[INHERIT-RESOLVE] Improved {improved} CALLS / HEURISTIC_CALLS edges in {time.time()-t0:.1f}s"
    )
    return improved
