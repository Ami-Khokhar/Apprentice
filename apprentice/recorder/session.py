"""A small post-login recorder that never stores unsanitized events."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import CDPSession, Page, Request

from apprentice.canonical import canonical_json, digest
from apprentice.recorder.artifacts import ArtifactSafetyError, publish_artifact
from apprentice.recorder.redact import (
    DEFAULT_SENSITIVE_KEYS,
    REDACTED,
    is_sensitive_field,
    sanitize_body,
    sanitize_headers,
    sanitize_url,
    sanitize_value,
)

Clock = Callable[[], float]
Screenshotter = Callable[[], bytes]
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_OBSERVED_RESOURCE_TYPES = frozenset({"document", "fetch", "xhr"})
_DOM_BINDING_NAME = "__apprenticeRecordEvent"
_SENSITIVE_SCREENSHOT_SELECTOR = ", ".join(
    (
        "input[type='password']",
        "input[autocomplete~='current-password' i]",
        "input[autocomplete~='new-password' i]",
        "input[autocomplete~='one-time-code' i]",
        "input[name*='password' i]",
        "input[name*='passcode' i]",
        "input[name*='otp' i]",
        "input[name*='token' i]",
        "textarea[name*='password' i]",
        "textarea[name*='passcode' i]",
        "textarea[name*='otp' i]",
        "[data-apprentice-sensitive]",
    )
)

_DOM_OBSERVER = r"""
(() => {
  const bindingName = __BINDING_NAME__;
  if (window.__apprenticeRecorderInstalled) return;
  window.__apprenticeRecorderInstalled = true;

  const accessibleName = element => {
    const labels = element.labels ? Array.from(element.labels) : [];
    const label = labels.map(item => item.innerText.trim()).filter(Boolean).join(" ");
    return element.getAttribute("aria-label") || label || element.innerText.trim() ||
      element.getAttribute("name") || "";
  };
  const cssFallback = element => {
    if (element.id) return `#${CSS.escape(element.id)}`;
    const name = element.getAttribute("name");
    if (name) return `${element.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    return element.tagName.toLowerCase();
  };
  const role = element => {
    const explicit = element.getAttribute("role");
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    if (tag === "button" || (tag === "input" && ["button", "file", "submit"].includes(type))) {
      return "button";
    }
    if (tag === "a") return "link";
    if (tag === "select") return "combobox";
    if (tag === "input" && type === "number") return "spinbutton";
    return "textbox";
  };
  const describe = element => ({
    url: window.location.href,
    anchor: {role: role(element), name: accessibleName(element), css: cssFallback(element)},
    field_name: element.getAttribute("name"),
    input_type: (element.getAttribute("type") || "").toLowerCase(),
    autocomplete: element.getAttribute("autocomplete"),
  });
  const emit = payload => {
    Promise.resolve(window[bindingName](payload)).catch(() => {});
  };

  document.addEventListener("input", event => {
    const element = event.target;
    if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement ||
          element instanceof HTMLSelectElement)) return;
    if (element instanceof HTMLInputElement && element.type === "file") return;
    emit({...describe(element), action: "input", value: element.value});
  }, true);
  document.addEventListener("change", event => {
    const element = event.target;
    if (!(element instanceof HTMLInputElement) || element.type !== "file") return;
    const files = Array.from(element.files || []).map(file => ({
      filename: file.name,
      content_type: file.type || "application/octet-stream",
      size: file.size,
    }));
    emit({...describe(element), action: "upload", value: {files}});
  }, true);
  document.addEventListener("click", event => {
    const element = event.target.closest("button, a, input[type='button'], input[type='submit']");
    if (!element) return;
    emit({...describe(element), action: "click"});
  }, true);
  document.addEventListener("submit", event => {
    const element = event.submitter || event.target;
    emit({...describe(element), action: "submit"});
  }, true);
})();
"""

_PAGE_STATE_SCRIPT = r"""
() => {
  const controls = Array.from(document.querySelectorAll("input, textarea, select, button, a"));
  const accessibleName = element => {
    const labels = element.labels ? Array.from(element.labels) : [];
    const label = labels.map(item => item.innerText.trim()).filter(Boolean).join(" ");
    return element.getAttribute("aria-label") || label || element.innerText.trim() ||
      element.getAttribute("name") || "";
  };
  const role = element => {
    const explicit = element.getAttribute("role");
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    if (tag === "button" || (tag === "input" && ["button", "file", "submit"].includes(type))) {
      return "button";
    }
    if (tag === "a") return "link";
    if (tag === "select") return "combobox";
    if (tag === "input" && type === "number") return "spinbutton";
    return "textbox";
  };
  const css = element => element.id ? `#${CSS.escape(element.id)}` :
    (element.getAttribute("name") ?
      `${element.tagName.toLowerCase()}[name="${CSS.escape(element.getAttribute("name"))}"]` :
      element.tagName.toLowerCase());
  const main = document.querySelector("main");
  return {
    title: document.title,
    text: main ? main.innerText : document.body.innerText,
    controls: controls.map(element => ({
      role: role(element),
      name: accessibleName(element),
      css: css(element),
      field_name: element.getAttribute("name"),
      input_type: (element.getAttribute("type") || "").toLowerCase(),
      autocomplete: element.getAttribute("autocomplete"),
    })),
  };
}
"""


class RecorderStateError(RuntimeError):
    """Raised when recorder methods are used outside an active session."""


@dataclass(frozen=True)
class SemanticAnchor:
    """A semantic locator with a deterministic CSS fallback."""

    role: str
    name: str
    css: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "name": self.name, "css": self.css}


Anchor = SemanticAnchor


class RecorderSession:
    """Collect a sanitized demonstration and publish it only after a final scan."""

    def __init__(
        self,
        *,
        task: str,
        allowed_hosts: Iterable[str],
        output_dir: str | Path | None = None,
        clock: Clock = time.time,
        sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
        secret_values: Iterable[str] = (),
        canonical_base_url: str | None = None,
    ) -> None:
        self.task = task
        self.allowed_hosts = tuple(sorted({host.casefold().strip() for host in allowed_hosts}))
        if not self.allowed_hosts:
            raise ValueError("at least one allowed host is required")
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self._clock = clock
        self._sensitive_keys = frozenset(sensitive_keys)
        self._secret_values = tuple(secret for secret in secret_values if secret)
        self._canonical_base_url = canonical_base_url.rstrip("/") if canonical_base_url else None
        self._steps: list[dict[str, Any]] = []
        self._http_entries: list[dict[str, Any]] = []
        self._response_templates: list[dict[str, Any]] = []
        self._screenshots: dict[str, bytes] = {}
        self._started = False
        self._closed = False
        self._attachment: BrowserRecorderAttachment | None = None

        if self._canonical_base_url is not None:
            canonical = urlsplit(self._canonical_base_url)
            if canonical.scheme not in {"http", "https"} or not self._host_is_allowed(
                self._canonical_base_url
            ):
                raise ValueError("canonical_base_url must be an allowed HTTP origin")
            if canonical.path not in {"", "/"} or canonical.query or canonical.fragment:
                raise ValueError("canonical_base_url must not include a path, query, or fragment")

    @property
    def steps(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(step) for step in self._steps)

    @property
    def http_entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(entry) for entry in self._http_entries)

    @property
    def response_templates(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(template) for template in self._response_templates)

    def _host_is_allowed(self, url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").casefold()
        return hostname in self.allowed_hosts

    def _canonical_url(self, url: str) -> str:
        clean = sanitize_url(
            url,
            sensitive_keys=self._sensitive_keys,
            secret_values=self._secret_values,
        )
        if self._canonical_base_url is None or not self._host_is_allowed(clean):
            return clean
        parsed = urlsplit(clean)
        canonical = urlsplit(self._canonical_base_url)
        return urlunsplit(
            (canonical.scheme, canonical.netloc, parsed.path, parsed.query, parsed.fragment)
        )

    def start(self, url: str) -> None:
        """Start only after authentication, on an allowed non-login page."""

        if self._started:
            raise RecorderStateError("recorder session has already started")
        if not self._host_is_allowed(url):
            raise ArtifactSafetyError("recording cannot start on a denylisted host")
        if urlsplit(url).path.rstrip("/").casefold() != "/expense":
            raise ArtifactSafetyError("recording must start on the post-login /expense page")
        self._started = True

    def attach(self, page: Page) -> BrowserRecorderAttachment:
        """Observe DOM actions and completed browser HTTP exchanges from a Playwright page."""

        self._ensure_active()
        if self._attachment is not None:
            raise RecorderStateError("a browser page is already attached")
        if not self._host_is_allowed(page.url):
            raise ArtifactSafetyError("cannot attach a denylisted browser page")
        self._attachment = BrowserRecorderAttachment(self, page)
        return self._attachment

    def _ensure_active(self) -> None:
        if not self._started:
            raise RecorderStateError("recorder session has not started")
        if self._closed:
            raise RecorderStateError("recorder session is closed")

    def _raw_contains_secret(self, value: Any) -> bool:
        try:
            encoded = canonical_json(value)
        except (TypeError, ValueError):
            encoded = repr(value).encode("utf-8", errors="replace")
        return any(secret.encode("utf-8") in encoded for secret in self._secret_values)

    def record_step(
        self,
        action: str,
        *,
        url: str,
        anchor: SemanticAnchor | Mapping[str, str] | None = None,
        value: Any = None,
        field_name: str | None = None,
        input_type: str | None = None,
        autocomplete: str | None = None,
        page_state: Any = None,
        screenshot: Screenshotter | None = None,
    ) -> dict[str, Any] | None:
        """Record one action; denied URLs cannot trigger screenshots or persistence."""

        self._ensure_active()
        if not self._host_is_allowed(url):
            return None

        sensitive = is_sensitive_field(
            name=field_name,
            input_type=input_type,
            autocomplete=autocomplete,
            sensitive_keys=self._sensitive_keys,
        )
        clean_page_state = sanitize_value(
            page_state,
            sensitive_keys=self._sensitive_keys,
            secret_values=self._secret_values,
        )
        raw_page_had_secret = self._raw_contains_secret(page_state)
        clean_value = (
            REDACTED
            if sensitive
            else sanitize_value(
                value,
                sensitive_keys=self._sensitive_keys,
                secret_values=self._secret_values,
                key=field_name,
            )
        )
        clean_anchor: dict[str, Any] | None = None
        if anchor is not None:
            raw_anchor = anchor.as_dict() if isinstance(anchor, SemanticAnchor) else dict(anchor)
            clean_anchor = sanitize_value(
                raw_anchor,
                sensitive_keys=self._sensitive_keys,
                secret_values=self._secret_values,
            )

        ordinal = len(self._steps)
        step: dict[str, Any] = {
            "ordinal": ordinal,
            "action": action,
            "url": self._canonical_url(url),
            "captured_at": float(self._clock()),
        }
        if clean_anchor is not None:
            step["anchor"] = clean_anchor
        if value is not None or sensitive:
            step["value"] = clean_value
        if page_state is not None:
            step["page_state"] = clean_page_state
        if sensitive:
            step["redacted"] = True

        if screenshot is not None and not sensitive and not raw_page_had_secret:
            content = screenshot()
            if not isinstance(content, bytes):
                raise TypeError("screenshot callback must return bytes")
            if any(secret.encode("utf-8") in content for secret in self._secret_values):
                raise ArtifactSafetyError("screenshot contains a configured secret sentinel")
            slug = re.sub(r"[^a-z0-9]+", "-", action.casefold()).strip("-") or "step"
            relative = f"screenshots/{ordinal:03d}-{slug}.png"
            screenshot_digest = hashlib.sha256(content).hexdigest()
            self._screenshots[relative] = content
            step["screenshot"] = {"path": relative, "sha256": screenshot_digest}

        self._steps.append(step)
        return copy.deepcopy(step)

    @staticmethod
    def _header_value(headers: Mapping[str, str], name: str) -> str | None:
        expected = name.casefold()
        return next(
            (value for key, value in headers.items() if key.casefold() == expected),
            None,
        )

    def record_http(
        self,
        *,
        method: str,
        url: str,
        status: int,
        request_headers: Mapping[str, str] | None = None,
        request_body: Any = None,
        response_headers: Mapping[str, str] | None = None,
        response_body: Any = None,
    ) -> dict[str, Any] | None:
        """Retain caller-provided HTTP context without granting observed provenance."""

        return self._retain_http(
            method=method,
            url=url,
            status=status,
            request_headers=request_headers,
            request_body=request_body,
            response_headers=response_headers,
            response_body=response_body,
            source="provided",
            create_response_template=False,
        )

    def _record_observed_http(
        self,
        observer: BrowserRecorderAttachment,
        *,
        method: str,
        url: str,
        status: int,
        request_headers: Mapping[str, str] | None = None,
        request_body: Any = None,
        response_headers: Mapping[str, str] | None = None,
        response_body: Any = None,
    ) -> dict[str, Any] | None:
        if observer is not self._attachment:
            raise ArtifactSafetyError("observed HTTP provenance requires the attached browser")
        return self._retain_http(
            method=method,
            url=url,
            status=status,
            request_headers=request_headers,
            request_body=request_body,
            response_headers=response_headers,
            response_body=response_body,
            source="observed",
            create_response_template=True,
        )

    def _retain_http(
        self,
        *,
        method: str,
        url: str,
        status: int,
        request_headers: Mapping[str, str] | None,
        request_body: Any,
        response_headers: Mapping[str, str] | None,
        response_body: Any,
        source: str,
        create_response_template: bool,
    ) -> dict[str, Any] | None:
        """Sanitize one exchange; only the attachment can request observed templates."""

        self._ensure_active()
        if not self._host_is_allowed(url):
            return None
        normalized_method = method.upper().strip()
        raw_request_headers = dict(request_headers or {})
        raw_response_headers = dict(response_headers or {})
        request_content_type = self._header_value(raw_request_headers, "content-type")
        response_content_type = self._header_value(raw_response_headers, "content-type")
        request = {
            "headers": sanitize_headers(
                raw_request_headers,
                secret_values=self._secret_values,
            ),
            "body": sanitize_body(
                request_body,
                request_content_type,
                sensitive_keys=self._sensitive_keys,
                secret_values=self._secret_values,
            ),
        }
        response = {
            "status": int(status),
            "headers": sanitize_headers(
                raw_response_headers,
                secret_values=self._secret_values,
            ),
            "body": sanitize_body(
                response_body,
                response_content_type,
                sensitive_keys=self._sensitive_keys,
                secret_values=self._secret_values,
            ),
        }
        entry = {
            "ordinal": len(self._http_entries),
            "method": normalized_method,
            "url": self._canonical_url(url),
            "request": request,
            "response": response,
            "source": source,
            "captured_at": float(self._clock()),
        }
        self._http_entries.append(entry)

        if normalized_method in _MUTATING_METHODS and create_response_template:
            template_body = {
                "request": {
                    "method": normalized_method,
                    "url": entry["url"],
                    "headers": request["headers"],
                    "body": request["body"],
                },
                "response": copy.deepcopy(response),
                "source": "observed",
            }
            template = {
                "id": digest(template_body),
                **template_body,
            }
            self._response_templates.append(template)
        return copy.deepcopy(entry)

    def artifact(self) -> dict[str, Any]:
        """Return the sanitized in-memory artifact before digest finalization."""

        self._ensure_active()
        return {
            "task": self.task,
            "allowed_hosts": list(self.allowed_hosts),
            "steps": copy.deepcopy(self._steps),
            "http_entries": copy.deepcopy(self._http_entries),
            "response_templates": copy.deepcopy(self._response_templates),
        }

    def finalize(self, output_dir: str | Path | None = None) -> Path:
        """Final-sanitize, scan, and publish the retained artifact tree."""

        self._ensure_active()
        attachment = self._attachment
        try:
            if attachment is not None:
                attachment.flush()
            destination = Path(output_dir) if output_dir is not None else self.output_dir
            if destination is None:
                raise ValueError("an output directory is required")
            artifact_path = publish_artifact(
                destination,
                self.artifact(),
                screenshots=self._screenshots,
                sensitive_keys=self._sensitive_keys,
                secret_values=self._secret_values,
            )
        finally:
            if attachment is not None:
                attachment.discard_raw()
        self._closed = True
        return artifact_path

    def abort(self) -> None:
        """Discard all in-memory data; no raw artifact is ever written."""

        if self._attachment is not None:
            self._attachment.discard_raw()
        self._steps.clear()
        self._http_entries.clear()
        self._response_templates.clear()
        self._screenshots.clear()
        self._closed = True

    def __enter__(self) -> RecorderSession:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None and not self._closed:
            self.abort()


SessionRecorder = RecorderSession


class BrowserRecorderAttachment:
    """Chromium observer binding provenance to browser DOM and network events.

    CDP Fetch interception is intentionally scoped to Chromium multipart mutations because
    Playwright omits their file-bearing request body from ``Request.post_data_buffer``.
    """

    def __init__(self, session: RecorderSession, page: Page) -> None:
        self._session = session
        self.page = page
        self._errors: list[Exception] = []
        self._pending_requests: dict[int, dict[str, Any]] = {}
        self._cdp_bodies: defaultdict[tuple[str, str], deque[bytes | str]] = defaultdict(deque)
        self._cdp: CDPSession = page.context.new_cdp_session(page)
        self._closed = False
        self._cdp.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
        )
        self._cdp.on("Fetch.requestPaused", self._on_cdp_request)
        observer = _DOM_OBSERVER.replace("__BINDING_NAME__", json.dumps(_DOM_BINDING_NAME))
        page.expose_binding(_DOM_BINDING_NAME, self._on_dom_event)
        page.add_init_script(observer)
        page.evaluate(observer)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_failed)

    def _on_dom_event(self, _source: Mapping[str, Any], payload: Any) -> None:
        if self._closed:
            return
        try:
            if not isinstance(payload, Mapping):
                raise TypeError("browser recorder event payload must be a mapping")
            action = str(payload.get("action", ""))
            if action not in {"click", "input", "upload", "submit"}:
                raise ValueError(f"unsupported observed DOM action: {action!r}")
            anchor = payload.get("anchor")
            self._session.record_step(
                action,
                url=str(payload.get("url", self.page.url)),
                anchor=anchor if isinstance(anchor, Mapping) else None,
                value=payload.get("value"),
                field_name=_optional_string(payload.get("field_name")),
                input_type=_optional_string(payload.get("input_type")),
                autocomplete=_optional_string(payload.get("autocomplete")),
            )
        except Exception as error:  # callbacks surface errors through flush()
            self._errors.append(error)

    @staticmethod
    def _relevant_headers(headers: Mapping[str, str]) -> dict[str, str]:
        retained = {"content-encoding", "content-type"}
        return {name: value for name, value in headers.items() if name.casefold() in retained}

    def _on_cdp_request(self, event: Mapping[str, Any]) -> None:
        request_id = event.get("requestId")
        try:
            if self._closed:
                return
            request = event.get("request")
            if not isinstance(request, Mapping):
                return
            method = request.get("method")
            url = request.get("url")
            if not isinstance(method, str) or not isinstance(url, str):
                return
            resource_type = str(event.get("resourceType", "")).casefold()
            if method.upper() not in _MUTATING_METHODS:
                return
            if resource_type not in _OBSERVED_RESOURCE_TYPES:
                return
            if not self._session._host_is_allowed(url):
                return
            post_data: bytes | str | None = None
            raw_post_data = request.get("postData")
            if isinstance(raw_post_data, str):
                post_data = raw_post_data
            entries = request.get("postDataEntries")
            if isinstance(entries, list) and entries:
                chunks = [
                    base64.b64decode(entry["bytes"])
                    for entry in entries
                    if isinstance(entry, Mapping) and isinstance(entry.get("bytes"), str)
                ]
                if chunks:
                    post_data = b"".join(chunks)
            if post_data is not None:
                self._cdp_bodies[(method.upper(), url)].append(post_data)
        finally:
            if isinstance(request_id, str):
                self._cdp.send("Fetch.continueRequest", {"requestId": request_id})

    def _on_request_finished(self, request: Request) -> None:
        try:
            observed_request = self._pending_requests.get(id(request))
            if observed_request is None:
                return
            response = request.existing_response
            if response is None:
                raise RecorderStateError(f"finished request has no response: {request.url}")
            request_body = observed_request["body"]
            key = (str(observed_request["method"]).upper(), str(observed_request["url"]))
            cdp_body = self._consume_cdp_body(key)
            if request_body is None:
                request_body = cdp_body
            is_mutating = str(observed_request["method"]).upper() in _MUTATING_METHODS
            if is_mutating and request_body is None:
                raise RecorderStateError("mutating browser request body was not observable")
            self._session._record_observed_http(
                self,
                method=observed_request["method"],
                url=observed_request["url"],
                status=response.status,
                request_headers=observed_request["headers"],
                request_body=request_body,
                response_headers=self._relevant_headers(response.all_headers()),
                response_body=response.body(),
            )
        except Exception as error:  # callbacks surface errors through flush()
            self._errors.append(error)
        finally:
            self._pending_requests.pop(id(request), None)

    def _on_request(self, request: Request) -> None:
        if self._closed:
            return
        if request.resource_type not in _OBSERVED_RESOURCE_TYPES:
            return
        if self._session._host_is_allowed(request.url):
            body = request.post_data_buffer
            self._pending_requests[id(request)] = {
                "method": request.method,
                "url": request.url,
                "headers": self._relevant_headers(request.all_headers()),
                "body": body if body is not None else request.post_data,
            }

    def _on_request_failed(self, request: Request) -> None:
        observed_request = self._pending_requests.pop(id(request), None)
        if observed_request is not None:
            key = (str(observed_request["method"]).upper(), str(observed_request["url"]))
            self._consume_cdp_body(key)

    def _consume_cdp_body(self, key: tuple[str, str]) -> bytes | str | None:
        queue = self._cdp_bodies.get(key)
        if not queue:
            self._cdp_bodies.pop(key, None)
            return None
        body = queue.popleft()
        if not queue:
            self._cdp_bodies.pop(key, None)
        return body

    def flush(self) -> None:
        """Drain browser callbacks and surface observation failures synchronously."""

        self.page.wait_for_load_state("load")
        self.page.wait_for_timeout(0)
        if self._pending_requests:
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(0)
        if self._pending_requests:
            raise RecorderStateError("browser requests did not finish before artifact finalization")
        if self._cdp_bodies:
            raise RecorderStateError("raw browser request bodies were not consumed")
        if self._errors:
            error = self._errors.pop(0)
            raise RecorderStateError("browser observation failed") from error

    def discard_raw(self) -> None:
        """Detach observers and erase request bytes that were not retained safely."""

        if self._closed:
            return
        self._closed = True
        self._pending_requests.clear()
        self._cdp_bodies.clear()
        self._errors.clear()
        with suppress(Exception):
            self.page.remove_listener("request", self._on_request)
            self.page.remove_listener("requestfinished", self._on_request_finished)
            self.page.remove_listener("requestfailed", self._on_request_failed)
        with suppress(Exception):
            self._cdp.send("Fetch.disable")
        with suppress(Exception):
            self._cdp.detach()

    @property
    def buffered_raw_request_count(self) -> int:
        """Expose a lifecycle diagnostic without exposing buffered request content."""

        return len(self._pending_requests) + sum(len(queue) for queue in self._cdp_bodies.values())

    def _page_state(self) -> dict[str, Any]:
        state = self.page.evaluate(_PAGE_STATE_SCRIPT)
        if not isinstance(state, dict):
            raise RecorderStateError("page-state observer returned an invalid value")
        state["url"] = self._session._canonical_url(self.page.url)
        return state

    def _safe_screenshot(self) -> bytes:
        sensitive_fields = self.page.locator(_SENSITIVE_SCREENSHOT_SELECTOR)
        return self.page.screenshot(
            animations="disabled",
            caret="hide",
            full_page=True,
            mask=[sensitive_fields],
            mask_color="#000000",
        )

    def capture_page(self, action: str = "page_state") -> dict[str, Any] | None:
        """Retain a page excerpt and screenshot with every sensitive field masked."""

        self.flush()
        if not self._session._host_is_allowed(self.page.url):
            return None
        return self._session.record_step(
            action,
            url=self.page.url,
            page_state=self._page_state(),
            screenshot=self._safe_screenshot,
        )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
