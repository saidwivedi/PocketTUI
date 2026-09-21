"""In-app browser: the pure rewriting half, on strings only.

Everything the proxy does to a page before it reaches the iframe is address
arithmetic, and it has to hold in both directions: a URL the page wrote must
come out under the proxy prefix, and a proxied path must say which target it
names. The rest of these guard the property the rewriter is built for — a
document comes back as the bytes it went in as, apart from the URLs and the
shim — because a page normalised on the way through is a page whose own
scripts stop recognising it.

Hosts here are example.net ones or 127.0.0.1 on purpose: a real tailnet name
in this repo fails the deploy's leak scan.
"""

import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

PREFIX = "/pockettui"
TOK = "TESTTOKEN"
ORIGIN = "https://pockettui.com"
CTX = A.BrowseCtx(PREFIX, TOK, "h", "127.0.0.1:3000", ORIGIN)
CTX_S = A.BrowseCtx(PREFIX, TOK, "s", "box.example.net:443", ORIGIN)
BASE = f"{PREFIX}/b/{TOK}"
SHIM = A.browse_shim_tag(CTX, ORIGIN)


# --- map_url ---------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    # Absolute, with the port filled in from the scheme or kept as written.
    ("http://example.net/a", f"{BASE}/h/example.net:80/a"),
    ("https://example.net/a", f"{BASE}/s/example.net:443/a"),
    ("http://example.net:8080/a", f"{BASE}/h/example.net:8080/a"),
    ("https://example.net:8443/a", f"{BASE}/s/example.net:8443/a"),
    ("HTTP://example.net/a", f"{BASE}/h/example.net:80/a"),
    ("http://example.net", f"{BASE}/h/example.net:80/"),
    ("http://example.net?q=1", f"{BASE}/h/example.net:80/?q=1"),
    ("https://example.net/a?q=1&r=2#frag", f"{BASE}/s/example.net:443/a?q=1&r=2#frag"),
    ("http://user:pw@example.net/a", f"{BASE}/h/example.net:80/a"),
    # A v6 literal keeps its brackets, which is what makes the path a host again.
    ("http://[::1]:9000/x", f"{BASE}/h/[::1]:9000/x"),
    ("https://[2001:db8::1]/x", f"{BASE}/s/[2001:db8::1]:443/x"),
    # ws maps like http, wss like https; the scheme is the shim's to restore.
    ("ws://example.net/sock", f"{BASE}/h/example.net:80/sock"),
    ("wss://example.net/sock", f"{BASE}/s/example.net:443/sock"),
    ("ws://example.net:5173/hmr?token=x", f"{BASE}/h/example.net:5173/hmr?token=x"),
    # Protocol-relative takes the scheme of the page it is on.
    ("//example.net/a", f"{BASE}/h/example.net:80/a"),
    ("//example.net:8080/a.js", f"{BASE}/h/example.net:8080/a.js"),
    # Root-relative means the target this page came from.
    ("/style.css", f"{BASE}/h/127.0.0.1:3000/style.css"),
    ("/", f"{BASE}/h/127.0.0.1:3000/"),
    ("/a/b?q=1#f", f"{BASE}/h/127.0.0.1:3000/a/b?q=1#f"),
    # Whitespace around an attribute value is not part of the URL.
    ("  http://example.net/a  ", f"{BASE}/h/example.net:80/a"),
    ("\n/style.css\t", f"{BASE}/h/127.0.0.1:3000/style.css"),
])
def test_map_url_rewrites(raw, want):
    assert A.browse_map_url(raw, CTX) == want


@pytest.mark.parametrize("raw", [
    "",
    "a/b.png",
    "b.png",
    "../up/b.png",
    "?q=1",
    "#frag",
    "data:image/png;base64,AAAA",
    "blob:https://example.net/8c7f",
    "javascript:void(0)",
    "mailto:someone@example.net",
    "about:blank",
    "tel:+15550100",
    "JAVASCRIPT:void(0)",
    # Already ours: mapping twice would double the prefix.
    f"{BASE}/h/example.net:80/a",
    f"{BASE}/s/127.0.0.1:3000/",
])
def test_map_url_leaves_alone(raw):
    assert A.browse_map_url(raw, CTX) == raw


def test_map_url_protocol_relative_follows_page_scheme():
    assert A.browse_map_url("//example.net/a", CTX_S) == f"{BASE}/s/example.net:443/a"
    assert A.browse_map_url("/a", CTX_S) == f"{BASE}/s/box.example.net:443/a"


