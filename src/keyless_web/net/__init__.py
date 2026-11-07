"""Network layer: backend selection, rate limiting and error translation."""

from __future__ import annotations

from .transport import (
    AllBackendsFailed,
    Backend,
    HostRateLimiter,
    backends,
    build_client,
    configured_proxy,
    default_limiter,
    inject_system_trust,
    is_retryable,
    request_with_fallback,
    reset_limiter,
    run_with_fallback,
    to_keyless_error,
)

__all__ = [
    "AllBackendsFailed",
    "Backend",
    "HostRateLimiter",
    "backends",
    "build_client",
    "configured_proxy",
    "default_limiter",
    "inject_system_trust",
    "is_retryable",
    "request_with_fallback",
    "reset_limiter",
    "run_with_fallback",
    "to_keyless_error",
]
