# Repo initialization (`repo_init`)

## Purpose

Index a repository directory into a graph that holds no data for it yet. The whole
directory is parsed, the relationships are resolved from an in-memory `SymbolIndex`, and
the rows are written with bulk loads instead of per-file `MERGE` queries. The standard
`cgc index` path is unchanged: it passes no index and runs the original queries.

Entry points:
- `initialize_repository(graph_builder, path, ...)` in
  `src/codegraphcontext/tools/indexing/repo_init.py`. It rejects paths that are not
  directories, paths outside the allowed roots (`CGC_ALLOWED_ROOTS`), and repositories that
  already have a `Repository` node (use `cgc index` to re-index).
- CLI: `cgc index <path> --repo-init`. Cannot be combined with `--force`.

## How it works

1. `repo_init.py` creates a `SymbolIndex` and a `GraphWriter` with the index attached, and
   calls `writer.enable_bulk()`. Bulk is on only for backends with a bulk path (below).
2. `pipeline.run_tree_sitter_index_async` parses every file. Per file,
   `GraphWriter.add_file_to_graph` registers the nodes in the index and stages the rows.
   Registrations and bulk calls are applied only after every write for that file has
   succeeded, so a retry does not duplicate them.
3. Relationship resolution reads from the index, not the database. Link writers check
   `_use_index_writes(backend)` (Kùzu, LadybugDB) or `_use_neo4j_bulk(backend)` (Neo4j,
   NornicDB) and take the bulk branch when it holds. Otherwise the standard queries run.
4. `writer.flush_bulk()` writes the collected node and relationship rows, after the per-file
   loop.
5. Phases 4 and 5 run in repo-init only when their flags are on (both are off by default):
   - **Phase 4 (embeddings):** runs only when `ENABLE_VECTOR_RESOLVE=true` and
     `ENABLE_INHERIT_RESOLVE=true`. Embeddings are computed in memory from the parsed
     Function records with fastembed (`CGC_EMBEDDING_MODEL=fastembed`, model
     `BAAI/bge-small-en-v1.5`, 384 dims). Nothing is written to the backend; the vectors are
     used only by the in-memory tiebreak. Otherwise a log line says why it was skipped.
   - **Phase 5 (inheritance re-resolution):** reads tier 8/9 CALLS, CONTAINS and INHERITS
     from the index. It writes tier 10/11 CALLS in bulk on Kùzu and LadybugDB (delete the old
     tier 8/9 edges, then COPY the new ones), and with `MERGE` on Neo4j.
     Target selection: the receiver's declared type is used first (the variable declared
     before the call, e.g. `numberQueue: Queue`). Otherwise candidates are sorted by
     `(path, line, class)`. Low-confidence rows are sorted by `(caller, caller line, call
     line)`, so the same target wins on every backend.

### Bulk path per backend

| Backend | Bulk mechanism | Enabled in repo-init |
|---|---|---|
| Kùzu (`kuzudb`) | Parquet `COPY` (`bulk_kuzu.py`) | yes |
| LadybugDB (`ladybugdb`) | same `COPY` as Kùzu | yes |
| Neo4j (`neo4j`) | driver-side `UNWIND` batches of 1000 (`bulk_neo4j.py`) | yes |
| NornicDB (`nornic`) | same as Neo4j (`_NEO4J_BULK_BACKENDS`) | yes (see Verified state) |
| FalkorDB (`falkordb`, `falkordb-remote`) | none; repo-init runs the standard queries | no |

FalkorDB does not need a bulk writer: its standard path was faster than Kùzu's standard path
on the same fixtures (TypeScript 8.6 s vs 10.7 s; sample_project 6.3 s vs 11.5 s). These are
whole-index times, not write-only times.

### Relationship types on the bulk path

- **Kùzu and LadybugDB (COPY):** CONTAINS (including directory CONTAINS), CALLS,
  HEURISTIC_CALLS, INHERITS (internal parents, `ExternalClass`), C# INHERITS, DECORATED_BY,
  BINDS, HAS_PARAMETER, IMPORTS, METACLASS, IMPLEMENTS, EMBEDS, PREVIEWS, COMPANION_OF,
  INJECTS, INCLUDES, PART_OF, PARTIAL_OF, C++ class-function CONTAINS, MAPS_TO, READS and
  WRITES (from `@Query` and MyBatis), Maven CHILD_MODULE, MODULE_DEPENDS_ON, USES_LIBRARY,
  and tier 10/11 CALLS from phase 5.
