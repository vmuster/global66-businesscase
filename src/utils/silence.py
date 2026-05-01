"""Silence noisy import-time banners from third-party deps.

`google-generativeai` (the deprecated SDK) prints a visible deprecation banner
at import time when present in the environment. We don't depend on it directly
(we use `google-genai`, the modern SDK), but `instructor` may transitively
import it on some installs. The banner is a `print()` to stdout, so a regular
`warnings.filterwarnings(...)` is not enough: we have to redirect stdout
during the noisy import.

Set `VOC_VERBOSE_THIRDPARTY=1` in the environment to keep the banners visible
(useful when debugging dependency issues).
"""

from __future__ import annotations

import contextlib
import io
import os
import warnings


def _should_silence() -> bool:
    return os.getenv("VOC_VERBOSE_THIRDPARTY", "0") != "1"


def silence_thirdparty_imports() -> None:
    """Pre-warm noisy third-party imports under a stdout sink.

    Idempotent and safe to call multiple times (Python caches imports).
    Call this at the top of every entry-point BEFORE any project import that
    transitively pulls `instructor`, `google.generativeai` or `google.genai`.
    """
    if not _should_silence():
        return

    warnings.filterwarnings(
        "ignore",
        message=r".*google\.generativeai.*",
    )
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        module=r"google\.generativeai.*",
    )

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            import google.generativeai  # noqa: F401
        except Exception:
            pass
        try:
            import google.genai  # noqa: F401
        except Exception:
            pass
