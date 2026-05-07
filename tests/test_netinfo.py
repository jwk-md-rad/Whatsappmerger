from __future__ import annotations

from whatsapp_photos import netinfo


def test_url_for_ipv4() -> None:
    assert netinfo.url_for("192.168.1.10", 8765) == "http://192.168.1.10:8765/"


def test_url_for_hostname() -> None:
    assert (
        netinfo.url_for("mylaptop.tail-scale.ts.net", 8765)
        == "http://mylaptop.tail-scale.ts.net:8765/"
    )


def test_url_for_ipv6_wraps_brackets() -> None:
    assert netinfo.url_for("::1", 8765) == "http://[::1]:8765/"


def test_primary_lan_ip_returns_string_or_none() -> None:
    # Don't assert a specific value; only that we return something safe.
    ip = netinfo.primary_lan_ip()
    assert ip is None or isinstance(ip, str)


def test_tailscale_endpoints_returns_list() -> None:
    # Should never raise, even when Tailscale isn't installed in CI.
    out = netinfo.tailscale_endpoints()
    assert isinstance(out, list)
    for label, host in out:
        assert isinstance(label, str) and isinstance(host, str)
