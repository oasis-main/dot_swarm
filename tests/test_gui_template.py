"""Tests for the `swarm gui` template.

`swarm gui` is a local operator tool. It must render on a plane, behind a
corporate proxy, and inside an egress-blocked container. The template used
to load React, ReactDOM, Babel-standalone and the Tailwind JIT from unpkg
and a CDN, plus a Google webfont — offline that produced a blank page.
These tests keep the template self-contained.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "src" / "dot_swarm" / "templates" / "gui.html"


@pytest.fixture(scope="module")
def html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_template_ships_with_the_package() -> None:
    assert TEMPLATE.is_file(), f"gui.html missing at {TEMPLATE}"


def test_no_external_subresources(html: str) -> None:
    """No src=/href= may point off-box. Any absolute or protocol-relative
    URL in a subresource attribute fails this test."""
    offenders = re.findall(
        r'(?:src|href)\s*=\s*["\']((?:https?:)?//[^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    assert offenders == [], f"external subresources found: {offenders}"


def test_no_known_cdn_hosts(html: str) -> None:
    """Belt and braces: catch a CDN reached from JS (import(), fetch())
    rather than from a tag attribute."""
    for host in ("unpkg.com", "jsdelivr.net", "cdnjs.", "cdn.tailwindcss.com",
                 "fonts.googleapis.com", "fonts.gstatic.com", "esm.sh"):
        assert host not in html, f"template references {host}"


def test_local_assets_only(html: str) -> None:
    """The one asset the page loads is served by the gui handler itself."""
    srcs = set(re.findall(r'src\s*=\s*["\']([^"\']+)["\']', html))
    assert srcs <= {"/logo.png"}, f"unexpected local assets: {srcs}"


def test_no_jsx_left_behind(html: str) -> None:
    """Without Babel in the page, JSX would be a syntax error at load."""
    assert 'type="text/babel"' not in html
    # match calls, not the prose in the file-header comment that explains
    # what this page used to depend on
    assert "ReactDOM.createRoot" not in html
    assert "React.createElement" not in html


def test_fetches_the_state_endpoint(html: str) -> None:
    assert "/api/state.json" in html