@pytest.mark.parametrize("raw", [
    "http://example.net/a", "https://example.net:8443/a?q=1", "//example.net/a",
    "/style.css", "ws://example.net/sock", "a/b.png", "data:text/plain,x",
])
def test_map_url_is_idempotent(raw):
    once = A.browse_map_url(raw, CTX)
    assert A.browse_map_url(once, CTX) == once


# --- unmap_path ------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("http://example.net:8080/a/b?q=1", ("h", "example.net:8080", "/a/b?q=1")),
    ("https://example.net/", ("s", "example.net:443", "/")),
    ("http://[::1]:9000/x", ("h", "[::1]:9000", "/x")),
    ("/deep/path", ("h", "127.0.0.1:3000", "/deep/path")),
])
def test_unmap_round_trips_map(raw, want):
    assert A.browse_unmap_path(A.browse_map_url(raw, CTX), PREFIX) == want


@pytest.mark.parametrize("path", [
    "/other/thing",
    "/pockettui/api/fs",
    f"{PREFIX}/b/",
    f"{PREFIX}/b/{TOK}/h",
    f"{PREFIX}/b/{TOK}/x/example.net:80/a",
    f"{PREFIX}/b/{TOK}/h//a",
])
def test_unmap_refuses_other_paths(path):
    assert A.browse_unmap_path(path, PREFIX) is None


def test_unmap_with_empty_prefix():
    assert A.browse_unmap_path(f"/b/{TOK}/s/example.net:443/a", "") == (
        "s", "example.net:443", "/a")


# --- CSS -------------------------------------------------------------------

@pytest.mark.parametrize("css,want", [
    ("a{background:url(/i.png)}", f"a{{background:url({BASE}/h/127.0.0.1:3000/i.png)}}"),
    ('a{background:url("/i.png")}', f'a{{background:url("{BASE}/h/127.0.0.1:3000/i.png")}}'),
    ("a{background:url('/i.png')}", f"a{{background:url('{BASE}/h/127.0.0.1:3000/i.png')}}"),
    ("a{background:url( /i.png )}", f"a{{background:url({BASE}/h/127.0.0.1:3000/i.png)}}"),
    ("a{background:url(http://example.net/i.png)}",
     f"a{{background:url({BASE}/h/example.net:80/i.png)}}"),
    ("a{background:url(//example.net/i.png)}",
     f"a{{background:url({BASE}/h/example.net:80/i.png)}}"),
    ('@import "/c.css";', f'@import "{BASE}/h/127.0.0.1:3000/c.css";'),
    ("@import '/c.css' screen;", f"@import '{BASE}/h/127.0.0.1:3000/c.css' screen;"),
    ("@import url(/c.css);", f"@import url({BASE}/h/127.0.0.1:3000/c.css);"),
    ('@import url("http://example.net/c.css");',
     f'@import url("{BASE}/h/example.net:80/c.css");'),
    ("@IMPORT \"/c.css\";", f"@IMPORT \"{BASE}/h/127.0.0.1:3000/c.css\";"),
])
def test_rewrite_css(css, want):
    assert A.browse_rewrite_css(css, CTX) == want


@pytest.mark.parametrize("css", [
    "a{background:url(i.png)}",
    "a{background:url(data:image/gif;base64,AAAA)}",
    "a{fill:url(#grad)}",
    "a{color:red}",
])
def test_rewrite_css_leaves_alone(css):
    assert A.browse_rewrite_css(css, CTX) == css


# --- HTML ------------------------------------------------------------------

def rewrite(src, content_type="text/html"):
    body, ctype = A.browse_rewrite_html(src, CTX, content_type)
    assert ctype == "text/html; charset=utf-8"
    return body.decode("utf-8")


