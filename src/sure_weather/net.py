from __future__ import annotations

import threading

import httpx

# Shared HTTP client with connection pooling: upstream calls (Open-Meteo,
# METAR, RainViewer, Nominatim) reuse warm TLS connections instead of paying
# a fresh handshake on every request. httpx clients are thread-safe.
_client: httpx.Client | None = None
_lock = threading.Lock()


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=httpx.Timeout(60.0, connect=10.0),
                    limits=httpx.Limits(
                        max_connections=20, max_keepalive_connections=10
                    ),
                    headers={"User-Agent": "sure-weather/0.1"},
                    follow_redirects=True,
                )
    return _client
