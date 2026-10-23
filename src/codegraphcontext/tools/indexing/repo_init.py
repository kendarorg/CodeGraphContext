# src/codegraphcontext/tools/indexing/repo_init.py
"""Repo initialization: index a repository that has no graph data yet.

Parses the whole directory, keeps the nodes in a :class:`SymbolIndex`, and
resolves the relationship lookups from that index instead of querying the
database. The standard ``cgc index`` path is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ...core.jobs import JobManager
from ...utils.debug_log import info_logger
from ...utils.path_sandbox import is_path_allowed
from .persistence.writer import GraphWriter
from .pipeline import run_tree_sitter_index_async
from .symbol_index import SymbolIndex


class RepoAlreadyIndexedError(RuntimeError):
    """Raised when the graph already holds nodes for the repository."""


def _repo_has_graph_data(driver: Any, repo_path: str) -> bool:
    with driver.session() as session:
        record = session.run(
            "MATCH (r:Repository {path: $path}) RETURN count(r) AS n",
            path=repo_path,
        ).single()
    return bool(record and record["n"])


async def initialize_repository(
    graph_builder: Any,
    path: Path,
    cgcignore_path: Optional[str] = None,
    is_dependency: bool = False,
    job_id: Optional[str] = None,
    job_manager: Optional[JobManager] = None,
) -> Dict[str, Any]:
    """Index a repository directory into an empty graph, using an in-memory symbol index.

    Raises
    ------
    ValueError
        If ``path`` is not an existing directory or is outside the allowed roots.
    RepoAlreadyIndexedError
        If the graph already holds this repository. Use ``cgc index`` to re-index.
    """
    path = Path(path).resolve()
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    if not is_path_allowed(path):
        raise ValueError(f"Path is outside the allowed roots: {path}")

    driver = graph_builder.db_manager.get_driver()
    if _repo_has_graph_data(driver, path.as_posix()):
        raise RepoAlreadyIndexedError(
            f"{path} is already in the graph. Use 'cgc index' to re-index it."
        )

    index = SymbolIndex()
    writer = GraphWriter(driver, db_manager=graph_builder.db_manager, symbol_index=index)
    writer.enable_bulk()
    manager = job_manager or graph_builder.job_manager
    diagnostics: list = []
    summary: Dict[str, Any] = {}

    info_logger(f"[REPO-INIT] Initializing {path}")
    await run_tree_sitter_index_async(
        path,
        is_dependency,
        job_id,
        cgcignore_path,
        writer,
        manager,
        graph_builder.parsers,
        graph_builder.get_parser,
        graph_builder.parse_file,
        writer.add_minimal_file_node,
        call_resolution_diagnostics=diagnostics,
        index_summary=summary,
    )
    info_logger(f"[REPO-INIT] Done: {len(index)} symbol(s) held in memory")
    return {"path": path.as_posix(), "symbols": len(index), "summary": summary}
