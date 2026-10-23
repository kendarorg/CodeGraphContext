# Repo initialization (`repo_init`): handoff

## Purpose

Index a repository directory into a graph that holds no data for it yet. The whole
directory is parsed, the nodes and relationships are resolved from an in-memory
`SymbolIndex`, and on KùzuDB they are written with bulk `COPY` instead of per-file
`MERGE` queries. The standard `cgc index` path is unchanged: it passes no index and
runs the original queries.

Entry point: `initialize_repository(graph_builder, path, ...)` in
`src/codegraphcontext/tools/indexing/repo_init.py`. It rejects paths that are not
directories, paths outside the allowed roots, and repositories that already have a
`Repository` node (use `cgc index` to re-index).

## Files

| File | Role |
|---|---|
| `tools/indexing/repo_init.py` | Entry point. Creates the index, a writer with bulk enabled, and runs the pipeline. |
| `tools/indexing/symbol_index.py` | `SymbolIndex`: written nodes keyed by `(name, path)`, with key and context fields. `kuzu_uid` implements the Kùzu primary-key rule. |
| `tools/indexing/persistence/bulk_kuzu.py` | `BulkCollector` (node, Parameter and IMPORTS rows; flush order), Parquet `COPY` (`copy_rel_rows`, `copy_grouped_edges`), and the edge builders (`build_call_edges`, `build_inherit_edges`, `build_decorated_edges`, `build_binds_edges`). |
| `tools/indexing/persistence/writer.py` | `GraphWriter`. `add_file_to_graph` builds rows with `_build_label_rows`, `_import_rows` and `_emit_side_rows`. Link writers check `_use_index_writes(backend)` and take the index/COPY branch when it holds. |
| `tools/indexing/pipeline.py` | Calls `writer.flush_bulk()` after the per-file loop, when bulk is enabled. Phases 4 and 5 are skipped in repo-init mode. |

Bulk mode is enabled only when the backend is `kuzudb` and `pyarrow` imports
(`GraphWriter.enable_bulk`). Otherwise the standard writers run.

## Kùzu rules that matter

- **Primary key `uid`** for code nodes (Function, Class, Interface, Object, Parameter,
  etc.) is the concatenation of the identity fields from `uid_map`, with
  `occurrence_index` defaulting to 0 and no separator. Implemented by `kuzu_uid`.
  Module is keyed by `name`; File by `path`.
- **Lists in COPY:** use Parquet. Kùzu's CSV reader mis-parses `STRING[]` elements that
  contain commas, brackets or quotes; Parquet handles them.
- **Parquet types:** boolean is `pyarrow.bool_`, not `pyarrow.bool`.
- **Relationship COPY** takes columns named `from` and `to` with the endpoint primary
  keys, plus the properties by name, and `COPY R FROM path (from="A", to="B")`. It works
  for groups and plain tables.
- **Bindings:** `CALL show_connection('<REL>')` returns the declared
  `(source table, destination table)` pairs. Writers use it to skip pairs the schema does
  not bind, which the old queries also skipped (binder exception).
- **Table columns:** `CALL table_info('<Label>')` gives column names and types for node
  tables. Relationship groups do not expose their properties this way; the property
  lists are written in code (`CALL_REL_PROPERTIES`, and the dicts at each call site).
- **Backend name** is `kuzudb`, not `kuzu`.

## Verified state

Standard path (MERGE) and repo-init on Kùzu are identical, node and edge sets compared
exactly, on six fixtures after every change:

| Fixture | Nodes | Edges |
|---|---|---|
| sample_project | 518 | 672 |
| sample_project_kotlin | 244 | 318 |
| sample_project_typescript | 918 | 1497 |
| sample_project_go | 674 | 910 |
| sample_project_java | 159 | 224 |
| sample_project_cpp | 144 | 189 |

Each conversion was also checked with a spy that counts rows sent through COPY on a
fixture that has the type, so an identical result cannot hide a branch that never ran.
Unit suite: 1741 passed, 8 skipped, 0 failed. `ruff check --select F821,F811,F401` is clean.

Coverage by relationship type (fixture where the spy confirmed COPY):

| Type | Status | Confirmed on |
|---|---|---|
| CONTAINS (File to node) | COPY | all |
| CALLS, HEURISTIC_CALLS: Function to Function | COPY | sample_project |
| CALLS: Function to Class, Interface, Object, File; File to Function, Class, Interface | COPY | typescript |
| CALLS: Function to Parameter; File to Object | query (no binding; see `_INDEXED_CALL_GROUPS`) | - |
| INHERITS (internal parents) | COPY | kotlin, java |
| DECORATED_BY | COPY | typescript |
| BINDS | COPY | kotlin |
| HAS_PARAMETER | COPY (in the Parameter node load) | sample_project |
| IMPORTS | COPY (rows applied at add time) | sample_project, typescript |

## Not converted yet (still MATCH queries after the node load)

