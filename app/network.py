"""Client network allowlist for the /v1 API surface.

The container publishes 127.0.0.1:4000 only and joins private Docker bridges,
so every legitimate caller arrives from loopback or a private range. This
guard keeps that true if a future deploy widens the bind: requests to /v1/*
from any other source are refused before authentication runs.

X-Forwarded-For is deliberately ignored: no reverse proxy sits in front of
this service, so the header would be caller-controlled.

LLM_PROXY_ALLOWED_NETWORKS (optional): comma-separated CIDRs that replace the
default list. An invalid value fails startup instead of silently opening up.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Sequence

ALLOWED_NETWORKS_ENV = "LLM_PROXY_ALLOWED_NETWORKS"

DEFAULT_ALLOWED_NETWORKS: tuple[str, ...] = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # Tailscale CGNAT mesh
)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_allowed_networks(raw: str | None) -> tuple[Network, ...]:
    """Parse the env value; None/blank means the default list.

    Raises ValueError on any malformed entry, and on an explicitly empty list,
    so a typo can never turn into "allow everything" or "allow nothing".
    """
    if raw is None or not raw.strip():
        entries: Sequence[str] = DEFAULT_ALLOWED_NETWORKS
    else:
        entries = [item.strip() for item in raw.split(",") if item.strip()]
        if not entries:
            raise ValueError(f"{ALLOWED_NETWORKS_ENV} lists no networks")
    networks: list[Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=True))
        except ValueError as exc:
            raise ValueError(f"{ALLOWED_NETWORKS_ENV} has an invalid CIDR entry") from exc
    return tuple(networks)


def allowed_networks_from_env() -> tuple[Network, ...]:
    return parse_allowed_networks(os.environ.get(ALLOWED_NETWORKS_ENV))


def client_allowed(host: str | None, networks: Sequence[Network]) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)
