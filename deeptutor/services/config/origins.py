from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

_ORIGIN_SEPARATORS = re.compile(r"[,;\n]+")
_UNSAFE_CREDENTIALED_ORIGINS = frozenset({"*", "null"})


def _raw_origin_items(value: Any) -> Iterable[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_raw_origin_items(item))
        return items
    return _ORIGIN_SEPARATORS.split(str(value))


def normalize_origin(value: Any) -> str:
    """Normalize a browser Origin value for CORS allowlists.

    Operators often paste values as ``host:port`` or separate multiple origins
    with semicolons. Browsers always send an Origin as ``scheme://host[:port]``.
    This helper makes common deployment input tolerant while keeping the output
    as exact origins for Starlette's CORSMiddleware.
    """

    origin = str(value or "").strip().rstrip("/")
    if not origin:
        return ""
    if origin in {"*", "null"}:
        return origin
    if "://" not in origin:
        origin = f"http://{origin}"

    try:
        parsed = urlparse(origin)
    except ValueError:
        return origin
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return origin


def normalize_origins(value: Any) -> list[str]:
    origins: list[str] = []
    seen: set[str] = set()
    for raw in _raw_origin_items(value):
        origin = normalize_origin(raw)
        if origin and origin not in seen:
            origins.append(origin)
            seen.add(origin)
    return origins


def find_unsafe_credentialed_origins(value: Any) -> list[str]:
    """Return configured origins that cannot be used with browser credentials."""
    return sorted(_UNSAFE_CREDENTIALED_ORIGINS & set(normalize_origins(value)))


def browser_allowed_origins(system_settings: Mapping[str, Any]) -> list[str]:
    """Return concrete browser origins shared by CORS and cookie-write guards.

    Wildcard and opaque origins are never concrete credentialed origins.  The
    API startup path additionally rejects them when authentication is enabled,
    but filtering here keeps every other caller fail-closed as well.
    """
    frontend_port = str(system_settings.get("frontend_port") or 3782)
    origins = [
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    for origin in normalize_origins(
        [system_settings.get("cors_origin"), system_settings.get("cors_origins")]
    ):
        if origin not in _UNSAFE_CREDENTIALED_ORIGINS and origin not in origins:
            origins.append(origin)
    return origins
