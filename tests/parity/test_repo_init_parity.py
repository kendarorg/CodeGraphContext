"""repo_init must write the same graph as the standard index path, on each backend.

Every index run is a subprocess with its own database, so the embedded engines
stay isolated (see test_verify_databases_parity.py). The graph is compared as a
sorted multiset of nodes and relationships: paths are made relative to the
fixture, and indexed_at is never read.
"""
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from test_verify_databases_parity import _probe_embedded_backend

pytestmark = pytest.mark.parity

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = REPO_ROOT / "tests" / "fixtures" / "sample_projects"
SERVER_BACKENDS = ("neo4j", "nornic")
BACKENDS = ("kuzudb", "ladybugdb", "neo4j", "nornic")
# Fixtures with a bulk path for at least one backend (repo_init.md, "Verified state").
FIXTURES = (
    "sample_project",
    "sample_project_cpp",
    "sample_project_csharp",
    "sample_project_go",
    "sample_project_java",
    "sample_project_jpa",
    "sample_project_kotlin",
    "sample_project_maven",
    "sample_project_mybatis",
    "sample_project_typescript",
)

# Runs inside the subprocess: index one fixture in one mode and dump the graph.
DUMP_SCRIPT = r'''
import asyncio, json, sys
from pathlib import Path

from codegraphcontext.core import get_database_manager
from codegraphcontext.core.jobs import JobManager
from codegraphcontext.tools.graph_builder import GraphBuilder
from codegraphcontext.tools.indexing.repo_init import initialize_repository
from codegraphcontext.tools.indexing.schema_contract import NODE_LABELS, RELATIONSHIP_TYPES

mode, backend, project, db_path, out = sys.argv[1:6]
project = Path(project).resolve()
base = project.as_posix().rstrip("/")


def relative(value):
    if value is None:
        return None
    value = str(value).replace("\\", "/")
    if value == base:
        return "."
    return value[len(base) + 1:] if value.startswith(base + "/") else value


def identity_of(label, path, name, line, occurrence):
    return [label, relative(path), name, line, occurrence]


async def main():
    db_mgr = get_database_manager(db_path=db_path)
    driver = db_mgr.get_driver()
    if backend in ("neo4j", "nornic"):
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    builder = GraphBuilder(db_mgr, JobManager(), asyncio.get_running_loop())
    if mode == "standard":
        await builder.build_graph_from_path_async(project)
    else:
        await initialize_repository(builder, project)

    embedded = backend in ("kuzudb", "ladybugdb")
    nodes, edges, errors = [], [], []
    identities = {}
    with driver.session() as session:
        # ExternalClass is written by the writers but is not in NODE_LABELS;
        # edges to it must still resolve to an identity.
        labels, rel_types = sorted(NODE_LABELS | {"ExternalClass"}), sorted(RELATIONSHIP_TYPES)
        if embedded:
            # Every table the schema declares: an edge endpoint can carry a
            # label that the contract lists nowhere. Types in the contract that
            # no table declares are left out, since querying them is a binder error.
            kinds = {row["name"]: row["type"] for row in session.run(
                "CALL show_tables() RETURN name, type").data()}
            labels = sorted(name for name, kind in kinds.items() if kind == "NODE")
            rel_types = sorted(name for name, kind in kinds.items() if kind == "REL")
        # Node ids are the join key for embedded edges: Kùzu and LadybugDB
        # have no property named like the others on every table.
        for label in labels:
            try:
                columns = {"path", "name", "line_number", "occurrence_index"}
                if embedded:
                    # A property missing from a table is a binder error on
                    # Kùzu and LadybugDB; select only what the table declares.
                    declared = {row["name"] for row in session.run(
                        f"CALL table_info('{label}') RETURN name").data()}
                    columns &= declared
                select = ", ".join(
                    f"n.{c} AS {c}" if c in columns else f"NULL AS {c}"
                    for c in ("path", "name", "line_number", "occurrence_index"))
                for rec in session.run(f"MATCH (n:`{label}`) RETURN id(n) AS nid, {select}"):
                    row = dict(rec)
                    identity = identity_of(label, row["path"], row["name"], row["line_number"],
                                           row["occurrence_index"])
                    identities[str(row["nid"])] = identity
                    nodes.append(json.dumps(identity, sort_keys=True))
            except Exception as exc:
                errors.append(f"node {label}: {exc}")
        for rel in rel_types:
            try:
                if embedded:
                    for rec in session.run(f"MATCH (a)-[:`{rel}`]->(b) RETURN id(a) AS aid, id(b) AS bid"):
                        row = dict(rec)
                        ends = [identities.get(str(row[key])) for key in ("aid", "bid")]
                        if None in ends:
                            # An endpoint outside the dumped labels; ids differ between runs, so fail loudly.
                            errors.append(f"relationship {rel}: endpoint not in dumped labels")
                            continue
                        edges.append(json.dumps([rel, ends[0], ends[1]], sort_keys=True))
                    continue
                for rec in session.run(
                    f"MATCH (a)-[:`{rel}`]->(b) RETURN labels(a)[0] AS alabel, a.path AS apath, "
                    "a.name AS aname, a.line_number AS aline, a.occurrence_index AS aoccurrence, "
                    "labels(b)[0] AS blabel, b.path AS bpath, b.name AS bname, "
                    "b.line_number AS bline, b.occurrence_index AS boccurrence"
                ):
                    row = dict(rec)
                    edges.append(json.dumps([
                        rel,
                        identity_of(row["alabel"], row["apath"], row["aname"], row["aline"], row["aoccurrence"]),
                        identity_of(row["blabel"], row["bpath"], row["bname"], row["bline"], row["boccurrence"]),
                    ], sort_keys=True))
            except Exception as exc:
                errors.append(f"relationship {rel}: {exc}")
    db_mgr.close_driver()

    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"nodes": sorted(nodes), "edges": sorted(edges), "errors": errors}, fh)


asyncio.run(main())
'''