@pytest.mark.parametrize("tag,want", [
    ('<a href="/go">x</a>', f'<a href="{BASE}/h/127.0.0.1:3000/go">x</a>'),
    ('<a href="http://example.net/go">x</a>', f'<a href="{BASE}/h/example.net:80/go">x</a>'),
    ('<img src="/i.png">', f'<img src="{BASE}/h/127.0.0.1:3000/i.png">'),
    ('<img src=/i.png>', f'<img src="{BASE}/h/127.0.0.1:3000/i.png">'),
    ('<script src="/j.js"></script>', f'<script src="{BASE}/h/127.0.0.1:3000/j.js"></script>'),
    ('<form action="/post"></form>', f'<form action="{BASE}/h/127.0.0.1:3000/post"></form>'),
    ("<form action='/post'></form>", f"<form action='{BASE}/h/127.0.0.1:3000/post'></form>"),
    ('<button formaction="/alt">s</button>',
     f'<button formaction="{BASE}/h/127.0.0.1:3000/alt">s</button>'),
    ('<video poster="/p.jpg"></video>', f'<video poster="{BASE}/h/127.0.0.1:3000/p.jpg"></video>'),
    ('<object data="/o.bin"></object>', f'<object data="{BASE}/h/127.0.0.1:3000/o.bin"></object>'),
    ('<img srcset="/a.png 1x, /b.png 2x">',
     f'<img srcset="{BASE}/h/127.0.0.1:3000/a.png 1x, {BASE}/h/127.0.0.1:3000/b.png 2x">'),
    ('<link rel="preload" imagesrcset="/a.png 1x">',
     f'<link rel="preload" imagesrcset="{BASE}/h/127.0.0.1:3000/a.png 1x">'),
    ('<div style="background:url(/c.png)"></div>',
     f'<div style="background:url({BASE}/h/127.0.0.1:3000/c.png)"></div>'),
    ('<base href="http://example.net/deep/">',
     f'<base href="{BASE}/h/example.net:80/deep/">'),
    ('<meta http-equiv="refresh" content="3; url=/next">',
     f'<meta http-equiv="refresh" content="3; url={BASE}/h/127.0.0.1:3000/next">'),
    ('<meta http-equiv="refresh" content="0;URL=\'http://example.net/n\'">',
     f'<meta http-equiv="refresh" content="0;URL=\'{BASE}/h/example.net:80/n\'">'),
])
def test_rewrite_html_attributes(tag, want):
    src = f"<html><head></head><body>{tag}</body></html>".encode()
    assert rewrite(src) == (f"<html><head>{SHIM}</head><body>{want}</body></html>")


@pytest.mark.parametrize("tag", [
    '<a href="rel/go">x</a>',
    '<a href="#frag">x</a>',
    '<a href="mailto:someone@example.net">x</a>',
    '<img src="data:image/gif;base64,AAAA">',
    '<img srcset="data:image/gif;base64,AAAA 1x">',
    '<div style="color:red"></div>',
    '<div data="/not-an-object"></div>',
    '<div class="/looks-like-a-path" id="x"></div>',
    '<meta http-equiv="content-type" content="text/html; charset=utf-8">',
    '<meta name="refresh" content="3; url=/next">',
])
def test_rewrite_html_leaves_tag_alone(tag):
    src = f"<html><head></head><body>{tag}</body></html>".encode()
    assert rewrite(src) == (f"<html><head>{SHIM}</head><body>{tag}</body></html>")


def test_rewrite_html_style_element_text():
    src = b"<html><head><style>a{background:url(/bg.png)}</style></head><body></body></html>"
    assert rewrite(src) == (
        f"<html><head>{SHIM}<style>"
        f"a{{background:url({BASE}/h/127.0.0.1:3000/bg.png)}}"
        "</style></head><body></body></html>")


def test_rewrite_html_script_text_is_untouched():
    # A URL inside JS is the shim's problem at runtime, not the rewriter's:
    # rewriting string literals would break as much as it fixed.
    src = b'<html><head><script>var u="/api/x";</script></head><body></body></html>'
    assert rewrite(src) == (
        f'<html><head>{SHIM}<script>var u="/api/x";</script></head><body></body></html>')


def test_rewrite_html_drops_integrity_when_the_url_moved():
    src = (b'<html><head>'
           b'<link rel="stylesheet" href="/s.css" integrity="sha384-abc">'
           b'<script src="/j.js" integrity="sha384-xyz"></script>'
           b'</head><body></body></html>')
    out = rewrite(src)
    assert "sha384-abc" not in out and "sha384-xyz" not in out
    assert f'<link rel="stylesheet" href="{BASE}/h/127.0.0.1:3000/s.css">' in out