- **Neo4j and NornicDB (UNWIND):** nodes per label, CONTAINS, HAS_PARAMETER, IMPORTS and the
  deferred side rows; MAPS_TO, READS/WRITES from `@Query`, MyBatis READS/WRITES; tier 10/11
  CALLS from phase 5 (MERGE).

## Kùzu and LadybugDB rules that matter

- **Primary key `uid`** for code nodes is the concatenation of the identity fields from
  `uid_map`, with `occurrence_index` defaulting to 0 and no separator (`kuzu_uid`).
  Module is keyed by `name`; File by `path`.
- **Lists in COPY:** use Parquet. Kùzu's CSV reader mis-parses `STRING[]` elements with
  commas, brackets or quotes.
- **Parquet types:** boolean is `pyarrow.bool_`, not `pyarrow.bool`.
- **Relationship COPY** takes `from` and `to` columns with the endpoint primary keys.
- **Bindings:** `CALL show_connection('<REL>')` returns the declared
  `(source table, destination table)` pairs. Writers skip pairs the schema does not bind,
  as the old queries did. These calls work only with a `RETURN` clause.
- **Table columns:** `CALL table_info('<Label>')` works on node tables only.
- **Backend names:** `kuzudb` (not `kuzu`) and `ladybugdb`. LadybugDB 0.19.1 has workarounds
  in `database_embedded_kuzu.py` (dropped rows and missing edges in 0.19); compare bulk and
  standard counts when changing it.

## Verified state

Repo-init matches the standard path on the same backend, node and edge sets identical, with
`indexed_at` stripped. Counts (nodes/edges):

| Fixture | Kùzu | LadybugDB | Neo4j | NornicDB (`nornic` type) |
|---|---|---|---|---|
| sample_project | 518/672 | 518/672 | 518/672 | - |
| sample_project_kotlin | 244/318 | - | - | - |
| sample_project_typescript | 918/1497 | 918/1497 | 918/1497 | 918/1497 (snapshots byte-identical; bulk ran) |
| sample_project_go | 674/910 | - | - | - |
| sample_project_java | 159/224 | 159/224 | 159/224 | - |
| sample_project_cpp | 144/189 | - | - | - |
| sample_project_csharp | 145/210 | - | - | - |
| sample_project_jpa | 28/33 | 28/33 | 28/33 | - |
| sample_project_mybatis | 14/16 | 14/16 | 14/16 | - |
| sample_project_maven | 29/31 | 29/31 | - | - |

Phase 5 on TypeScript (ENABLE_INHERIT_RESOLVE=true): tier 10/11 CALLS 0 to 53, and Java 0 to
1. LadybugDB repo-init with phase 5 on gives 56 tier 10/11 CALLS, the same as Kùzu, with
identical CALLS edge sets (313 each). NornicDB phase 5 has not been run yet. The `numberQueue.enqueue` target is `Queue.enqueue` (line 219) on Neo4j standard, Neo4j
repo-init and Kùzu repo-init.

Phase 4 on TypeScript (fastembed, in memory): 282 of 282 Function nodes embedded, dim 384.
`SampleComponent` first values match the standard Neo4j run (-0.0294, 0.0417, -0.0193,
-0.0058).

Unit suite: 1930 passed, 8 skipped, 0 failed (`pytest tests/unit`).

## Known differences and caveats

- **MyBatis READS:** the Kùzu COPY writes one edge per (function, table) pair with a NULL
  `line_number` and does not dedupe against `@Query` edges. The Neo4j MERGE reuses an
  existing READS between the pair. They differ only when a `@Query` and a mapper share a
  method name.
- **Kùzu vs Neo4j repo-init representation:** about 40 IMPORTS/INHERITS rows differ, a
  representation difference, not a resolution one.
- **Phase 5 on Kùzu without vectors** is not deterministic across runs (about 17 edges in one
  check). Accepted; not traced.
- **C# IMPLEMENTS:** interface bases and non-first class bases are not recorded in the index.
- **Retry:** writes are staged per file and applied after success. Phase 5 deletes its own
  previous edges before re-adding them, so a re-run does not duplicate them.
