"""The twin's function tools: navigate, read_page, fill, upload, click, commit.

The model never receives a production browser. Each tool acts on
``TwinEnvironment``, which resolves anchors against the *current* page ---
parsed structurally from whatever the twin's response router served, fixture
or fresh snapshot --- rather than replaying recorded coordinates blindly.
Every tool but ``read_page`` enforces the playbook's next effect class and
appends exactly one canonical ``RecordedAction``; only ``commit`` may ever
trigger the twin's captured mutation.

The page parser below is intentionally narrow: it understands the closed set
of markup shapes this harness's target renders (``#id`` label associations,
the page heading, and the submit button), not arbitrary HTML/CSS. That is a
proportionate choice for a conservative twin bound to one recorded target,
not a general-purpose DOM engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from apprentice.canonical import digest
from apprentice.induction.induce import Anchor, Playbook, PlaybookStep
from apprentice.models import RecordedAction
from apprentice.twin.builder import Twin
from apprentice.twin.responses import TwinAbortError

__all__ = [
    "FileValue",
    "PageState",
    "TwinEnvironment",
]

_CONTROL_TAGS = frozenset({"input", "textarea", "select", "button", "a"})
_TEXT_CONTROL_TAGS = frozenset({"button", "a"})


def _role_of(tag: str, input_type: str) -> str:
    if tag == "button" or (tag == "input" and input_type in {"button", "file", "submit"}):
        return "button"
    if tag == "a":
        return "link"
    if tag == "select":
        return "combobox"
    if tag == "input" and input_type == "number":
        return "spinbutton"
    return "textbox"


def _css_of(control_id: str | None, tag: str, field_name: str | None) -> str:
    if control_id:
        return f"#{control_id}"
    if field_name:
        return f'{tag}[name="{field_name}"]'
    return tag


@dataclass(frozen=True)
class _Control:
    role: str
    name: str
    css: str
    field_name: str | None


@dataclass(frozen=True)
class PageState:
    """A structural snapshot of the current page: the twin's read-only view."""

    title: str
    text: str
    form_action: str | None
    controls: tuple[_Control, ...]

    def resolve(self, anchor: Anchor) -> _Control:
        """Resolve a playbook anchor against this page, or fail closed.

        Resolution requires both the anchor's ``css`` identity *and* its
        declared accessible ``name`` to still match: an unchanged id whose
        label text has drifted is exactly the drift a conservative twin
        must refuse to paper over.
        """

        if anchor.css == "main":
            control = _Control(role="document", name=self.title, css="main", field_name=None)
        else:
            control = next((item for item in self.controls if item.css == anchor.css), None)
        if control is None:
            raise TwinAbortError("anchor_not_found", method="ANCHOR", url=anchor.css)
        if anchor.role != control.role:
            raise TwinAbortError("anchor_role_mismatch", method="ANCHOR", url=anchor.css)
        if (anchor.name or "") != control.name:
            raise TwinAbortError("anchor_name_mismatch", method="ANCHOR", url=anchor.css)
        return control


class _PageParser(HTMLParser):
    """Extract label/control/heading structure from the target's simple markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.form_action: str | None = None
        self._main_depth = 0
        self._text_parts: list[str] = []
        self._label_text: dict[str, list[str]] = {}
        self._controls: list[dict[str, Any]] = []
        self._in_title = False
        self._current_label_for: str | None = None
        self._current_control: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "main":
            self._main_depth += 1
        elif tag == "form" and self.form_action is None:
            self.form_action = attributes.get("action")
        elif tag == "label":
            target = attributes.get("for")
            self._current_label_for = target
            if target is not None:
                self._label_text.setdefault(target, [])
        elif tag in _CONTROL_TAGS:
            control = {
                "tag": tag,
                "id": attributes.get("id") or None,
                "field_name": attributes.get("name") or None,
                "input_type": attributes.get("type", "").casefold(),
                "aria_label": attributes.get("aria-label"),
                "text": [],
            }
            self._controls.append(control)
            if tag in _TEXT_CONTROL_TAGS:
                self._current_control = control

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "main" and self._main_depth > 0:
            self._main_depth -= 1
        elif tag == "label":
            self._current_label_for = None
        elif tag in _TEXT_CONTROL_TAGS:
            self._current_control = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._main_depth > 0:
            self._text_parts.append(data)
        if self._current_label_for is not None:
            self._label_text[self._current_label_for].append(data)
        if self._current_control is not None:
            self._current_control["text"].append(data)

    def page_state(self) -> PageState:
        controls: list[_Control] = []
        for control in self._controls:
            control_id = control["id"]
            field_name = control["field_name"]
            role = _role_of(control["tag"], control["input_type"])
            label = "".join(self._label_text.get(control_id, [])).strip() if control_id else ""
            name = (
                control["aria_label"]
                or label
                or "".join(control["text"]).strip()
                or field_name
                or ""
            )
            controls.append(
                _Control(
                    role=role,
                    name=name,
                    css=_css_of(control_id, control["tag"], field_name),
                    field_name=field_name,
                )
            )
        return PageState(
            title=self.title.strip(),
            text="".join(self._text_parts).strip(),
            form_action=self.form_action,
            controls=tuple(controls),
        )


def parse_page(html: str) -> PageState:
    """Parse the twin's routed page body into a structural page state."""

    parser = _PageParser()
    parser.feed(html)
    return parser.page_state()