@pytest.mark.parametrize("tag", [
    # Nothing moved, so the hash still describes what arrives.
    '<link rel="stylesheet" href="rel/s.css" integrity="sha384-abc">',
    '<script src="rel/j.js" integrity="sha384-abc"></script>',
])
def test_rewrite_html_keeps_integrity_when_the_url_did_not_move(tag):
    src = f"<html><head>{tag}</head><body></body></html>".encode()
    assert rewrite(src) == f"<html><head>{SHIM}{tag}</head><body></body></html>"


def test_rewrite_html_keeps_integrity_on_a_link_that_is_not_a_stylesheet():
    tag = '<link rel="preload" href="/f.woff" integrity="sha384-abc">'
    src = f"<html><head>{tag}</head><body></body></html>".encode()
    out = rewrite(src)
    assert 'integrity="sha384-abc"' in out
    assert f'href="{BASE}/h/127.0.0.1:3000/f.woff"' in out


def test_shim_goes_straight_after_the_head_start_tag():
    src = b'<!doctype html>\n<html lang="en">\n<head profile="x">\n<title>T</title>\n</head></html>'
    out = rewrite(src)
    assert out.startswith('<!doctype html>\n<html lang="en">\n<head profile="x">')
    assert out.index(SHIM) == out.index("<head profile=\"x\">") + len('<head profile="x">')


def test_shim_goes_before_the_first_tag_when_there_is_no_head():
    src = b"<!doctype html>\n<body><p>hi</p></body>"
    out = rewrite(src)
    assert out == f"<!doctype html>\n{SHIM}<body><p>hi</p></body>"


def test_shim_goes_on_top_of_a_document_with_no_tags():
    assert rewrite(b"just text") == f"{SHIM}just text"


DOC = b"""<!doctype html>
<!-- a comment with /a/path.png in it -->
<html lang="en">
<head><meta charset="utf-8"><title>Caf\xc3\xa9 &amp; co</title></head>
<body class="x" data-thing="/keep">
<p>Text with /a/path.png and http://example.net/x in it &mdash; untouched.</p>
<a href="rel/go" title="go &amp; stay">go</a>
<pre>  spaced
  lines  </pre>
</body></html>"""


def test_a_document_with_no_rewritable_url_is_byte_identical_apart_from_the_shim():
    out = rewrite(DOC)
    assert out.replace(SHIM, "", 1) == DOC.decode("utf-8")


def test_text_nodes_and_unrelated_attributes_survive_a_rewrite():
    src = DOC.replace(b'<a href="rel/go"', b'<a href="/go"')
    out = rewrite(src)
    plain = out.replace(SHIM, "", 1).replace(
        f'href="{BASE}/h/127.0.0.1:3000/go"', 'href="/go"')
    assert plain == src.decode("utf-8")


def test_charset_comes_from_the_content_type():
    src = "<html><head></head><body><p>Café</p></body></html>".encode("latin-1")
    body, ctype = A.browse_rewrite_html(src, CTX, "text/html; charset=ISO-8859-1")
    assert ctype == "text/html; charset=utf-8"
    assert body.decode("utf-8") == (
        f"<html><head>{SHIM}</head><body><p>Café</p></body></html>")


def test_charset_comes_from_the_meta_tag():
    src = ('<html><head><meta charset="windows-1252">'
           "<title>naïve</title></head><body></body></html>").encode("cp1252")
    out = A.browse_rewrite_html(src, CTX, "text/html")[0]
    assert "naïve" in out.decode("utf-8")


def test_charset_comes_from_a_meta_http_equiv():
    src = ('<html><head>'
           '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
           "<title>Café</title></head><body></body></html>").encode("latin-1")
    out = A.browse_rewrite_html(src, CTX, None)[0]
    assert "Café" in out.decode("utf-8")


def test_an_unknown_charset_falls_back_to_utf8():
    src = "<html><head></head><body>Café</body></html>".encode("utf-8")
    assert "Café" in A.browse_rewrite_html(src, CTX, "text/html; charset=x-made-up")[0].decode()


def test_undecodable_bytes_do_not_stop_the_rewrite():
    src = b"<html><head></head><body><a href='/go'>\xff\xfe</a></body></html>"
    out = A.browse_rewrite_html(src, CTX, "text/html")[0].decode("utf-8")
    assert f"{BASE}/h/127.0.0.1:3000/go" in out


def test_ampersand_in_a_rewritten_query_survives_the_round_trip():
    src = b'<html><head></head><body><a href="/go?a=1&amp;b=2">x</a></body></html>'
    out = rewrite(src)
    assert f'href="{BASE}/h/127.0.0.1:3000/go?a=1&amp;b=2"' in out


