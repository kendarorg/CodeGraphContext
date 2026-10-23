"""Phase 5 target selection: receiver declared type first, then a fixed tie-break.

Fixture shape (from sample_project_typescript): index.ts calls numberQueue.enqueue, where
numberQueue is declared with `new Queue<number>()`. Queue.enqueue and
PriorityQueue.enqueue are both in functions-generics.ts, so a path-only choice cannot
tell them apart.
"""

from unittest.mock import MagicMock

import pytest

from codegraphcontext.tools.indexing.resolution import post_resolution
from codegraphcontext.tools.indexing.symbol_index import (
    SymbolIndex,
    add_call_edges,
    add_contains_edges,
)

REPO = "/repo"
CALLER = "/repo/src/index.ts"
QUEUE_FILE = "/repo/src/functions-generics.ts"


def _add_classes(index, order=("Queue", "PriorityQueue")):
    """Register the two queue classes and their enqueue methods, in the given order."""
    specs = {
        "Queue": (216, 219),
        "PriorityQueue": (245, 248),
    }
    for cls in order:
        class_line, fn_line = specs[cls]
        index.register("Class", cls, QUEUE_FILE, class_line, key=f"{cls}{class_line}")
        index.register("Function", "enqueue", QUEUE_FILE, fn_line, key=f"enqueue{fn_line}")
        add_contains_edges(
            index,
            [{"class_name": cls, "class_line": class_line, "func_name": "enqueue", "func_line": fn_line}],
            QUEUE_FILE,
        )


def _calls(index, call_lines):
    """Three-ish low-confidence calls to numberQueue.enqueue, in the given order."""
    for line in call_lines:
        add_call_edges(
            index,
            [{
                "caller_name": "demonstrate",
                "caller_file_path": CALLER,
                "caller_line_number": 100,
                "called_name": "enqueue",
                "called_file_path": CALLER,
                "called_line_number": 0,
                "called_context": "",
                "line_number": line,
                "full_call_name": "numberQueue.enqueue(1)",
                "args": ["1"],
                "args_key": f'["1"]{line}',
                "confidence": 0.4,
                "resolution_tier": 9,
            }],
            "Function", "Function",
        )


def _index(declared="new Queue<number>()", class_order=("Queue", "PriorityQueue"), call_lines=(112, 113, 114)):
    index = SymbolIndex()
    index.register("File", "index.ts", CALLER, key=CALLER)
    index.register("Function", "demonstrate", CALLER, 100, key="demonstrate100")
    # Same-name fallback target the resolver left behind (tier 8/9 points into the caller file).
    index.register("Function", "enqueue", CALLER, 50, key="enqueue50")
    if declared is not None:
        index.register("Variable", "numberQueue", CALLER, 108, key="numberQueue108", value=declared)
    _add_classes(index, class_order)
    _calls(index, call_lines)
    return index


def _fake_driver():
    driver = MagicMock()
    driver.get_backend_type = lambda: "neo4j"
    return driver


def _fail(*args, **kwargs):
    raise AssertionError("unexpected database access")


def _capture_writes(monkeypatch):
    writes = []

    def fake_write(driver, backend, work):
        session = MagicMock()
        session.run.side_effect = lambda query, **kw: writes.append(kw.get("batch"))
        return work(session)

    monkeypatch.setattr(post_resolution, "execute_write_operation", fake_write)
    return writes


def _targets(batch):
    return {(row["new_called_path"], row["new_called_line"]) for row in batch}


# ---- helpers -------------------------------------------------------------------

@pytest.mark.parametrize("full_call_name, expected", [
    ("numberQueue.enqueue(1)", "numberQueue"),
    ("numberQueue.enqueue", "numberQueue"),
    ("this.queue.push(1)", None),
    ("push(1)", None),
    (None, None),
])
def test_receiver_of_call(full_call_name, expected):
    assert post_resolution._receiver_of(full_call_name) == expected


@pytest.mark.parametrize("value, expected", [
    ("new Queue<number>()", "Queue"),
    ("new Map<string, Queue<number>>()", "Map"),
    ("ns.Queue(3)", "Queue"),
    ("Queue(3)", "Queue"),
    ("factory.make(1)", None),
    ("makeQueue", None),
    ("", None),
    (None, None),
])
def test_constructed_type_from_initializer(value, expected):
    assert post_resolution._constructed_type(value) == expected


# ---- receiver-type choice (index path) ------------------------------------------

def test_declared_receiver_type_selects_queue_enqueue(monkeypatch):
    index = _index(declared="new Queue<number>()")
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)

    assert post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index) == 3
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 219)}


def test_declared_receiver_type_selects_priority_queue_enqueue(monkeypatch):
    index = _index(declared="new PriorityQueue<number>()")
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index)
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 248)}


def test_receiver_type_wins_over_vector_choice(monkeypatch):
    index = _index(declared="new Queue<number>()")
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)
    vector = MagicMock()
    vector.resolve.return_value = QUEUE_FILE

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, vector, symbol_index=index)
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 219)}
    vector.resolve.assert_not_called()


