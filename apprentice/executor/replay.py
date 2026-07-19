"""Exact deterministic production replay (plan invariant 3, plan §6.4).

The sidecar owns the single ``ProductionReplayer``: the model never receives
a raw Playwright page, only ever the ``activate_rehearsed_plan(run_id)``
result. Every action is loaded from ``run_actions`` --- never anywhere else
--- ordered by ``ordinal``, and consumed exactly once. The plan hash is
recomputed from those stored rows and checked against ``runs.action_plan_hash``
before the first navigation. Anchors are resolved against the live
accessibility tree (Playwright's role/name locators, cross-checked against
the recorded CSS identity). The one recorded mutating request is intercepted
--- method, normalized URL, structured multipart payload, and file hashes
compared against the rehearsed commit --- and forwarded to production
exactly once, only when it matches and the run is executing. Any mismatch
aborts before forwarding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email import message_from_bytes
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from apprentice.canonical import digest, normalize_hostname
from apprentice.induction.induce import Playbook
from apprentice.ledger.repository import ConflictError, Repository
from apprentice.models import FailureCause, RecordedAction, RunState
from apprentice.sidecar.run_service import RunService

__all__ = [
    "FileResolver",
    "ProductionReplayer",
    "ReplayAbortError",
    "ReplayOutcome",
    "execute_authorized_run",
]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_COMMIT_RESPONSE_TIMEOUT_MS = 5_000
# Anchors either resolve promptly on a loaded page or they have drifted;
# a short timeout keeps drift aborts fast instead of hanging for 30s.
_LOCATOR_TIMEOUT_MS = 3_000

# (filename, sha256) -> raw file bytes; how a real deployment would resolve
# them is out of this task's scope, so callers (including every test) inject
# this rather than the replayer inventing a storage backend.
FileResolver = Callable[[str, str], bytes]


class ReplayAbortError(RuntimeError):
    """Raised when the live target ever diverges from the stored rehearsed plan."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReplayOutcome:
    succeeded: bool
    mutation_forwarded: bool
    failure_cause: FailureCause | None
    abort_reason: str | None
    final_status: int | None
    final_body: str | None


def _normalize_request_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


