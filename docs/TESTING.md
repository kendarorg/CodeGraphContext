# 🧪 CodeGraphContext Testing Strategy

This document serves as the single source of truth for testing **CodeGraphContext**. It consolidates usage instructions, architectural philosophy, and future roadmap items.

---

## 🚀 Quick Start: Running Tests

We provide a helper script `tests/run_tests.sh` to simplify test execution.

| Suite | Command | Use Case |
| :--- | :--- | :--- |
| **All Tests** | `./tests/run_tests.sh all` | Full CI/CD verification (includes E2E). |
| **Fast Tests** | `./tests/run_tests.sh fast` | **Recommended for local dev.** Runs Unit + Integration. |
| **Unit Tests** | `./tests/run_tests.sh unit` | Focus on individual components (parsers, database logic). |
| **User Journeys** | `./tests/run_tests.sh e2e` | Validate full end-to-end user workflows. |
| **Parity** | `./tests/run_tests.sh parity` | Live-database parity (Neo4j, NornicDB in Docker). Not part of `fast`, `e2e` or `all`. |

### Parity suite (`tests/parity/`)

Marked `parity` and deselected by default (`addopts` in `pyproject.toml`), so `pytest` and CI's fast and e2e jobs never start it. Run it with `pytest tests/parity -m parity` or `./tests/run_tests.sh parity`.

*   **Docker is required.** Neo4j (`neo4j:5.12.0-community`) and NornicDB (`timothyswt/nornicdb-amd64-cpu-headless`, pinned by digest) start as containers through `testcontainers`. A missing daemon fails the run; it does not skip.
*   **Remote daemons:** set `DOCKER_HOST` (e.g. `tcp://192.168.1.45:2375`). Without it, the local default socket is used.
*   **Ryuk is disabled** (`TESTCONTAINERS_RYUK_DISABLED=true`, set by `conftest.py`, the run script and CI). The reaper container fails on remote hosts. Containers stop when their fixture ends; after a crash, `docker ps` shows leftovers.
*   **Contents:**
    *   `test_verify_databases_parity.py`: node and relationship counts and CONTAINS/CALLS edge sets across KùzuDB, LadybugDB, FalkorDB Lite, Neo4j and NornicDB.
    *   `test_repo_init_parity.py`: for each backend and fixture, `repo_init` must write the same nodes and relationships as the standard index path.
*   Each index runs in a subprocess with its own database, as in the other end-to-end tests.

---

## 🪟 Windows Notes

Verified on Windows 11 (Python 3.14, Docker over `DOCKER_HOST`):

*   **Parity suite:** passes. The Kùzu legs skip, because no `kuzu` wheel exists for Python 3.14 on Windows. LadybugDB, Neo4j and NornicDB run.
*   **Unit and integration suites:** 51 failed and 17 errors. The same counts occur on a clean checkout of HEAD, so they predate the parity work. Most come from POSIX path literals (for example `/repo/src`, which Windows resolves to `C:/repo/src`). The rest are golden-file and CLI output tests. Affected files: `tests/integration/test_parser_goldens.py`, `test_cgcignore_cli.py`, `test_find_text_output_stream.py`, `test_analyze_text_output_stream.py`, `test_analyze_complexity_ci.py`, `test_diagram_export.py`, `test_vector_resolve_visibility.py`, `test_context_repo_registration.py`, `tests/integration/cli/test_cli_commands.py`, `tests/unit/tools/test_bulk_directory_contains.py`, `test_bulk_retry.py`, `test_contains_invariant_repair.py`, `tests/unit/parsers/test_elisp_parser.py`, `tests/unit/core/test_database_falkordb_wrapper.py`, `tests/unit/cli/test_setup_wizard_jsonc.py`.

---

## 🏗️ Test Architecture

The test suite follows the **Testing Pyramid** principle, ensuring a balanced mix of speed and confidence.

