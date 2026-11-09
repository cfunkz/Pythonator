import pytest

from pythonator.net.gate import address_allowed, host_allowed


@pytest.mark.parametrize(
    ("host", "port", "ok"),
    [
        ("api.example.com", 443, True),
        ("eu.queue.example.net", 443, True),
        ("queue.example.net", 443, False),  # *.queue.example.net is subdomains only
        ("evil-example.com", 443, False),
        ("api.github.com", 443, True),
        ("api.github.com", 22, False),  # port pinned
        ("API.EXAMPLE.COM.", 443, True),
    ],
)
def test_host_rules(host, port, ok):
    assert host_allowed(host, port, ("api.example.com", "*.queue.example.net", "api.github.com:443")) is ok


@pytest.mark.parametrize(
    ("ip", "lan", "ok"),
    [
        ("1.1.1.1", False, True),
        ("127.0.0.1", True, False),
        ("169.254.169.254", True, False),  # cloud metadata, always
        ("100.100.100.200", True, False),
        ("fd00:ec2::254", True, False),
        ("::ffff:127.0.0.1", True, False),
        ("192.168.1.10", False, False),
        ("192.168.1.10", True, True),
        ("0.0.0.0", True, False),  # noqa: S104 - an address checked, not bound
        ("224.0.0.1", True, False),
    ],
)
def test_addresses(ip, lan, ok):
    assert address_allowed(ip, lan) is ok