def _unavailable_reason(backend):
    """Why a backend cannot run here, or None. Mirrors the parity module's checks."""
    import importlib.util

    package = {"kuzudb": "kuzu", "ladybugdb": "ladybug", "neo4j": "neo4j", "nornic": "neo4j"}[backend]
    if importlib.util.find_spec(package) is None:
        return f"{backend}: {package} driver not installed."
    if backend in ("kuzudb", "ladybugdb"):
        probe_error = _probe_embedded_backend(package)
        if probe_error is not None:
            return f"{backend}: native backend unusable -> {probe_error}"
    return None


def _dump(backend, mode, project, tmp_path, backend_env, script):
    """Index one fixture in one mode in a subprocess and return the dumped graph."""
    db_path = tmp_path / f"{backend}-{mode}-{project.name}"
    out = tmp_path / f"{backend}-{mode}-{project.name}.json"
    env = {
        **os.environ,
        **backend_env,
        "CGC_RUNTIME_DB_TYPE": backend,
        # The copy sits outside the default workspace; the sandbox must accept it.
        "CGC_ALLOWED_ROOTS": str(project.parent),
    }
    result = subprocess.run(
        [sys.executable, str(script), mode, backend, str(project), str(db_path), str(out)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        pytest.fail(f"{backend} {mode} index of {project.name} failed:\n{result.stderr[-4000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def _describe_difference(label, standard, repo_init):
    only_standard = list((Counter(standard) - Counter(repo_init)).items())[:20]
    only_repo_init = list((Counter(repo_init) - Counter(standard)).items())[:20]
    return (
        f"{label} differ. standard-only (first 20): {only_standard}\n"
        f"repo_init-only (first 20): {only_repo_init}"
    )


@pytest.fixture(scope="module")
def dump_script(tmp_path_factory):
    script = tmp_path_factory.mktemp("parity_dump") / "dump_index.py"
    script.write_text(DUMP_SCRIPT, encoding="utf-8")
    return script


@pytest.mark.parametrize("fixture", FIXTURES)
@pytest.mark.parametrize("backend", BACKENDS)
def test_repo_init_matches_standard_path(backend, fixture, tmp_path, request, dump_script):
    reason = _unavailable_reason(backend)
    if reason is not None:
        pytest.skip(reason)
    backend_env = request.getfixturevalue(f"{backend}_env") if backend in SERVER_BACKENDS else {}
    # Index a copy: indexing writes a .cgcignore into the directory it scans.
    project = tmp_path / fixture
    shutil.copytree(SAMPLE_ROOT / fixture, project)

    standard = _dump(backend, "standard", project, tmp_path, backend_env, dump_script)
    repo_init = _dump(backend, "repo_init", project, tmp_path, backend_env, dump_script)

    assert standard["nodes"], "standard path wrote no nodes; the comparison would be empty"
    assert not standard["errors"], f"standard path query errors: {standard['errors']}"
    assert not repo_init["errors"], f"repo_init query errors: {repo_init['errors']}"
    assert standard["nodes"] == repo_init["nodes"], _describe_difference(
        "nodes", standard["nodes"], repo_init["nodes"])
    assert standard["edges"] == repo_init["edges"], _describe_difference(
        "edges", standard["edges"], repo_init["edges"])