def _parse_multipart(content_type: str, body: bytes) -> dict[str, Any]:
    """Reconstruct the twin's ``{"kind": "multipart", "fields", "files"}`` body shape.

    Parses the *real* outgoing multipart request the browser produced, using
    the same field/file shape ``TwinEnvironment.commit`` recorded, so the two
    can be compared with the same ``digest()``.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = message_from_bytes(header + body)
    fields: dict[str, str] = {}
    files: list[dict[str, str]] = []
    seen_names: set[str] = set()
    if not message.is_multipart():
        return {"kind": "multipart", "fields": fields, "files": files}
    for part in message.get_payload():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        if name in seen_names:
            raise ReplayAbortError("duplicate_multipart_field")
        seen_names.add(name)
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append(
                {
                    "field": name,
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")
    return {"kind": "multipart", "fields": fields, "files": files}


class _MutationGuard:
    """Route interception for the WHOLE replay, never just the commit step.

    Invariant 3 exists precisely because the live target is not trusted: any
    mutating request it produces at any point --- a load-time beacon, a
    click-triggered POST, anything --- is aborted unless it is exactly the
    expected rehearsed commit, occurring during the commit step (armed), and
    no mutation has been forwarded yet. The expected commit is forwarded
    exactly once; ``forwarded`` makes at-most-once defensive, not incidental.

    Invariant 5 ("unknown requests and unseen hosts fail closed") is
    unqualified by method: a request to a host outside ``allowed_hosts`` is
    aborted whether it mutates or not (e.g. a load-time image or fetch to a
    host the twin never saw), never merely allowed through because it happens
    to be a GET.
    """

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        expected_method: str | None,
        expected_url_normalized: str | None,
        expected_digest: str | None,
        allowed_read_urls: frozenset[str],
    ) -> None:
        self._allowed_hosts = allowed_hosts
        self._expected_method = expected_method
        self._expected_url_normalized = expected_url_normalized
        self._expected_digest = expected_digest
        self._allowed_read_urls = allowed_read_urls
        self.armed = False
        self.forwarded = False
        self.abort_reason: str | None = None

    def handle(self, route: Route) -> None:
        request = route.request
        hostname = urlsplit(request.url).hostname
        host_allowed = hostname is not None and normalize_hostname(hostname) in self._allowed_hosts
        if request.method not in _MUTATING_METHODS:
            # Invariant 5 is unqualified: unseen hosts fail closed regardless
            # of method, not only for mutating requests.
            if not host_allowed:
                self._deny(route, "undeclared_host")
                return
            if _normalize_request_url(request.url) not in self._allowed_read_urls:
                self._deny(route, "unmatched_request")
                return
            route.continue_()
            return
        if not host_allowed:
            self._deny(route, "unseen_host")
            return
        if self.forwarded:
            self._deny(route, "duplicate_mutation")
            return
        if not self.armed:
            self._deny(route, "unexpected_mutation")
            return
        observed_body = _parse_multipart(
            request.headers.get("content-type", ""), request.post_data_buffer or b""
        )
        if (
            request.method != self._expected_method
            or _normalize_request_url(request.url) != self._expected_url_normalized
            or digest(observed_body) != self._expected_digest
        ):
            self._deny(route, "commit_mismatch")
            return
        self.forwarded = True
        route.continue_()

    def _deny(self, route: Route, reason: str) -> None:
        if self.abort_reason is None:
            self.abort_reason = reason
        route.abort()

    def check(self) -> None:
        """Raise if the guard has aborted any mutating request so far."""
        if self.abort_reason is not None:
            raise ReplayAbortError(self.abort_reason)


class ProductionReplayer:
    """Replays exactly one run's stored, ordered action plan against production."""

    def __init__(
        self,
        repository: Repository,
        run_id: str,
        *,
        file_resolver: FileResolver,
        headless: bool = True,
        authenticate: Callable[[Page], None] | None = None,
    ) -> None:
        self._repository = repository
        self._run_id = run_id
        self._file_resolver = file_resolver
        self._headless = headless
        self._authenticate = authenticate

    def replay(self) -> ReplayOutcome:
        run = self._repository.get_run(self._run_id)
        if run.state is not RunState.EXECUTING:
            raise ConflictError(f"Run {self._run_id} is not executing: {run.state.value}")

        actions = self._repository.get_run_actions(self._run_id)
        recomputed_hash = digest([action.model_dump(mode="json") for action in actions])
        if run.action_plan_hash is None or recomputed_hash != run.action_plan_hash:
            return self._abort("action_plan_hash_mismatch")

        bucket = self._repository.get_bucket_by_id(run.bucket_id)
        reviewed = self._repository.get_reviewed_playbook(bucket.id, run.playbook_version)
        success_criteria = Playbook.model_validate(reviewed.content).success_criteria
        allowed_hosts = frozenset(run.allowed_hosts)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self._headless)
            try:
                page = browser.new_page()
                page.set_default_timeout(_LOCATOR_TIMEOUT_MS)
                # Authentication is a caller-owned precondition (like the
                # recorded session the twin rehearsed under), performed before
                # the plan replay --- and before its mutation guard --- begins.
                if self._authenticate is not None:
                    self._authenticate(page)
                try:
                    return self._consume_plan(
                        page,
                        actions,
                        target_base_url=run.target_base_url,
                        allowed_hosts=allowed_hosts,
                        success_criteria=success_criteria,
                    )
                except ReplayAbortError as error:
                    return self._abort(error.reason)
            finally:
                browser.close()

    def _abort(self, reason: str) -> ReplayOutcome:
        return ReplayOutcome(
            succeeded=False,
            mutation_forwarded=False,
            failure_cause=FailureCause.PLAN_MISMATCH,
            abort_reason=reason,
            final_status=None,
            final_body=None,
        )

    def _consume_plan(
        self,
        page: Page,
        actions: tuple[RecordedAction, ...],
        *,
        target_base_url: str,
        allowed_hosts: frozenset[str],
        success_criteria: Any,
    ) -> ReplayOutcome:
        # A replayable plan ends with its single commit; anything else never
        # forwards a mutation.
        if not actions or actions[-1].tool_name != "commit":
            raise ReplayAbortError("plan_has_no_commit")
        commit_action = actions[-1]
        allowed_read_urls = frozenset(
            _normalize_request_url(
                urljoin(
                    f"{target_base_url.rstrip('/')}/",
                    str(action.arguments["path"]).lstrip("/"),
                )
            )
            for action in actions
            if action.tool_name == "navigate"
        )
        expected_url = commit_action.arguments.get("url")
        guard = _MutationGuard(
            allowed_hosts=allowed_hosts,
            expected_method=commit_action.arguments.get("method"),
            expected_url_normalized=(
                _normalize_request_url(str(expected_url)) if expected_url else None
            ),
            expected_digest=commit_action.arguments.get("payload_digest"),
            allowed_read_urls=allowed_read_urls,
        )
        page.route("**/*", guard.handle)
        try:
            outcome: ReplayOutcome | None = None
            for action in actions:
                self._repository.set_run_action_status(self._run_id, action.ordinal, "executing")
                try:
                    if action.tool_name == "commit":
                        outcome = self._commit(page, action, guard, success_criteria)
                    else:
                        self._perform(page, action, target_base_url, allowed_hosts)
                        guard.check()
                except ReplayAbortError:
                    self._repository.set_run_action_status(self._run_id, action.ordinal, "failed")
                    raise
                except PlaywrightError as error:
                    # A locator that no longer resolves (renamed label, changed
                    # role, missing control) is live-target drift: abort, never
                    # let a raw Playwright error escape the replay boundary.
                    self._repository.set_run_action_status(self._run_id, action.ordinal, "failed")
                    raise ReplayAbortError("anchor_not_found") from error
                self._repository.set_run_action_status(self._run_id, action.ordinal, "succeeded")
            assert outcome is not None  # the loop always ends on the commit action
            return outcome
        finally:
            page.unroute("**/*", guard.handle)

    def _perform(
        self,
        page: Page,
        action: RecordedAction,
        target_base_url: str,
        allowed_hosts: frozenset[str],
    ) -> None:
        if action.tool_name == "navigate":
            self._navigate(page, action, target_base_url, allowed_hosts)
        elif action.tool_name == "fill":
            self._resolve(page, action.arguments["anchor"]).fill(str(action.arguments["value"]))
        elif action.tool_name == "upload":
            self._upload(page, action)
        elif action.tool_name == "click":
            self._resolve(page, action.arguments["anchor"]).click()
        else:  # pragma: no cover - the schema constrains tool_name
            raise ReplayAbortError("unknown_action")

    def _navigate(
        self,
        page: Page,
        action: RecordedAction,
        target_base_url: str,
        allowed_hosts: frozenset[str],
    ) -> None:
        path = str(action.arguments["path"])
        url = urljoin(f"{target_base_url.rstrip('/')}/", path.lstrip("/"))
        hostname = urlsplit(url).hostname
        if hostname is None or normalize_hostname(hostname) not in allowed_hosts:
            raise ReplayAbortError("unseen_host")
        page.goto(url)
        anchor = action.arguments["anchor"]
        name = anchor.get("name")
        if name:
            try:
                page.get_by_role("heading", name=name).wait_for(timeout=_COMMIT_RESPONSE_TIMEOUT_MS)
            except PlaywrightTimeoutError as error:
                raise ReplayAbortError("anchor_not_found") from error

    def _upload(self, page: Page, action: RecordedAction) -> None:
        value = action.arguments["value"]
        filename = str(value["filename"])
        sha256 = str(value["sha256"])
        content_type = str(value.get("content_type", "application/octet-stream"))
        data = self._file_resolver(filename, sha256)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ReplayAbortError("file_hash_mismatch")
        locator = self._resolve(page, action.arguments["anchor"])
        locator.set_input_files({"name": filename, "mimeType": content_type, "buffer": data})

    def _resolve(self, page: Page, anchor: Mapping[str, Any]) -> Locator:
        css = str(anchor["css"])
        role = str(anchor["role"])
        name = anchor.get("name")
        locator = page.locator(css)
        if name:
            locator = locator.and_(page.get_by_role(role, name=name))
        return locator

    def _commit(
        self,
        page: Page,
        action: RecordedAction,
        guard: _MutationGuard,
        success_criteria: Any,
    ) -> ReplayOutcome:
        locator = self._resolve(page, action.arguments["anchor"])
        final_status: int | None = None
        final_body: str | None = None
        guard.armed = True
        try:
            try:
                with page.expect_response(
                    lambda response: response.request.method in _MUTATING_METHODS,
                    timeout=_COMMIT_RESPONSE_TIMEOUT_MS,
                ) as response_info:
                    locator.click()
                response = response_info.value
                final_status = response.status
                final_body = response.text()
            except PlaywrightTimeoutError:
                pass
        finally:
            guard.armed = False

        guard.check()
        if not guard.forwarded:
            raise ReplayAbortError("mutation_not_forwarded")
        succeeded = (
            final_status == success_criteria.status_code
            and success_criteria.page_contains in (final_body or "")
        )
        return ReplayOutcome(
            succeeded=succeeded,
            mutation_forwarded=True,
            failure_cause=None if succeeded else FailureCause.EXECUTION_ERROR,
            abort_reason=None,
            final_status=final_status,
            final_body=final_body,
        )