@dataclass(frozen=True)
class FileValue:
    """A declared file value: what invariants can check without raw bytes."""

    filename: str
    sha256: str
    content_type: str = "application/octet-stream"


class TwinEnvironment:
    """Per-run mutable state the six function tools act on.

    Enforces the playbook's next effect class on every recorded tool call,
    resolves anchors against the current routed page (not macro replay), and
    is the only place a commit's captured mutation is ever constructed.
    """

    def __init__(self, *, playbook: Playbook, twin: Twin, target_base_url: str) -> None:
        self._steps: tuple[PlaybookStep, ...] = tuple(playbook.steps)
        self._twin = twin
        self._target_base_url = target_base_url.rstrip("/")
        self._next_index = 0
        self._current_page: PageState | None = None
        self._current_url: str | None = None
        self._form_values: dict[str, Any] = {}
        self.actions: list[RecordedAction] = []
        self.final_response: Any | None = None
        self.aborted_reason: str | None = None

    @property
    def visited_hosts(self) -> frozenset[str]:
        return frozenset(self._twin.router.visited_hosts)

    def _next_step(self, tool_name: str) -> PlaybookStep:
        if self._next_index >= len(self._steps):
            raise TwinAbortError("plan_exhausted", method="TOOL", url=tool_name)
        step = self._steps[self._next_index]
        if step.action != tool_name:
            raise TwinAbortError("wrong_effect", method="TOOL", url=tool_name)
        return step

    def _record(self, step: PlaybookStep, arguments: dict[str, Any]) -> None:
        self.actions.append(
            RecordedAction(
                ordinal=len(self.actions),
                tool_name=step.action,
                arguments=arguments,
                arguments_digest=digest(arguments),
                effect=step.effect,
            )
        )
        self._next_index += 1

    def navigate(self, path: str) -> str:
        step = self._next_step("navigate")
        url = urljoin(f"{self._target_base_url}/", path.lstrip("/"))
        response = self._twin.router.get(url)
        page = parse_page(str(response.body))
        page.resolve(step.anchor)
        self._current_page = page
        self._current_url = url
        # Production replay re-resolves this same anchor after navigating
        # (see ``ProductionReplayer._navigate``), so it must be part of the
        # stored plan, exactly like every other step's anchor.
        self._record(step, {"path": path, "anchor": step.anchor.model_dump(mode="json")})
        return page.text

    def read_page(self) -> dict[str, Any]:
        if self._current_page is None:
            raise TwinAbortError("no_current_page", method="TOOL", url="read_page")
        page = self._current_page
        return {
            "title": page.title,
            "text": page.text,
            "controls": [
                {"role": c.role, "name": c.name, "css": c.css} for c in page.controls
            ],
        }

    def _current_page_or_abort(self) -> PageState:
        if self._current_page is None:
            raise TwinAbortError("no_current_page", method="TOOL", url="fill")
        return self._current_page

    def fill(self, anchor: Anchor, value: str) -> str:
        step = self._next_step("fill")
        control = self._current_page_or_abort().resolve(anchor)
        if control.field_name is not None:
            self._form_values[control.field_name] = value
        self._record(step, {"anchor": anchor.model_dump(mode="json"), "value": value})
        return "filled"

    def upload(self, anchor: Anchor, file: FileValue) -> str:
        step = self._next_step("upload")
        control = self._current_page_or_abort().resolve(anchor)
        if control.field_name is not None:
            self._form_values[control.field_name] = file
        self._record(
            step,
            {
                "anchor": anchor.model_dump(mode="json"),
                "value": {
                    "filename": file.filename,
                    "sha256": file.sha256,
                    "content_type": file.content_type,
                },
            },
        )
        return "uploaded"

    def click(self, anchor: Anchor) -> str:
        step = self._next_step("click")
        self._current_page_or_abort().resolve(anchor)
        self._record(step, {"anchor": anchor.model_dump(mode="json")})
        return "clicked"

    def commit(self, anchor: Anchor) -> str:
        step = self._next_step("commit")
        page = self._current_page_or_abort()
        page.resolve(anchor)
        fields: dict[str, str] = {}
        files: list[dict[str, str]] = []
        for field_name, value in self._form_values.items():
            if isinstance(value, FileValue):
                files.append(
                    {
                        "field": field_name,
                        "filename": value.filename,
                        "content_type": value.content_type,
                        "sha256": value.sha256,
                    }
                )
            else:
                fields[field_name] = value
        body = {"kind": "multipart", "fields": fields, "files": files}
        action_url = urljoin(f"{self._target_base_url}/", (page.form_action or "").lstrip("/"))
        response = self._twin.router.capture_mutation(
            method="POST",
            url=action_url,
            headers={"content-type": "multipart/form-data"},
            body=body,
        )
        self.final_response = response
        self._record(
            step,
            {
                "anchor": anchor.model_dump(mode="json"),
                # Recorded alongside the payload digest so a production
                # replay can compare the live mutating request's method and
                # normalized URL against exactly what was rehearsed, not just
                # its body.
                "method": "POST",
                "url": action_url,
                "payload_digest": digest(body),
            },
        )
        return "committed"
