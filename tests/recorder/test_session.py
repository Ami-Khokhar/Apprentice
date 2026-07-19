import hashlib
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from apprentice.recorder.artifacts import (
    ArtifactSafetyError,
    assert_no_sentinels,
    load_artifact,
    publish_artifact,
)
from apprentice.recorder.redact import REDACTED
from apprentice.recorder.session import RecorderSession, SemanticAnchor

SENTINELS = ("hunter2", "123456")


def _session(tmp_path: Path, name: str = "recording") -> RecorderSession:
    session = RecorderSession(
        task="file expense",
        allowed_hosts=("127.0.0.1",),
        output_dir=tmp_path / name,
        clock=lambda: 42.0,
        secret_values=SENTINELS,
        canonical_base_url="http://127.0.0.1",
    )
    session.start("http://127.0.0.1:8000/expense")
    return session


def test_sensitive_inputs_are_redacted_before_screenshot_or_persistence(tmp_path: Path) -> None:
    session = _session(tmp_path)
    calls = 0

    def screenshot() -> bytes:
        nonlocal calls
        calls += 1
        return b"should not be captured"

    password = session.record_step(
        "input",
        url="http://127.0.0.1:8000/expense",
        anchor=SemanticAnchor("textbox", "Password", "#password"),
        value="hunter2",
        field_name="password",
        input_type="password",
        screenshot=screenshot,
    )
    otp = session.record_step(
        "input",
        url="http://127.0.0.1:8000/expense",
        value="123456",
        field_name="verification_code",
        autocomplete="one-time-code",
        screenshot=screenshot,
    )

    assert calls == 0
    assert password is not None and password["value"] == REDACTED
    assert otp is not None and otp["value"] == REDACTED
    artifact_path = session.finalize()
    assert_no_sentinels(artifact_path.parent, SENTINELS)


def test_denylisted_host_produces_no_step_screenshot_or_http_entry(tmp_path: Path) -> None:
    session = _session(tmp_path)
    calls = 0

    def screenshot() -> bytes:
        nonlocal calls
        calls += 1
        return b"screen"

    assert (
        session.record_step(
            "navigate",
            url="https://denylisted.example/expense",
            page_state={"text": "unsafe"},
            screenshot=screenshot,
        )
        is None
    )
    assert (
        session.record_http(
            method="GET",
            url="https://denylisted.example/expense",
            status=200,
            response_body="unsafe",
        )
        is None
    )
    assert calls == 0
    assert session.steps == ()
    assert session.http_entries == ()


def test_caller_provided_http_is_sanitized_but_cannot_create_template(tmp_path: Path) -> None:
    def record(name: str) -> dict[str, object]:
        session = _session(tmp_path, name)
        session.record_step(
            "navigate",
            url="http://127.0.0.1:8000/expense?token=hunter2",
            page_state={"text": "File an expense"},
            screenshot=lambda: b"deterministic-png",
        )
        session.record_http(
            method="POST",
            url="http://127.0.0.1:8000/expense?token=hunter2",
            status=201,
            request_headers={
                "Authorization": "Bearer hunter2",
                "Cookie": "otp=123456",
                "Content-Type": "application/json",
            },
            request_body={"merchant": "Acme", "password": "hunter2"},
            response_headers={
                "Set-Cookie": "session=hunter2",
                "Content-Type": "text/html",
            },
            response_body="<h1>Expense submitted</h1>",
        )
        return load_artifact(session.finalize())

    first = record("first")
    second = record("second")
    assert first["artifact_digest"] == second["artifact_digest"]
    assert first["steps"][0]["url"] == (
        "http://127.0.0.1/expense?token=%5BREDACTED%5D"
    )
    assert first["http_entries"][0]["request"]["headers"] == {
        "content-type": "application/json"
    }
    assert first["http_entries"][0]["request"]["body"]["password"] == REDACTED
    assert first["http_entries"][0]["source"] == "provided"
    assert first["response_templates"] == []
    assert_no_sentinels(tmp_path, SENTINELS)


