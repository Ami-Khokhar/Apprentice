"""Generate all retained expense demonstrations from isolated local portals."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from playwright.sync_api import Browser, sync_playwright

from apprentice.recorder.artifacts import assert_no_sentinels, load_artifact
from apprentice.recorder.session import RecorderSession
from demo_portal.app import build_portal

SENTINELS = ("hunter2", "123456")
CANONICAL_ORIGIN = "http://127.0.0.1"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "fixtures" / "expense"


@dataclass(frozen=True)
class FixtureCase:
    name: str
    merchant: str
    amount: str
    justification: str
    failure_mode: str = "none"


CASES = (
    FixtureCase("training_1", "Acme Supplies", "42.50", ""),
    FixtureCase(
        "training_2",
        "Atlas Travel",
        "1250.00",
        "Client onsite travel required manager justification.",
    ),
    FixtureCase("heldout", "Northwind Books", "87.25", ""),
    FixtureCase(
        "changed_site",
        "Contoso Cafe",
        "63.40",
        "",
        failure_mode="commit_schema",
    ),
)


class FixedClock:
    """A deterministic capture clock; ordinals preserve event ordering."""

    def __init__(self, start: float = 1_720_000_000.0) -> None:
        self._value = start

    def __call__(self) -> float:
        return self._value


@contextmanager
def _portal(failure_mode: str) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            build_portal(failure_mode),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name=f"fixture-portal-{failure_mode}",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("fixture portal did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("fixture portal did not stop")


def _record_case(browser: Browser, case: FixtureCase, output_dir: Path) -> None:
    with _portal(case.failure_mode) as base_url:
        context = browser.new_context(
            viewport={"width": 1024, "height": 768},
            locale="en-US",
            timezone_id="UTC",
            color_scheme="light",
            reduced_motion="reduce",
        )
        try:
            page = context.new_page()
            page.goto(f"{base_url}/login")
            page.get_by_label("Username").fill("fixture-user")
            page.get_by_label("Password").fill(SENTINELS[0])
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/expense")

            recorder = RecorderSession(
                task="file expense",
                allowed_hosts=("127.0.0.1",),
                output_dir=output_dir,
                clock=FixedClock(),
                secret_values=SENTINELS,
                canonical_base_url=CANONICAL_ORIGIN,
            )
            recorder.start(page.url)
            observer = recorder.attach(page)

            if page.reload() is None:
                raise RuntimeError("expense page reload did not return a response")
            observer.capture_page("navigate")

            page.get_by_label("Merchant").fill(case.merchant)
            page.get_by_label("Amount").fill(case.amount)

            if case.justification:
                justification_label = "Justification for expenses over 1000"
                page.get_by_label(justification_label).fill(case.justification)

            receipt = (
                "%PDF-1.4\n"
                f"Fixture receipt for {case.merchant}: {case.amount}\n"
                "%%EOF\n"
            ).encode()
            page.get_by_label("Receipt").set_input_files(
                {"name": "receipt.pdf", "mimeType": "application/pdf", "buffer": receipt}
            )
            observer.capture_page()
            with page.expect_response(
                lambda item: item.request.method == "POST" and item.url.endswith("/expense")
            ):
                page.get_by_role("button", name="Submit expense").click()
            observer.capture_page()
            recorder.finalize()
        finally:
            context.close()


def generate_fixtures(output_root: str | Path = DEFAULT_OUTPUT) -> dict[str, str]:
    """Generate all four fixtures transactionally into the requested root."""

    destination = Path(output_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    with tempfile.TemporaryDirectory(
        prefix=".expense-fixtures-",
        dir=destination.parent,
    ) as temporary:
        staging = Path(temporary)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for case in CASES:
                    _record_case(browser, case, staging / case.name)
            finally:
                browser.close()

        assert_no_sentinels(staging, SENTINELS)
        for case in CASES:
            artifact = load_artifact(staging / case.name)
            digests[case.name] = artifact["artifact_digest"]

        destination.mkdir(parents=True, exist_ok=True)
        for case in CASES:
            target = destination / case.name
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging / case.name, target)
    assert_no_sentinels(destination, SENTINELS)
    return digests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="fixture root (default: fixtures/expense)",
    )
    args = parser.parse_args()
    digests = generate_fixtures(args.output)
    for name, artifact_digest in sorted(digests.items()):
        print(f"{name}: {artifact_digest}")


if __name__ == "__main__":
    main()
