"""Repo-init phase 4 gate: embeddings run only when phase 5 will read the vectors."""

import pytest

from codegraphcontext.tools.indexing.pipeline import repo_init_embed_wanted


@pytest.mark.parametrize(
    "vector_on, inherit_on, expected",
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_repo_init_embed_requires_both_flags(vector_on, inherit_on, expected):
    assert repo_init_embed_wanted(vector_on, inherit_on) is expected
