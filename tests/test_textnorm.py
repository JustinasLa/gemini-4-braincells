from urllib.parse import urlsplit

import pytest

from signals.textnorm import normalize_url


@pytest.mark.parametrize(
    "raw, expected, hostname, port",
    [
        (
            "HTTP://[2001:DB8::1]/news/?utm_source=feed&b=2&a=1#section",
            "https://[2001:db8::1]/news?a=1&b=2",
            "2001:db8::1",
            None,
        ),
        ("https://[::1]:443/", "https://[::1]", "::1", None),
        ("http://[2001:db8::1]:80/news", "https://[2001:db8::1]/news", "2001:db8::1", None),
        (
            "https://[2001:db8::1]:8080/news",
            "https://[2001:db8::1]:8080/news",
            "2001:db8::1",
            8080,
        ),
        ("[::ffff:192.0.2.1]/news", "https://[::ffff:192.0.2.1]/news", "::ffff:192.0.2.1", None),
    ],
)
def test_normalize_url_preserves_ipv6_authority(raw, expected, hostname, port):
    normalized = normalize_url(raw)
    assert normalized == expected
    parts = urlsplit(normalized)
    assert parts.hostname == hostname
    assert parts.port == port
    assert normalize_url(normalized) == normalized
