# src/codegraphcontext/tools/indexing/vector_resolver.py
"""Vector-similarity-based call resolution using pre-computed Function embeddings.

Used as a tiebreaker in ``resolve_function_call`` when heuristic tiers cannot
produce a high-confidence answer. Neo4j uses native ANN vector search; other
backends score stored embeddings with in-process cosine similarity.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...utils.debug_log import warning_logger
from .embeddings import cosine_similarity, detect_graph_backend

_VECTOR_INDEX_NAME = "function_embeddings"
_DEFAULT_THRESHOLD = 0.75
_DEFAULT_TOP_K = 5


class VectorResolver:
    """Pick the best Function among candidates using embedding similarity."""

    def __init__(
        self,
        driver: Any,
        threshold: float = _DEFAULT_THRESHOLD,
        top_k: int = _DEFAULT_TOP_K,
    ):
        self.driver = driver
        self.threshold = threshold
        self.top_k = top_k
        self._embedder = None
        self._backend = detect_graph_backend(driver)

    def _get_embedder(self):
        if self._embedder is None:
            from codegraphcontext.tools.indexing.embeddings import _get_embedder

            self._embedder = _get_embedder()
        return self._embedder

    def _embed_query(self, text: str) -> List[float]:
        return self._get_embedder().embed_batch([text])[0]

    def _resolve_neo4j(
        self,
        called_name: str,
        candidate_paths: List[str],
        query_vec: List[float],
    ) -> Optional[str]:
        with self.driver.session() as session:
            try:
                effective_top_k = max(self.top_k, len(candidate_paths))
                result = session.run(
                    f"""
                    CALL db.index.vector.queryNodes(
                        '{_VECTOR_INDEX_NAME}', $top_k, $vec
                    ) YIELD node AS fn, score
                    WHERE fn.name = $name
                      AND fn.path IN $paths
                    RETURN fn.path AS path, score
                    ORDER BY score DESC
                    LIMIT 1
                    """,
                    top_k=effective_top_k,
                    vec=query_vec,
                    name=called_name,
                    paths=candidate_paths,
                )
                row = result.single()
                if row and row["score"] >= self.threshold:
                    return row["path"]
            except Exception as e:
                warning_logger(f"[VECTOR] Neo4j ANN query failed: {e}")
        return None

    def _resolve_property_scan(
        self,
        called_name: str,
        candidate_paths: List[str],
        query_vec: List[float],
    ) -> Optional[str]:
        """Brute-force cosine similarity over stored node embeddings."""
        with self.driver.session() as session:
            try:
                result = session.run(
                    """
                    MATCH (f:Function)
                    WHERE f.name = $name
                      AND f.path IN $paths
                      AND f.embedding IS NOT NULL
                    RETURN f.path AS path, f.embedding AS embedding
                    """,
                    name=called_name,
                    paths=candidate_paths,
                )
                best_path: Optional[str] = None
                best_score = self.threshold
                for row in result:
                    embedding = row.get("embedding")
                    if not embedding:
                        continue
                    if hasattr(embedding, "tolist"):
                        embedding = embedding.tolist()
                    score = cosine_similarity(query_vec, list(embedding))
                    if score >= best_score:
                        best_score = score
                        best_path = row["path"]
                return best_path
            except Exception as e:
                warning_logger(f"[VECTOR] Property-scan query failed: {e}")
        return None

    def resolve(
        self,
        called_name: str,
        caller_qualified_name: Optional[str],
        candidate_paths: List[str],
        repo_path: str,
    ) -> Optional[str]:
        """Return the file path of the best-matching Function among candidates."""
        if not candidate_paths:
            return None

        query_text = f"{caller_qualified_name or ''} calls {called_name}"
        try:
            query_vec = self._embed_query(query_text)
        except Exception as e:
            warning_logger(f"[VECTOR] Embed query failed: {e}")
            return None

        if self._backend == "neo4j":
            return self._resolve_neo4j(called_name, candidate_paths, query_vec)
        return self._resolve_property_scan(called_name, candidate_paths, query_vec)

    def resolve_bulk(
        self,
        calls: List[Dict[str, Any]],
        repo_path: str,
    ) -> Dict[int, str]:
        """Resolve a list of calls; returns {call_index: resolved_path}."""
        results: Dict[int, str] = {}
        for idx, call in enumerate(calls):
            resolved = self.resolve(
                called_name=call["called_name"],
                caller_qualified_name=call.get("caller_qualified_name"),
                candidate_paths=call["candidate_paths"],
                repo_path=repo_path,
            )
            if resolved:
                results[idx] = resolved
        return results


class InMemoryVectorResolver:
    """Cosine tiebreak over embeddings held in memory (repo-init path).

    Scores the same candidates as ``VectorResolver._resolve_property_scan``:
    entries with the called name whose path is a candidate, at or above the
    threshold. Entries are scanned in the order given, and on equal scores the
    later entry wins, as in the property scan. No database is touched.
    """

    def __init__(
        self,
        embedder: Any,
        entries: Sequence[Tuple[str, str, Sequence[float]]],
        threshold: float = _DEFAULT_THRESHOLD,
    ):
        self._embedder = embedder
        self.threshold = threshold
        # called name -> [(path, vector)] in entry order
        self._by_name: Dict[str, List[Tuple[str, List[float]]]] = {}
        for name, path, vec in entries:
            if vec is None or len(vec) == 0:
                continue
            self._by_name.setdefault(name, []).append((path, list(vec)))

    @classmethod
    def from_function_records(
        cls,
        embedder: Any,
        functions: Sequence[Dict[str, Any]],
        vectors: Dict[int, Sequence[float]],
        threshold: float = _DEFAULT_THRESHOLD,
    ) -> "InMemoryVectorResolver":
        """Build from parsed Function records and the {index: vector} map from
        ``embeddings.embed_functions_in_memory``. Records without a vector are skipped."""
        entries = [
            (functions[i]["name"], functions[i]["path"], vec)
            for i, vec in sorted(vectors.items())
        ]
        return cls(embedder, entries, threshold=threshold)

    def resolve(
        self,
        called_name: str,
        caller_qualified_name: Optional[str],
        candidate_paths: List[str],
        repo_path: Optional[str] = None,
    ) -> Optional[str]:
        """Return the file path of the best-matching Function among candidates.

        ``repo_path`` is accepted for signature parity with ``VectorResolver``; the
        entries are already limited to the repository being indexed.
        """
        if not candidate_paths:
            return None
        candidates = self._by_name.get(called_name)
        if not candidates:
            return None

        query_text = f"{caller_qualified_name or ''} calls {called_name}"
        try:
            query_vec = self._embedder.embed_batch([query_text])[0]
        except Exception as e:
            warning_logger(f"[VECTOR] Embed query failed: {e}")
            return None

        allowed = set(candidate_paths)
        best_path: Optional[str] = None
        best_score = self.threshold
        for path, vec in candidates:
            if path not in allowed:
                continue
            score = cosine_similarity(query_vec, vec)
            if score >= best_score:
                best_score = score
                best_path = path
        return best_path
