from codegraphcontext.tools.indexing.persistence import bulk_kuzu
from codegraphcontext.tools.indexing.persistence.bulk_kuzu import (
    apply_inherit_reresolve,
    build_inherit_reresolve_edges,
)
from codegraphcontext.tools.indexing.symbol_index import SymbolIndex

REPO = "/repo"


def _index():
    """A caller with two low-confidence edges to `log` (tier 8 and 9), and one tier 1 edge."""
    index = SymbolIndex()
    index.register("Function", "caller", f"{REPO}/a.ts", 10, key="caller-key")
    index.register("Function", "log", f"{REPO}/a.ts", 3, key="log-local")
    index.register("Function", "log", f"{REPO}/b.ts", 20, key="log-remote")
    index.register("Function", "other", f"{REPO}/a.ts", 30, key="other-key")
    caller = index.entries("Function", "caller", f"{REPO}/a.ts")[0]
    local = index.entries("Function", "log", f"{REPO}/a.ts")[0]
    index.add_edge("CALLS", caller, local, line_number=11, resolution_tier=8, args_key="[]")
    index.add_edge("CALLS", caller, local, line_number=12, resolution_tier=9, args_key="[]")
    index.add_edge("CALLS", caller, index.entries("Function", "other", f"{REPO}/a.ts")[0],
                   line_number=13, resolution_tier=1, args_key="[]")
    return index


def _imp(**overrides):
    imp = {
        "caller_name": "caller",
        "caller_path": f"{REPO}/a.ts",
        "caller_line": 10,
        "called_name": "log",
        "call_line": 12,
        "new_called_path": f"{REPO}/b.ts",
        "new_called_line": 20,
        "confidence": 0.78,
        "resolution_tier": 10,
    }
    imp.update(overrides)
    return imp


def test_old_tier_8_9_edges_to_the_called_name_are_deleted_once_per_pair():
    deletes, _ = build_inherit_reresolve_edges(_index(), [_imp()])
    # Two tier 8/9 edges share one (caller, local log) pair; the tier 1 edge is kept.
    assert deletes == [("CALLS", "Function", "caller-key", "Function", "log-local")]


def test_new_edge_is_a_tier_10_calls_row_with_inheritance_method():
    _, edges = build_inherit_reresolve_edges(_index(), [_imp()])
    assert len(edges) == 1
    edge = edges[0]
    assert (edge["from"], edge["to"], edge["from_label"], edge["to_label"]) == (
        "caller-key", "log-remote", "Function", "Function",
    )
    assert edge["resolution_tier"] == 10
    assert edge["confidence"] == 0.78
    assert edge["line_number"] == 12
    assert edge["resolution_method"] == "inheritance"
    assert edge["called_name"] == "log"


def test_repeated_caller_callee_pair_keeps_the_last_call_line():
    rows = [_imp(call_line=11, confidence=0.78), _imp(call_line=12, confidence=0.82, resolution_tier=11)]
    _, edges = build_inherit_reresolve_edges(_index(), rows)
    assert len(edges) == 1
    assert edges[0]["line_number"] == 12
    assert edges[0]["resolution_tier"] == 11
    assert edges[0]["confidence"] == 0.82


def test_no_caller_or_no_callee_writes_nothing():
    assert build_inherit_reresolve_edges(_index(), [_imp(caller_line=99)]) == ([], [])
    assert build_inherit_reresolve_edges(_index(), [_imp(new_called_path=f"{REPO}/zz.ts")]) == ([], [])


def test_callee_line_is_a_predicate_only_when_given():
    rows = [_imp(new_called_line=None)]
    _, edges = build_inherit_reresolve_edges(_index(), rows)
    assert [e["to"] for e in edges] == ["log-remote"]


def test_caller_line_none_matches_any_line_at_the_path():
    _, edges = build_inherit_reresolve_edges(_index(), [_imp(caller_line=None)])
    assert len(edges) == 1


class _Session:
    """Records each statement and the relationship COPY calls, without a database."""

    def __init__(self):
        self.statements = []

    def run(self, query, **params):
        self.statements.append((query, params))

        class _Result:
            def data(self_inner):
                return []

        return _Result()


def test_apply_deletes_old_edges_then_copies_new_ones(monkeypatch):
    calls = []
    monkeypatch.setattr(bulk_kuzu, "declared_bindings", lambda session, rel: {("Function", "Function")})
    monkeypatch.setattr(
        bulk_kuzu, "copy_grouped_edges",
        lambda session, rel, edges, props, bindings: calls.append((rel, len(edges))),
    )
    session = _Session()
    apply_inherit_reresolve(session, _index(), [_imp()])

    deletes = [q for q, _ in session.statements if "DELETE r" in q]
    assert any("r.resolution_tier IN [8, 9]" in q for q in deletes)
    assert any("r.resolution_method = 'inheritance'" in q for q in deletes)
    assert calls == [("CALLS", 1)]
    # Every statement run directly is a delete; the new edges go through copy_grouped_edges.
    assert len(session.statements) == len(deletes)


def test_apply_on_empty_plan_touches_nothing(monkeypatch):
    def _no_copy(*args, **kwargs):
        raise AssertionError("COPY must not run for an empty plan")

    monkeypatch.setattr(bulk_kuzu, "copy_grouped_edges", _no_copy)
    session = _Session()
    apply_inherit_reresolve(session, _index(), [_imp(caller_line=99)])
    assert session.statements == []