### 1. `tests/unit/` (Bottom Layer)
*   **Speed:** Very Fast (< 100ms)
*   **Scope:** Isolated classes and functions.
*   **Mocking:** Heavy mocking of external dependencies (Neo4j, FileSystem).
*   **Content:**
    *   `core/`: `DatabaseManager`, `JobManager`, `FileWatcher`.
    *   `parsers/`: Output verification for `TreeSitterParser` (Python, JS, etc.).
    *   `tools/`: `GraphBuilder` logic, `CodeFinder` query generation.

### 2. `tests/integration/` (Middle Layer)
*   **Speed:** Fast (~1s)
*   **Scope:** Interaction between 2+ components.
*   **Mocking:** Partial (e.g., mock the database connection but run the real `GraphBuilder` logic).
*   **Content:**
    *   `cli/`: Typer command execution, argument parsing, error handling.
    *   `mcp/`: Server routing, tool call validation, JSON protocol adherence.

### 3. `tests/e2e/` (Top Layer)
*   **Speed:** Slow (> 10s)
*   **Scope:** Full system as seen by the user.
*   **Mocking:** Minimal/None (uses real file interactions, simulated sub-processes).
*   **Content:**
    *   `test_user_journeys.py`: "User initializes repo", "User queries function callers", "User exports bundle".

### 4. `tests/perf/` (Side Quest)
*   **Scope:** Benchmarks for large codebase indexing and complex query latency.

---

## ✅ What We Test

### 1. User Workflows & Journeys
We simulate real-world usage to ensure the product solves user problems.
*   **First-time Setup:** Initialization -> Indexing -> Verifying output.
*   **Daily Dev:** Watching for file changes -> Auto-updating graph.
*   **Code Search:** Finding functions by name, argument, or decorator.

### 2. CLI Functionality
Every command in `cgc --help` is tested.
*   `index`: Argument validation, force flags.
*   `find/analyze`: Query construction and output formatting.
*   `mcp`: Server startup and tool exposure.

### 3. Language Support (Parsers)
We verify that our Tree-sitter parsers correctly extract:
*   **Structure:** Classes, Functions, Modules.
*   **Relationships:** Calls, Inheritance, Imports, Dependencies.
*   **Languages Covered:** Python, JavaScript/TypeScript, Java, C++, Go, Rust, Ruby, PHP, Dart, Perl, and more.

---

## 🛠️ How to Add Tests

### Adding a New Language Parser
1.  Create `tests/unit/parsers/test_<lang>_parser.py`.
2.  Use the `get_tree_sitter_manager()` singleton.
3.  Feed it a sample code string.
4.  Assert the structure of the returned definition dict.

### Adding a New CLI Command
1.  Create/Edit `tests/integration/cli/test_cli_commands.py`.
2.  Mock the underlying service (e.g., `GraphBuilder` or `CodeFinder`).
3.  Use `CliRunner` from `typer.testing` to invoke the command.
4.  Assert `result.exit_code == 0` and check `result.stdout`.

### Adding a Regression Test
1.  Identify the bug workflow.
2.  Create a test case in `tests/e2e/test_user_journeys.py` that reproduces it.
3.  Fix the bug and verify the test passes.

---

## 🔮 Future Roadmap (Ideas)

These ideas are consolidated from the legacy `IDEAL_TEST_PLAN.md`.

### 1. Advanced Performance Benchmarks
*   **Idea**: Test indexing on > 100k LoC repositories (e.g., use an archived version of React or Django as a fixture).
*   **Metric**: Indexing time per 1000 lines, Memory usage peak.

### 2. Mutation Testing
*   **Idea**: Use a tool like `mutmut` to introduce random bugs in the code and verify the test suite catches them.

### 3. Snapshot Testing for Parsers
*   **Idea**: Instead of asserting specific keys, verify the entire JSON output of a parser against a stored "snapshot". This makes updating parser tests much faster when schema changes.

### 4. Testcontainers Integration (partly done)
*   **Done**: Neo4j and NornicDB in the parity suite run in containers (see above). FalkorDB is not containerised yet.
*   **Idea**: Use the same fixtures for E2E tests that need a server.

### 5. Multi-Repo Analysis Workflows
*   **Idea**: E2E tests simulating a user querying calls *across* two different repositories (microservices scenario).