def test_a_quote_in_a_rewritten_value_is_escaped():
    src = b'<html><head></head><body><a href="/go?q=%22x%22&quot;">x</a></body></html>'
    out = rewrite(src)
    assert out.count('">x</a>') == 1
    assert "&quot;" in out


# --- shim ------------------------------------------------------------------

def test_shim_is_small_enough_to_sit_on_every_page():
    assert len(A.BROWSE_SHIM.encode("utf-8")) < 6144


def test_shim_hands_a_traversal_to_the_pane():
    # back/forward/go inside the frame would walk the shell's joint history,
    # so the shim posts the step out instead of taking it.
    assert '"pockettui-history"' in A.BROWSE_SHIM
    for n in ("history.go=", "history.back=", "history.forward="):
        assert n in A.BROWSE_SHIM


def test_shim_parses_as_javascript(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    f = tmp_path / "shim.js"
    f.write_text(A.BROWSE_SHIM, encoding="utf-8")
    subprocess.run([node, "--check", str(f)], check=True, capture_output=True)


def test_shim_cannot_close_its_own_script_element():
    assert "</script" not in A.BROWSE_SHIM.lower()


def test_shim_tag_config_round_trips():
    tag = A.browse_shim_tag(CTX, ORIGIN)
    raw = re.search(r"data-cfg='([^']*)'", tag).group(1)
    assert json.loads(html.unescape(raw)) == {
        "prefix": PREFIX, "tok": TOK, "sch": "h",
        "hostport": "127.0.0.1:3000", "origin": ORIGIN}


def test_shim_tag_config_cannot_break_out_of_its_attribute():
    ctx = A.BrowseCtx(PREFIX, "a'b", "h", "<script>:80", "https://x.example.net")
    tag = A.browse_shim_tag(ctx, "'></script><img src=x>")
    raw = re.search(r"data-cfg='([^']*)'", tag).group(1)
    assert "<" not in raw and "'" not in raw
    cfg = json.loads(html.unescape(raw))
    assert cfg["tok"] == "a'b" and cfg["origin"] == "'></script><img src=x>"


# --- error page ------------------------------------------------------------

@pytest.mark.parametrize("code,detail,want", [
    ("refused", "127.0.0.1:3000", "Nothing is answering at 127.0.0.1:3000"),
    ("timeout", "slow.example.net:443", "slow.example.net:443 did not answer in time"),
    ("tls", "box.example.net:443", "box.example.net:443 refused a secure connection"),
    ("unknown_host", "nope.example.net:80",
     "nope.example.net:80 is not a name this computer knows"),
    ("loop", "127.0.0.1:5560", "That address is PocketTUI itself"),
    ("expired", "", "This browser link has expired"),
    ("something_else", "", "The page could not be loaded"),
])
def test_error_page_titles(code, detail, want):
    page = A.browse_error_page(code, detail, ORIGIN)
    assert f"<h1>{want}</h1>" in page
    assert f"<title>{want}</title>" in page


def test_error_page_escapes_the_detail():
    page = A.browse_error_page("refused", "<script>alert(1)</script>", ORIGIN)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_error_page_posts_to_the_shell_origin():
    page = A.browse_error_page("refused", "127.0.0.1:3000", ORIGIN)
    assert '"pockettui-browse-error"' in page
    assert f'}}, "{ORIGIN}")' in page


def test_error_page_posts_to_anyone_when_the_origin_is_unknown():
    assert '}, "*")' in A.browse_error_page("expired", "", None)


def test_error_page_offers_a_retry_and_both_themes():
    page = A.browse_error_page("timeout", "slow.example.net:443", ORIGIN)
    assert 'onclick="location.reload()"' in page
    assert "@media (prefers-color-scheme: dark)" in page
    assert "#f4efe6" in page and "#1c1a17" in page


# --- headers ---------------------------------------------------------------

def test_page_headers_match_the_iframe_sandbox_word_for_word():
    headers = A.browse_page_headers()
    assert headers["Content-Security-Policy"] == A.BROWSE_SANDBOX
    assert A.BROWSE_SANDBOX == (
        "sandbox allow-scripts allow-forms allow-popups allow-modals "
        "allow-downloads allow-popups-to-escape-sandbox")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
