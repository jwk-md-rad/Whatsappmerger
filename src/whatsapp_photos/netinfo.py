"""Best-effort discovery of useful URLs to print at server startup.

We don't enumerate every network interface (that's painful in pure
stdlib across OSes). Instead we surface the two things people actually
need:

* the laptop's primary outbound IPv4 (what most LAN clients will use), and
* if the ``tailscale`` CLI is on PATH, the device's Tailscale IP and
  MagicDNS hostname.

Each helper degrades silently — failure to resolve any of these just
means we print less.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess


def primary_lan_ip() -> str | None:
    """Return the IP address used to reach the wider network, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Picking any non-local target tells the kernel which interface
        # it would route out of; we don't actually send traffic.
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def tailscale_endpoints() -> list[tuple[str, str]]:
    """Return ``[(label, host), ...]`` derived from the Tailscale CLI.

    Empty list if the CLI is missing or the daemon isn't logged in.
    """
    ts = shutil.which("tailscale")
    if not ts:
        return []

    out: list[tuple[str, str]] = []
    try:
        ip4 = subprocess.run(
            [ts, "ip", "--4"], capture_output=True, text=True, timeout=2
        )
        for line in ip4.stdout.splitlines():
            line = line.strip()
            if line:
                out.append(("Tailscale IPv4", line))
    except (subprocess.SubprocessError, OSError):
        pass

    try:
        status = subprocess.run(
            [ts, "status", "--json"], capture_output=True, text=True, timeout=2
        )
        if status.returncode == 0:
            data = json.loads(status.stdout)
            self_node = data.get("Self") or {}
            dns_name = (self_node.get("DNSName") or "").rstrip(".")
            if dns_name:
                out.append(("Tailscale MagicDNS", dns_name))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        pass

    return out


def url_for(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}/"
    return f"http://{host}:{port}/"
