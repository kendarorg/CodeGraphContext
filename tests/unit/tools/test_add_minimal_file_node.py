from pathlib import Path
from types import SimpleNamespace

from codegraphcontext.tools.indexing.persistence.writer import GraphWriter, _normalize_path


class FakeSession:
    """Records (normalized query, params) for every run."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.calls.append((" ".join(query.split()), params))
        return SimpleNamespace()


class FakeDriver:
    def __init__(self):
        self.session_obj = FakeSession()

    def get_backend_type(self):
        return "neo4j"

    def session(self):
        return self.session_obj


def _writer(driver):
    db_manager = SimpleNamespace(get_backend_type=lambda: "neo4j")
    return GraphWriter(driver=driver, db_manager=db_manager)


def test_nested_file_writes_repository_directory_chain_and_contains_links(tmp_path):
    repo = tmp_path / "repo"
    file_path = repo / "a" / "b" / "f.py"
    driver = FakeDriver()

    _writer(driver).add_minimal_file_node(file_path, repo, is_dependency=False)

    calls = driver.session_obj.calls
    assert len(calls) == 5

    repo_s = _normalize_path(repo)
    dir_a = _normalize_path(repo / "a")
    dir_b = _normalize_path(repo / "a" / "b")
    file_s = _normalize_path(file_path)

    # 1. Repository node
    q, p = calls[0]
    assert "MERGE (r:Repository {path: $repo_path})" in q
    assert p == {"repo_path": repo_s, "repo_name": "repo"}

    # 2. File node
    q, p = calls[1]
    assert "MERGE (f:File {path: $file_path})" in q
    assert p == {"file_path": file_s, "file_name": "f.py", "is_dependency": False}

    # 3. Repository -> Directory a
    q, p = calls[2]
    assert "MATCH (p:Repository {path: $parent_path})" in q
    assert "MERGE (d:Directory {path: $current_path})" in q
    assert "MERGE (p)-[:CONTAINS]->(d)" in q
    assert p == {"parent_path": repo_s, "current_path": dir_a, "part": "a"}

    # 4. Directory a -> Directory b
    q, p = calls[3]
    assert "MATCH (p:Directory {path: $parent_path})" in q
    assert "MERGE (d:Directory {path: $current_path})" in q
    assert "MERGE (p)-[:CONTAINS]->(d)" in q
    assert p == {"parent_path": dir_a, "current_path": dir_b, "part": "b"}

    # 5. Directory b -> File
    q, p = calls[4]
    assert "MATCH (p:Directory {path: $parent_path})" in q
    assert "MATCH (f:File {path: $file_path})" in q
    assert "MERGE (p)-[:CONTAINS]->(f)" in q
    assert p == {"parent_path": dir_b, "file_path": file_s}


def test_file_at_repository_root_links_repository_to_file(tmp_path):
    repo = tmp_path / "repo"
    file_path = repo / "main.py"
    driver = FakeDriver()

    _writer(driver).add_minimal_file_node(file_path, repo, is_dependency=True)

    calls = driver.session_obj.calls
    assert len(calls) == 3  # Repository, File, Repository -> File (no Directory nodes)
    q, p = calls[2]
    assert "MATCH (p:Repository {path: $parent_path})" in q
    assert "MERGE (p)-[:CONTAINS]->(f)" in q
    assert p == {"parent_path": _normalize_path(repo), "file_path": _normalize_path(file_path)}
    assert calls[1][1]["is_dependency"] is True
