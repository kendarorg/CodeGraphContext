"""Container fixtures for the parity suite.

Neo4j and NornicDB run in Docker containers started by testcontainers. The
Docker daemon is found through DOCKER_HOST when it is set (for example
tcp://192.168.1.45:2375), otherwise through the local default socket.

The testcontainers reaper (Ryuk) is disabled: it needs to pull and run an extra
container and fails on hosts with a remote daemon. Containers are stopped by the
context managers below, so a crashed session can leave one behind; `docker ps`
shows it, and `docker rm -f` removes it.
"""
import os

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import docker
import pytest
from testcontainers.community.neo4j import Neo4jContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

# Pinned by digest or exact tag. Neo4j matches the CI service it replaces.
NEO4J_IMAGE = "neo4j:5.12.0-community"
NORNICDB_IMAGE = (
    "timothyswt/nornicdb-amd64-cpu-headless"
    "@sha256:3108453b69789913033726eef1b87aa3e8f4f181db740ecbf900ede6135c5c37"
)

# Throwaway credentials for containers this suite starts and removes itself.
NEO4J_PASSWORD = "12345678"


@pytest.fixture(scope="session")
def docker_client():
    """Fail the run, rather than skip it, when no Docker daemon answers."""
    target = os.environ.get("DOCKER_HOST") or "default socket"
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.fail(f"Parity suite needs Docker; daemon not reachable at {target}: {exc}")
    return client


@pytest.fixture(scope="session")
def neo4j_env(docker_client):
    """Connection settings for a Neo4j container, started once per session."""
    with Neo4jContainer(NEO4J_IMAGE, username="neo4j", password=NEO4J_PASSWORD) as container:
        yield {
            "NEO4J_URI": container.get_connection_url(),
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": NEO4J_PASSWORD,
        }


@pytest.fixture(scope="session")
def nornic_env(docker_client):
    """Connection settings for a NornicDB container (auth off), once per session."""
    container = (
        DockerContainer(NORNICDB_IMAGE)
        .with_env("NORNICDB_NO_AUTH", "true")
        .with_exposed_ports(7474, 7687)
        .waiting_for(HttpWaitStrategy(7474, "/health").with_startup_timeout(120))
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(7687)
        yield {
            "NORNIC_URI": f"bolt://{host}:{port}",
            "NORNIC_USERNAME": "nornic",
            # The client refuses to connect without a password; auth is off.
            "NORNIC_PASSWORD": "unused",
        }
