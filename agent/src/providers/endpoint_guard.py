"""Keep a user-supplied model endpoint on the public internet at connect time.

A custom OpenAI-compatible provider is checked when it is tested and saved,
but a host name can re-resolve later (DNS rebinding) and point the saved
profile at a private address. The clients built here resolve every direct
connection once, refuse it unless every address is public, and connect to the
address they checked, so there is no second lookup to rebind. TLS still
verifies the certificate against the original host name.

When a proxy applies to the endpoint, the proxy resolves the name and no
local check can see the address it reaches; those clients are not built and
the caller keeps its default transport.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx


# Fake-IP proxies (Clash, Surge, sing-box in TUN mode) answer every lookup
# from the RFC 2544 benchmarking range and resolve the real name themselves,
# the way an HTTP proxy does; refusing it refused every endpoint on such hosts.
_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")


def is_non_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, resolved: bool = False
) -> bool:
    """Return whether an endpoint address is outside the public internet.

    ``is_private`` misses the shared address space 100.64.0.0/10, which holds
    Alibaba Cloud's metadata service (100.100.100.200); ``is_global`` covers it
    together with loopback, link-local, reserved and documentation ranges. An
    IPv4-mapped IPv6 address is judged by the IPv4 address it carries.

    Args:
        address: The literal or resolved endpoint address.
        resolved: True when the address came from a DNS lookup. A looked-up
            address in 198.18.0.0/15 is a local fake-IP proxy's answer and is
            allowed; the same address written into a URL is not.

    Returns:
        True when the address must not be used as a provider endpoint.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if resolved and address in _FAKE_IP_RANGE:
        return False
    return not address.is_global or address.is_multicast


def proxy_applies(url: str) -> bool:
    """Return whether the process proxy settings route ``url`` through a proxy.

    Args:
        url: The endpoint URL.

    Returns:
        True when a proxy from the environment would carry the request.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    proxies = urllib.request.getproxies()
    if not (proxies.get(parts.scheme) or proxies.get("all")):
        return False
    return not urllib.request.proxy_bypass(host)


def _checked_address(host: str, port: int) -> str:
    """Resolve ``host`` and return one address, refusing any non-public one.

    Raises:
        httpcore.ConnectError: If the name does not resolve or any of its
            addresses is not public.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise httpcore.ConnectError(f"Provider host {host!r} could not be resolved") from exc
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(is_non_public_address(address, resolved=True) for address in addresses):
        raise httpcore.ConnectError(
            f"Provider host {host!r} resolves to a local or private network address"
        )
    return str(addresses[0])


class _PublicOnlyBackend(httpcore.NetworkBackend):
    """Sync network backend that connects only to checked public addresses."""

    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None, socket_options: Any = None) -> httpcore.NetworkStream:
        return self._inner.connect_tcp(
            _checked_address(host, port), port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    def connect_unix_socket(self, path: str, timeout: float | None = None,
                            socket_options: Any = None) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not provider endpoints")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class _AsyncPublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Async network backend that connects only to checked public addresses."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        address = await anyio.to_thread.run_sync(_checked_address, host, port)
        return await self._inner.connect_tcp(
            address, port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not provider endpoints")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _guard_pool(transport: httpx.HTTPTransport | httpx.AsyncHTTPTransport) -> None:
    """Wrap the transport's connection pool backend in the public-only check.

    Raises:
        RuntimeError: If the installed httpx no longer exposes the pool's
            network backend, so the guard cannot be installed.
    """
    pool = getattr(transport, "_pool", None)
    inner = getattr(pool, "_network_backend", None)
    if inner is None:
        raise RuntimeError("httpx transport layout changed; cannot install the endpoint guard")
    wrapper = _AsyncPublicOnlyBackend if isinstance(transport, httpx.AsyncHTTPTransport) else _PublicOnlyBackend
    pool._network_backend = wrapper(inner)


def public_only_async_client(**client_kwargs: Any) -> httpx.AsyncClient:
    """Build an async client whose every direct connection is checked.

    The transport is explicit, so environment proxies are not mounted; call
    this only when :func:`proxy_applies` is false for the endpoint.

    Args:
        **client_kwargs: Extra keyword arguments for the client.

    Returns:
        The guarded async client.
    """
    transport = httpx.AsyncHTTPTransport(proxy=None, trust_env=True)
    _guard_pool(transport)
    return httpx.AsyncClient(transport=transport, **client_kwargs)


def build_public_only_clients(**client_kwargs: Any) -> tuple[httpx.Client, httpx.AsyncClient]:
    """Build sync and async clients whose every direct connection is checked.

    The transports are explicit, so environment proxies are not mounted; call
    this only when :func:`proxy_applies` is false for the endpoint.

    Args:
        **client_kwargs: Extra keyword arguments for both clients (timeouts,
            redirect policy).

    Returns:
        A ``(sync_client, async_client)`` pair.
    """
    sync_transport = httpx.HTTPTransport(proxy=None, trust_env=True)
    _guard_pool(sync_transport)
    return (
        httpx.Client(transport=sync_transport, **client_kwargs),
        public_only_async_client(**client_kwargs),
    )