- INHERITS to `ExternalClass` (creates nodes; needs its own node step).
- PREVIEWS, INCLUDES, IMPLEMENTS, METACLASS, COMPANION_OF, EMBEDS, PART_OF,
  PARTIAL_OF, INJECTS.
- Directory CONTAINS (Repository/Directory/File chain; uses query paths in `add_file_to_graph`).
- Spring (INJECTS, EXPOSES_ENDPOINT, PROVIDES_BEAN), build (MODULE_DEPENDS_ON,
  USES_LIBRARY, CHILD_MODULE, FILE_BELONGS_TO), datasource (READS, WRITES, MAPS_TO,
  HAS_COLUMN, STORED_IN), ORM, query and MyBatis writers. No parity fixtures exist for
  most of these; add fixtures before converting them.
- `write_cpp_class_function_links`, `repair_missing_contains_links` (post-load repairs).

## Neo4j

- **No bulk writer.** Repo-init on Neo4j runs the standard MERGE writers. Neo4j results
  so far cover the standard path only, so they say nothing about the new code.
- The decided approach (see Decisions) is driver-side `UNWIND` batches, no CSV. Indexes
  on `(name, path)` and the identity keys exist for the main labels.
- To verify Neo4j on the new code, the same row builders must feed `UNWIND` writers, and
  the Neo4j results must match the standard path on the same fixtures.
- Test container: `cgc-neo4j-test` (Neo4j 5.15.0) on `127.0.0.1:7687`, user `neo4j`,
  password `cgctestpw`. Local test only. Remove with `docker rm -f cgc-neo4j-test`.

## Open items

1. **Phase 5, inheritance re-resolution** (`resolution/post_resolution.py`): reads tier
   8/9 CALLS and candidate implementations from the database. Needs an index version.
   Off by default (`ENABLE_INHERIT_RESOLVE=false`).
2. **Phase 4, embeddings** (`vector_resolver.py`, `embeddings.py`): needs embeddings in
   the index and an in-memory cosine comparison. Off by default.
3. **Memory:** the whole parse result and all collected rows are held for the run. Peak
   not measured.
4. **Speed:** not measured. Remaining MATCH queries are still on the path.
5. **Retry path:** `pipeline.py` retries a failed per-file write. In bulk mode a retry
   can add the same rows twice; they merge on primary key, but this is untested.
6. **CLI:** `initialize_repository` has no command yet.
7. **Tests:** unit tests cover `SymbolIndex`, the uid rule, and the CALLS, INHERITS and
   DECORATED_BY edge builders. Not covered yet: `BulkCollector` (flush order, Parameter and
   IMPORTS merge rules), and `initialize_repository`.

## Decisions

- Repo-init writes through a bulk path per backend, not `MERGE` queries that search for
  endpoints.
- Kùzu: `COPY` from Parquet (nodes and relationships).
- Neo4j: driver-side `UNWIND` batches, no CSV, no offline import (user decision).
- Cross-repo references are not reachable from the pipeline (the resolver only sees the
  files being indexed), so no fallback is needed.
- Scope: all 28 relationship types (user decision). Work proceeds type by type, each
  checked against the standard path.

## How to verify a change

Environment: WSL, `~/cgc-venv` (Python 3.14), project installed editable with `.[dev]`
and `.[kuzu]` (which now includes `pyarrow`).

Scripts (in the session scratchpad, not in the repository):

- `parity_run.py <standard|repo_init> <kuzu|neo4j> <repo> <out.json> [kuzu_db_dir]`
  dumps a sorted node and edge snapshot. Use a separate Kùzu DB directory for each
  fixture; reusing one accumulates nodes across fixtures.
- Baselines: `base-<fixture>.json`, from the standard path, regenerated when the code
  changes behaviour. Compare each repo-init run with `cmp`.
- Spy: patch `bulk_kuzu.copy_rel_rows` (or `copy_grouped_edges`' target) to count rows per
  relationship type, to confirm the COPY branch ran.

Checks, in order: `py_compile`, `ruff check --select F821,F811,F401`, the six-fixture
comparison, the spy on a fixture that has the type, then `pytest tests/unit`.

## Pitfalls already hit

- A splice script inserted a method at the wrong place and left two copies of
  `add_file_to_graph`. The parity check passed for the wrong reason. Check the
  structure with `ast` (exactly one definition per name) before trusting a parity result.
- Parity baseline loops that reuse one Kùzu directory accumulate nodes from the earlier
  fixtures. Use one directory per fixture.
- Kùzu backend name is `kuzudb`, and `CALL table_info` works on node tables only.
- The Kùzu CSV reader mangles list elements; use Parquet.

## Standard-path defects (not fixed, on purpose)

Standard behaviour must stay exact until a change is agreed.

- Phase 5 on Kùzu: `Binder exception: Cannot find property qualified_name for cls`.
  Caught and logged by the pipeline, so phase 5 silently does nothing on the standard path.
- Phase 4 on Kùzu: the Function fetch uses `qualified_name` and `parameters`, which the
  Function table does not have. The standard path never embeds on Kùzu.