def execute_authorized_run(
    run_service: RunService,
    run_id: str,
    *,
    file_resolver: FileResolver,
    headless: bool = True,
    authenticate: Callable[[Page], None] | None = None,
) -> tuple[Any, ReplayOutcome]:
    """Transition an authorized run into execution and replay its stored plan exactly once.

    This is the ``activate_rehearsed_plan`` tool's actual body once the SDK
    resumes past approval: the model never drives any of this, it only ever
    learns the resulting status string.
    """
    run_service.start_execution(run_id)
    replayer = ProductionReplayer(
        run_service.repository,
        run_id,
        file_resolver=file_resolver,
        headless=headless,
        authenticate=authenticate,
    )
    try:
        outcome = replayer.replay()
    except Exception as error:
        outcome = ReplayOutcome(
            succeeded=False,
            mutation_forwarded=False,
            failure_cause=FailureCause.EXECUTION_ERROR,
            abort_reason=f"unexpected_{type(error).__name__}",
            final_status=None,
            final_body=None,
        )
    detail: dict[str, Any] = {}
    if outcome.abort_reason is not None:
        detail["abort_reason"] = outcome.abort_reason
    updated_run, _event = run_service.record_verified_outcome(
        run_id,
        succeeded=outcome.succeeded,
        failure_cause=outcome.failure_cause,
        detail=detail,
    )
    return updated_run, outcome
