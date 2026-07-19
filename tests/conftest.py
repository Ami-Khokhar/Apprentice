import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import uvicorn

from demo_portal.app import build_portal


@dataclass(frozen=True)
class RunningServer:
    base_url: str


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def portal_server() -> Iterator[RunningServer]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            build_portal(),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name="test-portal",
            daemon=True,
        )
        thread.start()

        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            pytest.fail("portal server did not start")

        yield RunningServer(base_url=f"http://127.0.0.1:{port}")
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        listener.close()
        if thread is not None and thread.is_alive():
            pytest.fail("portal server did not stop")