def test_unknown_receiver_type_falls_back_to_vector_choice(monkeypatch):
    index = _index(declared=None)
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)
    vector = MagicMock()
    vector.resolve.return_value = QUEUE_FILE

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, vector, symbol_index=index)
    (batch,) = writes
    # Both candidates share the chosen file; the lowest line wins.
    assert _targets(batch) == {(QUEUE_FILE, 219)}


def test_type_with_no_matching_class_falls_back(monkeypatch):
    index = _index(declared="new Stack<number>()")
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)
    vector = MagicMock()
    vector.resolve.return_value = QUEUE_FILE

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, vector, symbol_index=index)
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 219)}


def test_declaration_after_call_is_not_used(monkeypatch):
    # The Variable is declared at line 108; the calls are at 112+. A later declaration
    # at line 200 must not be used for calls above it.
    index = _index(declared="new PriorityQueue<number>()")
    index.register("Variable", "numberQueue", CALLER, 200, key="numberQueue200", value="new Queue<number>()")
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index)
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 248)}


# ---- deterministic tie-break ----------------------------------------------------

@pytest.mark.parametrize("class_order", [("Queue", "PriorityQueue"), ("PriorityQueue", "Queue")])
def test_tie_break_is_independent_of_registration_order(monkeypatch, class_order):
    index = _index(declared=None, class_order=class_order)
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)
    vector = MagicMock()
    vector.resolve.return_value = QUEUE_FILE

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, vector, symbol_index=index)
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 219)}


def test_calls_sorted_by_call_line_whatever_the_registration_order(monkeypatch):
    index = _index(declared="new Queue<number>()", call_lines=(114, 112, 113))
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)

    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index)
    (batch,) = writes
    # The last row applied sets the call line of the single CALLS edge: always the highest.
    assert [row["call_line"] for row in batch] == [112, 113, 114]


# ---- Neo4j path must choose the same target --------------------------------------

def test_cypher_path_matches_index_path(monkeypatch):
    caller_rows = [
        {"caller_name": "demonstrate", "caller_path": CALLER, "caller_line": 100,
         "called_name": "enqueue", "call_line": line, "full_call_name": "numberQueue.enqueue(1)",
         "args": ["1"]}
        for line in (114, 112, 113)
    ]
    candidate_rows = [
        {"queried_name": "enqueue", "path": QUEUE_FILE, "line_number": 248, "class_name": "PriorityQueue",
         "class_qname": None, "parent_name": None, "parent_qname": None},
        {"queried_name": "enqueue", "path": QUEUE_FILE, "line_number": 219, "class_name": "Queue",
         "class_qname": None, "parent_name": None, "parent_qname": None},
    ]
    variable_rows = [
        {"name": "numberQueue", "path": CALLER, "line_number": 108, "value": "new Queue<number>()"},
    ]

    def run(query, **kwargs):
        if "Variable" in query:
            return variable_rows
        if "CONTAINS" in query:
            return candidate_rows
        return caller_rows

    session = MagicMock()
    session.run.side_effect = run

    def fake_read(driver, backend, work):
        return work(session)

    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", fake_read)

    assert post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None) == 3
    (batch,) = writes
    assert _targets(batch) == {(QUEUE_FILE, 219)}
    assert [row["call_line"] for row in batch] == [112, 113, 114]


def test_cypher_and_index_paths_produce_identical_batches(monkeypatch):
    index = _index(declared="new Queue<number>()", call_lines=(114, 112, 113))
    writes = _capture_writes(monkeypatch)
    monkeypatch.setattr(post_resolution, "execute_read_operation", _fail)
    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None, symbol_index=index)
    (index_batch,) = writes

    caller_rows = [
        {"caller_name": "demonstrate", "caller_path": CALLER, "caller_line": 100,
         "called_name": "enqueue", "call_line": line, "full_call_name": "numberQueue.enqueue(1)",
         "args": ["1"]}
        for line in (114, 112, 113)
    ]
    candidate_rows = [
        {"queried_name": "enqueue", "path": QUEUE_FILE, "line_number": 248, "class_name": "PriorityQueue",
         "class_qname": None, "parent_name": None, "parent_qname": None},
        {"queried_name": "enqueue", "path": QUEUE_FILE, "line_number": 219, "class_name": "Queue",
         "class_qname": None, "parent_name": None, "parent_qname": None},
    ]

    def run(query, **kwargs):
        if "Variable" in query:
            return [{"name": "numberQueue", "path": CALLER, "line_number": 108, "value": "new Queue<number>()"}]
        if "CONTAINS" in query:
            return candidate_rows
        return caller_rows

    session = MagicMock()
    session.run.side_effect = run
    writes.clear()
    monkeypatch.setattr(post_resolution, "execute_read_operation", lambda driver, backend, work: work(session))
    post_resolution.run_inheritance_reresolve(_fake_driver(), REPO, None)
    (cypher_batch,) = writes
    assert cypher_batch == index_batch