def test_page_attachment_observes_actions_actual_http_and_masks_secrets(
    tmp_path: Path,
    portal_server,
) -> None:
    receipt = b"actual browser receipt"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            page.goto(f"{portal_server.base_url}/login")
            page.get_by_label("Username").fill("recorder-test")
            page.get_by_label("Password").fill("hunter2")
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/expense")

            session = RecorderSession(
                task="file expense",
                allowed_hosts=("127.0.0.1",),
                output_dir=tmp_path / "browser-recording",
                clock=lambda: 42.0,
                secret_values=SENTINELS,
                canonical_base_url="http://127.0.0.1",
            )
            session.start(page.url)
            observer = session.attach(page)

            page.reload()
            observer.capture_page("navigate")
            page.evaluate(
                """async () => {
                  try {
                    await fetch("http://127.0.0.2:9/expense", {
                      method: "POST",
                      body: "hunter2",
                    });
                  } catch (_) {}
                }"""
            )
            observer.flush()
            assert observer.buffered_raw_request_count == 0
            page.get_by_label("Merchant").fill("Browser Observed Merchant")
            page.get_by_label("Amount").fill("73.45")
            page.get_by_label("Receipt").set_input_files(
                {"name": "receipt.pdf", "mimeType": "application/pdf", "buffer": receipt}
            )
            observer.capture_page()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/expense")
            ):
                page.get_by_role("button", name="Submit expense").click()
            observer.capture_page()

            page.locator("main").evaluate(
                """main => main.insertAdjacentHTML("beforeend", `
                  <label for="late-password">Late password</label>
                  <input id="late-password" name="password" type="password">
                  <label for="late-otp">Late OTP</label>
                  <input id="late-otp" name="otp" autocomplete="one-time-code">
                `)"""
            )
            page.get_by_label("Late password").fill("hunter2")
            page.get_by_label("Late OTP").fill("123456")
            first_masked = observer.capture_page()
            page.evaluate(
                """() => {
                  document.querySelector("#late-password").value = "different-secret";
                  document.querySelector("#late-otp").value = "999999999";
                }"""
            )
            second_masked = observer.capture_page()
            outward_template = session.response_templates[0]
            outward_template["request"]["body"]["fields"]["merchant"] = "tampered"
            artifact = load_artifact(session.finalize())
            assert observer.buffered_raw_request_count == 0
        finally:
            browser.close()

    actions = [step["action"] for step in artifact["steps"]]
    assert {"click", "input", "submit", "upload"}.issubset(actions)
    post = next(entry for entry in artifact["http_entries"] if entry["method"] == "POST")
    assert post["source"] == "observed"
    assert post["request"]["body"]["fields"] == {
        "amount": "73.45",
        "justification": "",
        "merchant": "Browser Observed Merchant",
    }
    assert post["request"]["body"]["files"] == [
        {
            "content_type": "application/pdf",
            "field": "receipt",
            "filename": "receipt.pdf",
            "sha256": hashlib.sha256(receipt).hexdigest(),
        }
    ]
    template = artifact["response_templates"][0]
    assert template["source"] == "observed"
    assert template["request"] == {
        "method": post["method"],
        "url": post["url"],
        "headers": post["request"]["headers"],
        "body": post["request"]["body"],
    }
    assert template["response"] == post["response"]
    assert template["request"]["headers"] == {"content-type": "multipart/form-data"}
    assert all("/login" not in entry["url"] for entry in artifact["http_entries"])
    assert first_masked is not None and second_masked is not None
    assert first_masked["screenshot"]["sha256"] == second_masked["screenshot"]["sha256"]
    assert all(step.get("value") == REDACTED for step in artifact["steps"] if step.get("redacted"))
    assert_no_sentinels(tmp_path / "browser-recording", SENTINELS)


def test_staging_is_cleaned_when_final_sentinel_scan_fails(tmp_path: Path) -> None:
    screenshot = b"image bytes containing hunter2"
    artifact = {
        "task": "file expense",
        "allowed_hosts": ["127.0.0.1"],
        "steps": [
            {
                "ordinal": 0,
                "action": "navigate",
                "screenshot": {
                    "path": "screenshots/000-navigate.png",
                    "sha256": hashlib.sha256(screenshot).hexdigest(),
                },
            }
        ],
        "http_entries": [],
        "response_templates": [],
    }
    target = tmp_path / "retained"
    with pytest.raises(ArtifactSafetyError):
        publish_artifact(
            target,
            artifact,
            screenshots={"screenshots/000-navigate.png": screenshot},
            secret_values=SENTINELS,
        )
    assert not target.exists()
    assert list(tmp_path.glob(".retained.tmp-*")) == []


def test_fixture_artifacts_are_distinct_and_capture_meaningful_change() -> None:
    root = Path(__file__).resolve().parents[2] / "fixtures" / "expense"
    artifacts = {
        name: load_artifact(root / name)
        for name in ("training_1", "training_2", "heldout", "changed_site")
    }
    input_values = {
        name: [
            step.get("value")
            for step in artifact["steps"]
            if step["action"] == "input"
        ]
        for name, artifact in artifacts.items()
    }
    assert input_values["training_1"] != input_values["training_2"]
    assert artifacts["heldout"]["artifact_digest"] not in {
        artifacts["training_1"]["artifact_digest"],
        artifacts["training_2"]["artifact_digest"],
    }
    changed_post = next(
        entry for entry in artifacts["changed_site"]["http_entries"] if entry["method"] == "POST"
    )
    assert "total_amount" in changed_post["request"]["body"]["fields"]
    assert "amount" not in changed_post["request"]["body"]["fields"]
    assert_no_sentinels(root, SENTINELS)


def test_artifact_json_is_plain_utf8_json(tmp_path: Path) -> None:
    session = _session(tmp_path)
    path = session.finalize()
    assert json.loads(path.read_text(encoding="utf-8"))["task"] == "file expense"
