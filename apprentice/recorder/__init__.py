"""Post-login browser demonstration recording."""

from apprentice.recorder.artifacts import (
    ArtifactIntegrityError,
    ArtifactSafetyError,
    assert_no_sentinels,
    load_artifact,
)
from apprentice.recorder.session import BrowserRecorderAttachment, RecorderSession, SemanticAnchor

__all__ = [
    "ArtifactIntegrityError",
    "ArtifactSafetyError",
    "BrowserRecorderAttachment",
    "RecorderSession",
    "SemanticAnchor",
    "assert_no_sentinels",
    "load_artifact",
]
