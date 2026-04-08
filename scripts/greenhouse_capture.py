"""Shared Greenhouse screenshot capture helpers."""

from __future__ import annotations


def _capture_overflow(candidate: dict) -> float:
    return max(
        float(candidate.get("scroll_height") or 0) - float(candidate.get("client_height") or 0),
        0.0,
    )


def _choose_capture_root(capture_metadata: dict | None) -> dict:
    """Prefer a large scrollable form ancestor over the document root."""
    candidates = list((capture_metadata or {}).get("candidates") or [])
    if not candidates:
        return {"key": "__document__", "kind": "document"}

    viewport_width = max(float((capture_metadata or {}).get("viewport_width") or 0), 1.0)
    viewport_height = max(float((capture_metadata or {}).get("viewport_height") or 0), 1.0)
    document_candidate = next(
        (candidate for candidate in candidates if candidate.get("kind") == "document"),
        candidates[0],
    )

    def is_large_enough(candidate: dict) -> bool:
        return (
            float(candidate.get("width") or 0) >= viewport_width * 0.45
            and float(candidate.get("height") or 0) >= viewport_height * 0.30
        )

    def score(candidate: dict) -> tuple[float, float, float]:
        width = float(candidate.get("width") or 0)
        height = float(candidate.get("height") or 0)
        return (width * height, _capture_overflow(candidate), height)

    preferred = [
        candidate
        for candidate in candidates
        if candidate.get("kind") != "document"
        and candidate.get("contains_form")
        and is_large_enough(candidate)
        and _capture_overflow(candidate) >= max(40.0, float(candidate.get("client_height") or 0) * 0.12)
    ]
    if preferred:
        return max(preferred, key=score)

    if is_large_enough(document_candidate) and _capture_overflow(document_candidate) > 0:
        return document_candidate

    fallback = [
        candidate
        for candidate in candidates
        if candidate.get("kind") != "document" and is_large_enough(candidate) and _capture_overflow(candidate) >= 40.0
    ]
    if fallback:
        return max(fallback, key=score)

    return document_candidate


def _capture_root_selector(root_key: str | None) -> str | None:
    if not root_key or root_key == "__document__":
        return None
    return f'[data-job-assets-capture-root-key="{root_key}"]'


def _capture_scroll_metrics(page, *, root_key: str) -> dict:
    return page.evaluate(
        """payload => {
            const { rootKey } = payload;
            const docScroller = document.scrollingElement || document.documentElement || document.body;
            const root = rootKey === "__document__"
              ? docScroller
              : document.querySelector(`[data-job-assets-capture-root-key="${rootKey}"]`) || docScroller;
            const rect = root === docScroller ? null : root.getBoundingClientRect();
            const doc = document.documentElement;
            const body = document.body;
            const scrollHeight = root === docScroller
              ? Math.max(
                  doc ? doc.scrollHeight : 0,
                  doc ? doc.offsetHeight : 0,
                  body ? body.scrollHeight : 0,
                  body ? body.offsetHeight : 0
                )
              : Math.max(root.scrollHeight || 0, root.offsetHeight || 0, root.clientHeight || 0);
            const viewportHeight = root === docScroller
              ? (window.innerHeight || 0)
              : Math.max(
                  Math.min(
                    Math.max(rect?.height || 0, root.clientHeight || 0),
                    window.innerHeight || 0
                  ),
                  1
                );
            return {
              scrollHeight,
              viewportHeight,
              devicePixelRatio: window.devicePixelRatio || 1,
            };
        }""",
        {"rootKey": root_key},
    )


def _set_capture_scroll_position(page, *, root_key: str, target_css: float) -> float:
    return float(
        page.evaluate(
            """payload => {
                const { rootKey, target } = payload;
                const docScroller = document.scrollingElement || document.documentElement || document.body;
                const root = rootKey === "__document__"
                  ? docScroller
                  : document.querySelector(`[data-job-assets-capture-root-key="${rootKey}"]`) || docScroller;
                if (root === docScroller) {
                  window.scrollTo(0, target);
                  return window.scrollY || 0;
                }
                const rect = root.getBoundingClientRect();
                const absoluteTop = (window.scrollY || 0) + rect.top;
                window.scrollTo(0, Math.max(0, absoluteTop));
                root.scrollTop = target;
                return root.scrollTop || 0;
            }""",
            {"rootKey": root_key, "target": target_css},
        )
    )
