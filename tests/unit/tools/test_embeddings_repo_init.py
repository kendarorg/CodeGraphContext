"""Repo-init phase 4: in-memory embedding text and the in-memory tiebreak.

No network, no model download, no database. The embedder is a fake.
"""

from unittest.mock import MagicMock, patch

_EMBED = "codegraphcontext.tools.indexing.embeddings"


def _fake_embedder(default=(1.0, 0.0, 0.0)):
    embedder = MagicMock()
    embedder.dim = 3
    embedder.embed_batch.side_effect = lambda texts: [list(default) for _ in texts]
    return embedder


class TestDbTextFields:

    def test_uses_parameters_not_args(self):
        """The database path reads `parameters` only, so args must not leak into the text."""
        from codegraphcontext.tools.indexing.embeddings import _build_text, db_text_fields

        fn = {"name": "run", "args": ["x", "y"], "parameters": [], "docstring": None}
        assert _build_text(db_text_fields(fn)) == "run"

    def test_missing_fields_default_like_the_database_row(self):
        from codegraphcontext.tools.indexing.embeddings import db_text_fields

        assert db_text_fields({"name": "f"}) == {
            "name": "f", "qualified_name": None, "docstring": None, "parameters": [],
        }

    def test_text_keeps_qualified_name_docstring_and_parameters(self):
        from codegraphcontext.tools.indexing.embeddings import _build_text, db_text_fields

        fn = {"name": "process", "qualified_name": "pkg.Svc.process",
              "docstring": "Handles orders.", "parameters": ["order_id"]}
        assert _build_text(db_text_fields(fn)) == "pkg.Svc.process | Handles orders. | params: order_id"


class TestEmbedWithPreloadedEmbedder:

    def test_pre_loaded_embedder_skips_probe_and_load(self):
        from codegraphcontext.tools.indexing.embeddings import embed_functions_in_memory

        embedder = _fake_embedder()
        fns = [{"name": "a", "path": "/a.py"}, {"name": "b", "path": "/b.py"}]
        with patch(f"{_EMBED}.probe_embedding_backend") as probe, \
             patch(f"{_EMBED}._get_embedder") as factory:
            vectors, reason = embed_functions_in_memory(fns, embedder=embedder)

        assert reason is None
        assert set(vectors) == {0, 1}
        probe.assert_not_called()
        factory.assert_not_called()


class TestLoadEmbedder:

    def test_probe_failure_returns_reason_and_no_embedder(self):
        from codegraphcontext.tools.indexing.embeddings import load_embedder

        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(False, "no backend")), \
             patch(f"{_EMBED}._get_embedder") as factory:
            embedder, reason = load_embedder("fastembed")

        assert embedder is None
        assert reason == "no backend"
        factory.assert_not_called()

    def test_load_failure_returns_reason(self):
        from codegraphcontext.tools.indexing.embeddings import load_embedder

        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fastembed")), \
             patch(f"{_EMBED}._get_embedder", side_effect=RuntimeError("boom")):
            embedder, reason = load_embedder("fastembed")

        assert embedder is None
        assert "boom" in reason

    def test_success_returns_embedder(self):
        from codegraphcontext.tools.indexing.embeddings import load_embedder

        sentinel = object()
        with patch(f"{_EMBED}.probe_embedding_backend", return_value=(True, "fastembed")), \
             patch(f"{_EMBED}._get_embedder", return_value=sentinel):
            embedder, reason = load_embedder("fastembed")

        assert embedder is sentinel
        assert reason is None


class TestInMemoryResolverSignature:

    def test_accepts_repo_path_keyword_as_phase5_passes_it(self):
        """Phase 5 calls resolve() with repo_path=...; the in-memory resolver must accept it."""
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        resolver = InMemoryVectorResolver(
            _fake_embedder(default=(1.0, 0.0, 0.0)),
            [("run", "/a.py", [1.0, 0.0, 0.0])],
        )
        assert resolver.resolve(
            called_name="run",
            caller_qualified_name=None,
            candidate_paths=["/a.py"],
            repo_path="/repo",
        ) == "/a.py"

    def test_records_built_with_normalised_path_resolve_to_that_path(self):
        """Pipeline builds records with the writer's path form; candidates use the same form."""
        from codegraphcontext.tools.indexing.vector_resolver import InMemoryVectorResolver

        records = [{"name": "run", "path": "C:/repo/a.ts", "line_number": 1}]
        resolver = InMemoryVectorResolver.from_function_records(
            _fake_embedder(default=(1.0, 0.0, 0.0)), records, {0: [1.0, 0.0, 0.0]}
        )
        assert resolver.resolve("run", None, ["C:/repo/a.ts"], repo_path="C:/repo") == "C:/repo/a.ts"