- **Embeddings dependency:** fastembed pulls in onnxruntime, which requires protobuf
  4.x. The project pins `protobuf<3.21`, so pip reports a conflict. Installed protobuf is
  3.20.3; fastembed works on it, and the unit suite passes. Kept on purpose.

## Not done (probably never)

- **Spring EXPOSES_ENDPOINT, PROVIDES_BEAN, FILE_BELONGS_TO:** no code produces them. Only
  `schema_contract.py` lists them. Writing producers is a feature, not a conversion. Kept as
  a note; not planned.
- **Datasource (READS, WRITES, MAPS_TO, HAS_COLUMN, STORED_IN):** produced by
  `mysql_ingester.py`, which reads a live MySQL database. Left on the query path.
- **Spring Data derived-query links:** the Kùzu schema rejects their MERGE (READS has no
  `source` column). The standard path swallows the error in `build_graph_from_path_async` and
  skips later phases. Pre-existing, not fixed; left on the query path.
- **Gradle build (MODULE_DEPENDS_ON, USES_LIBRARY):** no Gradle fixture, so not converted. A
  repo with both Maven and Gradle would probably fail the Kùzu COPY, since both write
  `ExternalLibrary` rows. Untested.
- **`write_cpp_class_function_links`, `repair_missing_contains_links`:** no effect on the
  fixtures (the standard path adds nothing). Left on the query path.
- **Maven on Neo4j and NornicDB:** the Maven types stay on the `MERGE` path. Decided.
- **FalkorDB and FalkorDB remote:** no bulk writer. FalkorDB Lite standard path is fast enough.
  FalkorDB remote is untested.
- **NornicDB timing:** not measured.

## Standard-path defects (not fixed, on purpose)

Standard behaviour must stay exact until a change is agreed.

- Phase 5 on Kùzu: `Binder exception: Cannot find property qualified_name for cls`.
  The pipeline catches and logs it, so phase 5 silently does nothing on the standard path.
- Phase 4 on Kùzu: the Function fetch uses `qualified_name` and `parameters`, which the
  Function table does not have. The standard path never embeds on Kùzu.

## Neo4j and NornicDB test setup

Neo4j and NornicDB run in Docker containers started by the parity suite (`tests/parity/conftest.py`, testcontainers). Nothing is started by hand:

- Neo4j: `neo4j:5.12.0-community`.
- NornicDB: `timothyswt/nornicdb-amd64-cpu-headless`, pinned by digest, auth off. Its HTTP health endpoint is the readiness check.
- Docker is found through `DOCKER_HOST` when set. Ryuk is disabled (`TESTCONTAINERS_RYUK_DISABLED=true`).

Run with `./tests/run_tests.sh parity`. The suite is not part of the default test runs.

## How to verify a change

Environment: WSL, `~/cgc-venv` (Python 3.14), project installed with `.[dev]` and `.[kuzu]`.

The comparisons are automated in `tests/parity/`:
- `test_repo_init_parity.py`: repo_init against the standard path, per backend and fixture.
- `test_verify_databases_parity.py`: counts and edge sets across all backends.

Checks, in order: `py_compile`, `ruff check --select F821,F811,F401`, the six-fixture
comparison (`indexed_at` stripped), the spy on a fixture that has the type, then
`pytest tests/unit`.

Baselines (Kùzu, standard path): sample_project 518/672, kotlin 244/318, typescript 918/1497,
go 674/910, java 159/224, cpp 144/189.

## Pitfalls already hit

- A splice script inserted a method at the wrong place and left two copies of
  `add_file_to_graph`. Parity passed for the wrong reason. Check with `ast` that each name is
  defined once before trusting a parity result.
- Parity loops that reuse one Kùzu directory accumulate nodes from earlier fixtures. Use one
  directory per fixture.
- Fresh `HOME` with no tree-sitter cache parses to 1 node and 0 edges. Symlink
  `~/.cache/tree-sitter-language-pack` and `huggingface` into a fresh `HOME`.
- `cgc index` ignored `LADYBUGDB_PATH` in one run and wrote to `~/.codegraphcontext`.
- `CGC_ALLOWED_ROOTS` must include the fixtures directory for repo-init runs (or `*` on
  Neo4j/NornicDB test runs).
- Docker on this machine: `docker run` output can be swallowed when piped through `tail`;
  write to a file or use `docker exec` instead.
