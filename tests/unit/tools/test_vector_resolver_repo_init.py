"""Unit tests for the in-memory embedding path used by repo init (Phase 4).

No network, no model download, no database. The embedder is a fake that maps
each text to a fixed vector. The skip test patches the import probe, so it does
not depend on which embedding packages the host has installed.
"""

from unittest.mock import MagicMock, patch


_EMBED = "codegraphcontext.tools.indexing.embeddings"


def _fake_embedder(vectors_by_text=None, default=(1.0, 0.0, 0.0)):
    """Embedder whose embed_batch returns the mapped vector per text."""
    vectors_by_text = vectors_by_text or {}
    embedder = MagicMock()
    embedder.dim = 3
    embedder.embed_batch.side_effect = lambda texts: [
        list(vectors_by_text.get(t, default)) for t in texts
    ]
    return embedder


def _functions(n):
    return [
        {"name": f"fn{i}", "path": f"/repo/f{i}.py", "line_number": i + 1,
         "qualified_name": None, "docstring": None, "parameters": []}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# embed_functions_in_memory
# ---------------------------------------------------------------------------

class TestEmbedFunctionsInMemory:

    def test_skips_cleanly_when_no_backend_installed(self):
        """No embedding package installed: nothing is embedded, the reason is logged and returned."""
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        embedder_factory = MagicMock()
        with patch("importlib.util.find_spec", return_value=None), \
             patch(f"{_EMBED}._get_embedder", embedder_factory), \
             patch(f"{_EMBED}.warning_logger") as logged:
            vectors, reason = embed_functions_in_memory(_functions(3), model_spec="local")

        assert vectors == {}
        assert reason and "no embedding backend" in reason
        embedder_factory.assert_not_called()
        assert any("Skipping in-memory embeddings" in str(c.args[0]) for c in logged.call_args_list)

    def test_skips_when_openai_key_missing(self, monkeypatch):
        """Explicit openai model without OPENAI_API_KEY is a skip, not an error."""
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch(f"{_EMBED}._get_embedder") as factory:
            vectors, reason = embed_functions_in_memory(_functions(2), model_spec="openai")

        assert vectors == {}
        assert reason is not None
        factory.assert_not_called()

    def test_model_load_failure_returns_reason(self):
        """A probe that passes but a model that fails to load is still a logged skip."""
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fastembed")), \
             patch(f"{_EMBED}._get_embedder", side_effect=RuntimeError("model download blocked")):
            vectors, reason = embed_functions_in_memory(_functions(2), model_spec="fastembed")

        assert vectors == {}
        assert "model download blocked" in reason

    def test_embeds_each_function_by_index_in_batches(self):
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        embedder = _fake_embedder()
        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fake")), \
             patch(f"{_EMBED}._get_embedder", return_value=embedder):
            vectors, reason = embed_functions_in_memory(_functions(3), batch_size=2)

        assert reason is None
        assert set(vectors) == {0, 1, 2}
        assert embedder.embed_batch.call_count == 2

    def test_uses_the_same_text_as_the_database_path(self):
        """Text is built by _build_text from the same fields the database path reads."""
        from codegraphcontext.tools.indexing.embeddings import _build_text, embed_functions_in_memory

        fns = [{"name": "process", "path": "/a.py", "line_number": 1,
                "qualified_name": "pkg.Svc.process", "docstring": "Handles orders.",
                "parameters": ["order_id"]}]
        embedder = _fake_embedder()
        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fake")), \
             patch(f"{_EMBED}._get_embedder", return_value=embedder):
            embed_functions_in_memory(fns)

        embedder.embed_batch.assert_called_once_with([_build_text(fns[0])])

    def test_failed_batch_is_skipped_and_others_kept(self):
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        embedder = _fake_embedder()
        embedder.embed_batch.side_effect = [RuntimeError("API error"), [[0.1, 0.2, 0.3]]]
        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fake")), \
             patch(f"{_EMBED}._get_embedder", return_value=embedder):
            vectors, reason = embed_functions_in_memory(_functions(3), batch_size=2)

        assert reason is None
        assert set(vectors) == {2}

    def test_length_mismatch_skips_batch(self):
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        embedder = _fake_embedder()
        embedder.embed_batch.side_effect = [[[0.1, 0.2, 0.3]]]  # 1 vector for 2 inputs
        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fake")), \
             patch(f"{_EMBED}._get_embedder", return_value=embedder):
            vectors, _ = embed_functions_in_memory(_functions(2), batch_size=2)

        assert vectors == {}


# ---------------------------------------------------------------------------
# InMemoryVectorResolver
# ---------------------------------------------------------------------------

class TestInMemoryVectorResolver:

    def test_returns_best_path_above_threshold(self):
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        embedder = _fake_embedder(default=(1.0, 0.0, 0.0))
        resolver = InMemoryVectorResolver(
            embedder,
            [("run", "/a.py", [1.0, 0.0, 0.0]), ("run", "/b.py", [0.6, 0.8, 0.0])],
            threshold=0.75,
        )
        assert resolver.resolve("run", "pkg.Caller.go", ["/a.py", "/b.py"]) == "/a.py"

    def test_candidate_paths_restrict_the_choice(self):
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        embedder = _fake_embedder(default=(1.0, 0.0, 0.0))
        resolver = InMemoryVectorResolver(
            embedder,
            [("run", "/a.py", [1.0, 0.0, 0.0]), ("run", "/b.py", [0.9, 0.1, 0.0])],
        )
        assert resolver.resolve("run", None, ["/b.py"]) == "/b.py"

    def test_below_threshold_returns_none(self):
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        embedder = _fake_embedder(default=(1.0, 0.0, 0.0))
        resolver = InMemoryVectorResolver(embedder, [("run", "/a.py", [0.0, 1.0, 0.0])], threshold=0.75)
        assert resolver.resolve("run", None, ["/a.py"]) is None

    def test_tie_goes_to_later_entry(self):
        """Same as the property scan: score >= best, so the later entry wins a tie."""
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        embedder = _fake_embedder(default=(1.0, 0.0, 0.0))
        resolver = InMemoryVectorResolver(
            embedder,
            [("run", "/a.py", [1.0, 0.0, 0.0]), ("run", "/b.py", [1.0, 0.0, 0.0])],
        )
        assert resolver.resolve("run", None, ["/a.py", "/b.py"]) == "/b.py"

    def test_no_candidates_skips_embedding(self):
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        embedder = _fake_embedder()
        resolver = InMemoryVectorResolver(embedder, [("run", "/a.py", [1.0, 0.0, 0.0])])
        assert resolver.resolve("run", None, []) is None
        assert resolver.resolve("missing", None, ["/a.py"]) is None
        embedder.embed_batch.assert_not_called()

    def test_embedder_failure_returns_none(self):
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        embedder = MagicMock()
        embedder.embed_batch.side_effect = RuntimeError("network error")
        resolver = InMemoryVectorResolver(embedder, [("run", "/a.py", [1.0, 0.0, 0.0])])
        assert resolver.resolve("run", None, ["/a.py"]) is None

    def test_from_function_records_maps_indices_to_entries(self):
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        fns = _functions(2)
        embedder = _fake_embedder(default=(0.0, 1.0, 0.0))
        resolver = InMemoryVectorResolver.from_function_records(
            embedder, fns, {1: [0.0, 1.0, 0.0]}  # index 0 has no vector, so it is not an entry
        )
        assert resolver.resolve("fn1", None, ["/repo/f1.py"]) == "/repo/f1.py"
        assert resolver.resolve("fn0", None, ["/repo/f0.py"]) is None

    def test_end_to_end_skip_then_no_resolver_needed(self):
        """With no backend, embed returns no vectors, so an in-memory resolver has no entries and resolves nothing."""
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        fns = _functions(2)
        with patch("importlib.util.find_spec", return_value=None):
            vectors, reason = embed_functions_in_memory(fns)
        assert reason is not None
        resolver = InMemoryVectorResolver.from_function_records(_fake_embedder(), fns, vectors)
        assert resolver.resolve("fn0", None, ["/repo/f0.py"]) is None
