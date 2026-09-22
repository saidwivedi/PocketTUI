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
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

PREFIX = "/pockettui"
TOK = "TESTTOKEN"
ORIGIN = "https://pockettui.com"
CTX = A.BrowseCtx(PREFIX, TOK, "h", "127.0.0.1:3000", ORIGIN)
CTX_S = A.BrowseCtx(PREFIX, TOK, "s", "box.example.net:443", ORIGIN)
# The unsandboxed flavour, which a tab put on the computer's own network is
# served under. Everything the pane's own flavour does is byte-for-byte what it
# was; only this one grows a script wrapper.
CTX_TAB = A.BrowseCtx(PREFIX, TOK, "h", "127.0.0.1:3000", ORIGIN, sandbox=False)
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


# --- map_url on an address that is already the proxy's ----------------------
# The founder's runaway started here. A restart mints a new token, so a page
# still open carries a dead one; the rewriter saw an address on some host it
# did not recognise and wrapped it, and the wrap was the proxy's own public
# name. Every one of these has to come out as the target at the bottom of it,
# under the token this process is minting now.

OTHER = "https://ps054.example-tailnet.example.net"
DEAD = "DEADTOKEN"


@pytest.mark.parametrize("raw,want", [
    # This backend's public name, with a token it no longer knows.
    (f"{OTHER}{PREFIX}/b/{DEAD}/s/portal.example.net:443/startPage",
     f"{BASE}/s/portal.example.net:443/startPage"),
    # The same, bare: `tailscale serve` strips the prefix on the way in, so
    # both spellings are in circulation at once.
    (f"{OTHER}/b/{DEAD}/h/box.example.net:8080/a?q=1",
     f"{BASE}/h/box.example.net:8080/a?q=1"),
    # Two layers, the inner one under the prefix the browser saw.
    (f"{OTHER}{PREFIX}/b/{DEAD}/s/{OTHER[8:]}:443{PREFIX}/b/{DEAD}"
     f"/s/portal.example.net:443/startPage",
     f"{BASE}/s/portal.example.net:443/startPage"),
    # Root-relative, which is how a link on such a page reads.
    (f"{PREFIX}/b/{DEAD}/s/portal.example.net:443/startPage",
     f"{BASE}/s/portal.example.net:443/startPage"),
    # And nested root-relative, the shape a second pass would have made.
    (f"{PREFIX}/b/{DEAD}/h/127.0.0.1:3000{PREFIX}/b/{DEAD}/h/box.example.net:80/x",
     f"{BASE}/h/box.example.net:80/x"),
])
def test_map_url_peels_an_address_that_is_already_ours(raw, want):
    assert A.browse_map_url(raw, CTX) == want


def test_map_url_peels_to_a_fixed_point():
    raw = f"{OTHER}{PREFIX}/b/{DEAD}/s/portal.example.net:443/startPage"
    once = A.browse_map_url(raw, CTX)
    assert A.browse_map_url(once, CTX) == once


# --- peel ------------------------------------------------------------------

@pytest.mark.parametrize("path,want", [
    # One layer, under the prefix and bare.
    (f"{PREFIX}/b/{TOK}/h/example.net:80/a", ("h", "example.net:80", "/a")),
    (f"/b/{TOK}/h/example.net:80/a", ("h", "example.net:80", "/a")),
    # The innermost, not the outermost: the layers above it are all this proxy.
    (f"{PREFIX}/b/{TOK}/h/a.example.net:80{PREFIX}/b/{DEAD}/s/b.example.net:443/x",
     ("s", "b.example.net:443", "/x")),
    (f"/b/{TOK}/h/a.example.net:80/b/{DEAD}/h/b.example.net:80/b/x/h/c.example.net:80/y",
     ("h", "c.example.net:80", "/y")),
    # No path at all is the target's root.
    (f"{PREFIX}/b/{TOK}/h/example.net:80", ("h", "example.net:80", "/")),
    # The query rides along with the innermost path.
    (f"{PREFIX}/b/{TOK}/h/example.net:80/a?q=1", ("h", "example.net:80", "/a?q=1")),
    # Not ours: a path of the target's that merely begins the same way.
    ("/b/only-two/parts", None),
    ("/other/thing", None),
    (f"{PREFIX}/api/fs", None),
    (f"{PREFIX}/b/{TOK}/x/example.net:80/a", None),
])
def test_peel_reads_every_layer(path, want):
    assert A.browse_peel(path, PREFIX) == want


def test_peel_is_bounded():
    """A crafted address cannot spin the loop, it only reaches the bottom."""
    deep = f"/b/{TOK}/h/a.example.net:80" * (A.BROWSE_PEEL_MAX + 5) + "/end"
    got = A.browse_peel(deep, PREFIX)
    assert got is not None and got[1] == "a.example.net:80"


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


SHIM_TAB = A.browse_shim_tag(CTX_TAB, ORIGIN)


def rewrite_tab(src):
    """The document as the tab flavour serves it, minus the shim itself.

    The shim carries the same wrapper as a string, so an assertion about what
    the page got would otherwise match the shim's own copy of it.
    """
    body, ctype = A.browse_rewrite_html(src, CTX_TAB, "text/html")
    assert ctype == "text/html; charset=utf-8"
    out = body.decode("utf-8")
    assert SHIM_TAB in out
    return out.replace(SHIM_TAB, "")


def test_inline_script_reads_the_pages_own_top_in_the_tab_flavour():
    # A page on the computer's own network is a document in its own right, but
    # it is still drawn in the pane's frame: `top` is the shell and reading it
    # throws. `with` points the bare word at what the shim worked out the page's
    # own top would be, and the member spelling is rewritten because `with`
    # cannot reach it.
    out = rewrite_tab(b"<html><head><script>if(top===self){go()}</script>"
                      b"</head><body></body></html>")
    assert "with(window.__pt||{}){if(top===self){go()}\n}" in out


def test_inline_script_is_untouched_in_the_panes_own_flavour():
    # The pane's pages are exactly what they were. This is the whole of the
    # blast radius: one flavour changed, the other not at all.
    src = b"<html><head><script>if(top===self){go()}</script></head><body></body></html>"
    assert rewrite(src) == (
        f"<html><head>{SHIM}<script>if(top===self){{go()}}</script>"
        "</head><body></body></html>")


def test_a_script_that_never_mentions_either_word_is_left_alone():
    # `with` scopes a top-level let/const/class to the block, so a script with
    # no stake in this is not wrapped at all — which is most of them.
    out = rewrite_tab(b"<html><head><script>const x=1;</script></head>"
                      b"<body></body></html>")
    assert "<script>const x=1;</script>" in out
    assert "__pt" not in out


def test_a_module_and_a_json_island_are_left_alone():
    # A module has its own scope rules and `with` is a SyntaxError in one; a
    # JSON island is not code at all.
    for attr in ('type="module"', 'type="application/json"',
                 'type="text/x-template"'):
        out = rewrite_tab(f"<html><head><script {attr}>"
                          "{\"top\": 1}</script></head><body></body></html>"
                          .encode("utf-8"))
        assert "with(window.__pt" not in out, attr


def test_a_strict_script_is_left_alone():
    # `with` is a SyntaxError in strict code, so such a script is handed on.
    out = rewrite_tab(b'<html><head><script>"use strict";var a=top;</script>'
                      b"</head><body></body></html>")
    assert "with(window.__pt" not in out
    assert '<script>"use strict";var a=top;</script>' in out
    # Even behind the comments a build tool leaves in front of the directive.
    assert A.browse_wrap_js("// c\n/* d */\n'use strict'\nvar a=top;") == (
        "// c\n/* d */\n'use strict'\nvar a=top;")


def test_the_directive_test_does_not_backtrack():
    """The head-of-script scan is walked, not matched.

    A regex over runs of whitespace and lazy block comments backtracks
    exponentially on a script that opens with comments and no directive behind
    them — which is every minified library carrying a licence header — and it
    ran on the loop thread, so the whole server stopped.
    """
    src = ("/**/\n" * 60) + "x=top;"
    t0 = time.monotonic()
    out = A.browse_wrap_js(src)
    assert time.monotonic() - t0 < 0.5
    assert out.startswith("with(window.__pt")


def test_a_directive_behind_comments_is_still_found():
    for src in ("'use strict';\nwindow.x=1;var a=top;",
                '/* c */ // d\n  "use strict";\nvar a=top;'):
        assert A.browse_wrap_js(src) == src


def test_an_unterminated_comment_is_not_a_directive():
    # And does not hang: the scan stops where the comment does not end.
    t0 = time.monotonic()
    assert A.browse_wrap_js("/* unterminated top").startswith("with(window.__pt")
    assert A.browse_starts_strict("/* unterminated 'use strict'") is False
    assert time.monotonic() - t0 < 0.5


def test_a_library_sized_script_is_wrapped_in_good_time():
    # Half a megabyte with a licence header's worth of comments in it, which
    # is the shape of the file that hung the proxy.
    src = ("/* c */\n" * 400) + ("var pad='" + "p" * 500_000 + "';\n") + "var a=top;"
    t0 = time.monotonic()
    out = A.browse_wrap_js(src)
    assert time.monotonic() - t0 < 1.0
    assert out.startswith("with(window.__pt")


def test_only_the_two_members_of_a_named_global_are_rewritten():
    # window.top / self.parent / globalThis.top, and nothing else: a .top on
    # somebody else's object is that object's own property.
    got = A.browse_wrap_js("a=window.top;b=self.parent;c=globalThis.top;"
                           "d=el.top;e=o.window.top;")
    assert "a=__pt.top;b=__pt.parent;c=__pt.top;d=el.top;" in got
    # o.window.top is window.top by any other name, and is rewritten as one.
    assert "e=o.__pt.top;" in got or "e=o.window.top;" in got


def test_a_script_ending_in_a_line_comment_still_closes():
    # The brace goes on a line of its own, or the last line's // swallows it.
    assert A.browse_wrap_js("var a=top;//end").endswith("//end\n}")


def test_the_wrapper_never_ends_the_script_element():
    # The one invariant that has to hold whatever the wrapper does: a
    # </script> inside a string is the page's business, and nothing added
    # around it may introduce another one.
    body = 'var s="</scr"+"ipt>";var t=top;'
    assert "</scr" + "ipt" not in A.browse_wrap_js(body).replace(body, "")


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
    # A budget, not a protocol limit: the shim rides on every proxied document,
    # so it is kept small deliberately. Raise the cap when behaviour needs the
    # room rather than trimming what the shim does — which is what the tab
    # flavour did: window.open, the storage gate and the close listener are
    # three behaviours the pane has no other way to get, and the cookie jar and
    # the credentials gate are two more the sandbox leaves no other way to
    # have, and handing a targeted link and a scripted open to the pane is one
    # more: a window the laptop's browser opens is out of the pane for good.
    # The last raise bought three the sandbox leaves no other way to have
    # either: the reads it answers with a SecurityError made absent, the frames
    # it will not share read as not loaded, and a request body buffered before
    # it goes out. Without them the YouTube app did not boot at all.
    assert len(A.BROWSE_SHIM.encode("utf-8")) < 17408


def test_shim_reads_a_proxy_path_without_the_token():
    # B builds an address; this reads one. Reading with B would mean a page
    # still open under a restart's old token did not recognise its own
    # location as the proxy's, and wrapped it a layer deeper per link.
    assert "/b/[^/]+/([hs])/([^/]+)(/.*)?$" in A.BROWSE_SHIM
    assert "function peel(" in A.BROWSE_SHIM


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


def test_shim_maps_the_urls_in_markup_a_page_writes(tmp_path):
    # document.write hands the parser a string that was never in the bytes the
    # rewriter saw and never passes an element setter, so without this the
    # root-relative src= in it is fetched from this server's root. It is how
    # SAP's portal loads its UI5 core, and the whole page stayed blank for it.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    cfg = {"prefix": PREFIX, "tok": TOK, "sch": "h",
           "hostport": "127.0.0.1:3000", "origin": ORIGIN}
    here = "http://box.example.net:8080" + BASE + "/h/127.0.0.1:3000/page"
    harness = """
const written = [];
globalThis.window = globalThis;
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:", href: HERE,
  pathname: new URL(HERE).pathname, search: "", hash: "", reload() {}};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: HERE,
  addEventListener() {}, write(s) { written.push(s); },
  writeln(s) { written.push(s); }};
globalThis.addEventListener = function () {};
globalThis.history = {};
SHIM
for (const s of CASES) document.write(s);
console.log(JSON.stringify(written));
"""
    f = tmp_path / "harness.mjs"
    cases = [
        # what SAP writes: a root-relative script src, mapped onto the target
        '<script id="sap-ui-bootstrap" src="/sapui5/core-min-0.js"></scr' + 'ipt>',
        # an absolute URL to a third host, and an unquoted value
        '<img src=http://other.example.net/a.png><a href="/b">x</a>',
        # a value this call left half written, prose that only looks like one,
        # and a relative value the proxied base already resolves
        '<img src="/half',
        'say src=nothing and move on',
        '<img src="thumb.png">',
    ]
    # the shim goes in last: it is the one substitution whose text must not
    # be searched for the placeholders that follow.
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("HERE", json.dumps(here))
                 .replace("CASES", json.dumps(cases))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode("utf-8"))
    # spelled from the frame's own origin, the way the shim maps everywhere
    site = "http://box.example.net:8080" + BASE
    assert f'src="{site}/h/127.0.0.1:3000/sapui5/core-min-0.js"' in got[0]
    assert 'id="sap-ui-bootstrap"' in got[0]
    assert f'src={site}/h/other.example.net:80/a.png' in got[1]
    assert f'href="{site}/h/127.0.0.1:3000/b"' in got[1]
    assert got[2] == cases[2]
    assert got[3] == cases[3]
    assert got[4] == cases[4]


def test_shim_hands_a_targeted_link_to_the_pane():
    # A link with target="_blank" used to be left to the browser, and the
    # frame's sandbox carries allow-popups: the arxiv links on a proxied page
    # opened in a window of the laptop's browser, outside the pane for good.
    # In the pane a named target is posted out instead; a tab is a window in
    # its own right and its browser still knows where to put one.
    assert 'var t=(a.getAttribute("target")||"").toLowerCase(),n=t&&t!=="_self";' \
        in A.BROWSE_SHIM
    assert "if(n&&!C.sandbox)return;" in A.BROWSE_SHIM
    # _parent and _top name the shell, which the sandbox refuses anyway, so
    # they stay this frame's own navigation rather than becoming a tab.
    assert 'if(n&&t!=="_parent"&&t!=="_top"){out(h);return}' in A.BROWSE_SHIM
    assert '"pockettui-open"' in A.BROWSE_SHIM


def test_shim_opens_the_pane_a_tab_rather_than_a_window(tmp_path):
    # The pane flavour has no window to hand back: an open goes out as an
    # address for the pane to put in a tab of its own, unmapped, and the call
    # answers null. The tab flavour, where a real window works, is untouched
    # (test_shim_maps_the_window_the_page_opens, below).
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    cfg = {"prefix": PREFIX, "tok": TOK, "sch": "h",
           "hostport": "127.0.0.1:3000", "origin": ORIGIN, "sandbox": True}
    here = "http://box.example.net:8080" + BASE + "/h/127.0.0.1:3000/page"
    harness = """
const posted = [], opened = [], back = [];
globalThis.window = globalThis;
globalThis.parent = {postMessage(m, o) { posted.push([m, o]); }};
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:", href: HERE,
  pathname: new URL(HERE).pathname, search: "", hash: "",
  replace() {}, reload() {}};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: HERE,
  addEventListener() {}, write() {}, writeln() {}};
globalThis.addEventListener = function () {};
globalThis.history = {};
globalThis.open = function (u, n, f) { opened.push([u, n, f]); return {}; };
SHIM
back.push(window.open("/child", "view1"));
back.push(window.open("http://other.example.net/x"));
back.push(window.open());
back.push(window.open("about:blank"));
console.log(JSON.stringify({posted, opened, back}));
"""
    f = tmp_path / "open-pane.mjs"
    # the shim goes in last, for the reason the document.write harness gives.
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("HERE", json.dumps(here))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode("utf-8"))
    # The address the pane is given is the one the page meant, not the proxied
    # spelling it clicked: the pane proxies it again itself.
    assert got["posted"] == [
        [{"type": "pockettui-open", "url": "http://127.0.0.1:3000/child"}, ORIGIN],
        [{"type": "pockettui-open", "url": "http://other.example.net/x"}, ORIGIN],
    ]
    # A blank window is nothing to open, and no window of the device's own is
    # ever asked for.
    assert got["opened"] == []
    assert got["back"] == [None, None, None, None]


def test_shim_maps_the_window_the_page_opens(tmp_path):
    # A portal opens its views in windows of their own. Unmapped, each one
    # leaves the proxy for an address only the workstation can reach — and in
    # a tab, where these actually work, that is the whole navigation.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    cfg = {"prefix": PREFIX, "tok": TOK, "sch": "h",
           "hostport": "127.0.0.1:3000", "origin": ORIGIN, "sandbox": False}
    here = "http://box.example.net:8080" + BASE + "/h/127.0.0.1:3000/page"
    harness = """
const opened = [];
globalThis.window = globalThis;
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:", href: HERE,
  pathname: new URL(HERE).pathname, search: "", hash: "", reload() {}};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: HERE,
  addEventListener() {}, write() {}, writeln() {}};
globalThis.addEventListener = function () {};
globalThis.history = {};
globalThis.open = function (u, n, f) { opened.push([u, n, f]); return {}; };
SHIM
window.open("/child", "view1");
window.open("http://other.example.net/x");
window.open();
console.log(JSON.stringify(opened));
"""
    f = tmp_path / "open.mjs"
    # the shim goes in last, for the reason the document.write harness gives.
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("HERE", json.dumps(here))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode("utf-8"))
    site = "http://box.example.net:8080" + BASE
    assert got[0] == [f"{site}/h/127.0.0.1:3000/child", "view1", None]
    assert got[1][0] == f"{site}/h/other.example.net:80/x"
    # An open with no address is a blank window, and stays one.
    assert got[2] == [None, None, None]


def test_shim_asks_no_page_to_close_itself():
    """Nothing opens a window of the device's own any more: a page on the
    computer's network is shown in the pane's own frame, and the frame goes
    when the tab does. The listener that used to close such a window is gone
    with the only thing that ever posted to it."""
    assert "pockettui-close" not in A.BROWSE_SHIM


def test_shim_leaves_an_unsandboxed_page_its_own_storage():
    """The polyfill is for a document that has none: sandboxed, it sits on an
    opaque origin where every storage access throws. A tab-flavour page is on
    this server's real origin — which the mint only hands out to a shell served
    from somewhere else — and the storage it has is the one it should use."""
    assert 'var SB=!!C.sandbox||window.origin==="null";' in A.BROWSE_SHIM
    assert "if(!SB)return;\n try{localStorage.length;return}catch(e){}" in A.BROWSE_SHIM


def test_shim_fakes_a_cookie_jar_only_where_the_real_one_throws():
    """Sandboxed, every document.cookie read throws for want of
    allow-same-origin, and a script that cannot read one decides cookies are
    off: Google answers a search with its "having trouble accessing Google
    Search" page. A tab-flavour page has a real cookie and keeps it."""
    assert "if(!SB)return;\n try{void document.cookie;return}catch(e){}" in A.BROWSE_SHIM
    assert 'Object.defineProperty(Document.prototype,"cookie",d)' in A.BROWSE_SHIM
    # The prototype is where the accessor lives, but a browser that refuses it
    # there still has the one document this page is.
    assert 'Object.defineProperty(document,"cookie",d)' in A.BROWSE_SHIM


def test_shim_makes_what_an_opaque_origin_refuses_read_as_absent(tmp_path):
    """The three reads a sandboxed Chrome answers with a SecurityError, run for
    real: window.caches, navigator.serviceWorker and the window of a frame the
    page made for itself. A bundle that feature-detects by reading one dies on
    the spot — the YouTube app read caches while its page was still building
    and never booted, and it took an about:blank frame's history next — so each
    reads as absent instead. A frame with an address of its own keeps its
    window: cross-origin is what a page expects there, and postMessage into it
    still has to work."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    cfg = {"prefix": PREFIX, "tok": TOK, "sch": "h",
           "hostport": "127.0.0.1:3000", "origin": ORIGIN, "sandbox": True}
    here = "http://box.example.net:8080" + BASE + "/h/127.0.0.1:3000/page"
    harness = """
const out = {};
globalThis.window = globalThis;
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:", href: HERE,
  pathname: new URL(HERE).pathname, search: "", hash: "", reload() {}};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: HERE,
  addEventListener() {}, write() {}, writeln() {}};
globalThis.addEventListener = function () {};
globalThis.history = {};
function refuse(o, n) {
  Object.defineProperty(o, n, {configurable: true, get() {
    const e = new Error("sandboxed and lacks the allow-same-origin flag");
    e.name = "SecurityError";
    throw e;
  }});
}
refuse(globalThis, "caches");
// node has a navigator of its own, and it is read-only there
Object.defineProperty(globalThis, "navigator",
  {configurable: true, writable: true, value: {}});
refuse(globalThis.navigator, "serviceWorker");
// A child frame of a sandboxed document lands on an opaque origin of its own,
// so reading any property of its window throws however it was made.
const foreign = {get document() {
  const e = new Error("Blocked a frame");
  e.name = "SecurityError";
  throw e;
}};
globalThis.HTMLIFrameElement = function () {};
Object.defineProperty(HTMLIFrameElement.prototype, "contentWindow",
  {configurable: true, get() { return foreign; }});
function frame(attrs) {
  const f = Object.create(HTMLIFrameElement.prototype);
  f.getAttribute = (n) => (n in attrs ? attrs[n] : null);
  f.hasAttribute = (n) => n in attrs;
  return f;
}
function read(f) { try { return f(); } catch (e) { return "THROW " + e.name; } }
SHIM
out.caches = read(() => ("caches" in window ? String(window.caches) : "absent"));
out.serviceWorker = read(() => String(navigator.serviceWorker));
out.blank = read(() => frame({src: "about:blank"}).contentWindow);
out.empty = read(() => frame({}).contentWindow);
out.page = read(() => frame({src: "http://127.0.0.1:3000/x"}).contentWindow === foreign);
out.srcdoc = read(() => frame({srcdoc: "<p>hi"}).contentWindow === foreign);
console.log(JSON.stringify(out));
"""
    f = tmp_path / "refused.mjs"
    # the shim goes in last, for the reason the document.write harness gives.
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("HERE", json.dumps(here))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode("utf-8"))
    # Gone where the property is the object's own, undefined where it is
    # inherited from a prototype the shim cannot take it off; never a throw.
    assert got["caches"] in ("absent", "undefined")
    assert got["serviceWorker"] == "undefined"
    assert got["blank"] is None
    assert got["empty"] is None
    assert got["page"] is True
    assert got["srcdoc"] is True


def test_shim_drops_credentials_only_from_a_document_with_no_origin():
    """An opaque origin cannot ask for credentials: every request it makes is
    cross-origin, and Chrome refuses a wildcard Access-Control-Allow-Origin for
    a credentialed one, which is what took out Google's own /complete/s and
    /xjs/ fetches. Nothing is lost — the jar is server-side and /b/ drops the
    frame's Cookie header — so the ask is dropped rather than the answer."""
    for line in (
        'if(i&&i.credentials==="include")i=new Request(i,{credentials:"same-origin"});',
        'if(o&&o.credentials==="include")o=Object.assign({},o,'
        '{credentials:"same-origin"});',
        # And a request that asks to stay on its own origin, which from an
        # opaque one nothing is: YouTube's cookie upgrade asked for it and was
        # refused before it was sent, and the consent wall stayed up.
        'if(i&&i.mode==="same-origin")i=new Request(i,{mode:"cors"});',
        'if(o&&o.mode==="same-origin")o=Object.assign({},o,{mode:"cors"});',
    ):
        assert line in A.BROWSE_SHIM
    assert ("P(function(){if(!SB)return;\n"
            ' // The same refusal on the XHR path, and the same reason the flag'
            " is a no-op.\n"
            ' Object.defineProperty(XMLHttpRequest.prototype,"withCredentials",{\n'
            "  configurable:true,get:function(){return false},set:function(){}});"
            ) in A.BROWSE_SHIM
    # Both turn on the same word the storage and cookie polyfills gate on:
    # five blocks return early on it — storage, the cookie jar, the XHR flag,
    # the reads an opaque origin refuses and the frames it will not share —
    # and the fetch override branches on it.
    assert A.BROWSE_SHIM.count("if(!SB)return;") == 5
    assert "if(SB)try{" in A.BROWSE_SHIM


def test_shim_sends_a_request_with_its_body_buffered(tmp_path):
    """A Request rebuilt around another one takes that one's body as a stream,
    and Chrome will not put a streamed upload on an HTTP/1.1 connection, which
    is all uvicorn speaks: it fails the call with ERR_ALPN_NEGOTIATION_FAILED
    before it is sent. Mapping the URL and dropping the credentials are two
    such rebuilds, so every POST a page made with a Request object died here —
    the YouTube app makes all of its youtubei calls that way, its consent form
    among them. The body is read back into one buffer after the last rebuild,
    and a call with no body still goes out untouched."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    cfg = {"prefix": PREFIX, "tok": TOK, "sch": "h",
           "hostport": "127.0.0.1:3000", "origin": ORIGIN, "sandbox": True}
    here = "http://box.example.net:8080" + BASE + "/h/127.0.0.1:3000/page"
    harness = """
const sent = [];
globalThis.window = globalThis;
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:", href: HERE,
  pathname: new URL(HERE).pathname, search: "", hash: "", reload() {}};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: HERE,
  addEventListener() {}, write() {}, writeln() {}};
globalThis.addEventListener = function () {};
globalThis.history = {};
Object.defineProperty(globalThis, "navigator",
  {configurable: true, writable: true, value: {}});
const enc = (s) => new TextEncoder().encode(s).buffer;
const dec = (b) => new TextDecoder().decode(new Uint8Array(b));
// A Request the way a browser builds one. What matters is where the body came
// from: bytes handed in are bytes on the wire, and anything taken off another
// Request — which is all a rebuild can do — arrives as a stream.
class Req {
  constructor(input, init) {
    init = init || {};
    const from = typeof input === "object" ? input : null;
    const b = "body" in init ? init.body : null;
    this.url = from ? from.url : String(input);
    this.method = init.method || (from && from.method) || "GET";
    this.credentials = init.credentials || (from && from.credentials) || "omit";
    this.bodyUsed = false;
    if (b && typeof b.getReader === "function") { this.kind = "stream"; this.payload = b.payload; }
    else if (b instanceof ArrayBuffer) { this.kind = "buffer"; this.payload = dec(b); }
    else if (b != null) { this.kind = "bytes"; this.payload = String(b); }
    else if (from && from.payload != null) { this.kind = "stream"; this.payload = from.payload; }
    else { this.kind = "none"; this.payload = null; }
    this.body = this.payload == null ? null
      : {payload: this.payload, getReader() {}};
  }
  clone() { return new Req(this.url, {body: this.payload}); }
  arrayBuffer() { this.bodyUsed = true; return Promise.resolve(enc(this.payload)); }
}
globalThis.Request = Req;
globalThis.fetch = function (i, o) {
  sent.push({url: i.url, method: i.method, kind: i.kind, payload: i.payload,
             credentials: i.credentials});
  return Promise.resolve("answered");
};
SHIM
const post = new Req("http://127.0.0.1:3000/youtubei/v1/guide",
  {method: "POST", body: '{"a":1}', credentials: "include"});
const get = new Req("http://127.0.0.1:3000/feed");
Promise.all([window.fetch(post), window.fetch(get)]).then((answers) => {
  console.log(JSON.stringify({sent, answers}));
});
"""
    f = tmp_path / "body.mjs"
    # the shim goes in last, for the reason the document.write harness gives.
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("HERE", json.dumps(here))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([node, str(f)], check=True, capture_output=True)
    got = json.loads(out.stdout.decode("utf-8"))
    site = "http://box.example.net:8080" + BASE + "/h/127.0.0.1:3000"
    assert got["sent"] == [
        {"url": f"{site}/feed", "method": "GET", "kind": "none",
         "payload": None, "credentials": "omit"},
        # the body it was given, spelled as bytes rather than as the stream
        # every rebuild would have handed the connection
        {"url": f"{site}/youtubei/v1/guide", "method": "POST", "kind": "buffer",
         "payload": '{"a":1}', "credentials": "same-origin"},
    ]
    # The answer still reaches the caller through the buffering.
    assert got["answers"] == ["answered", "answered"]


def test_shim_cookie_jar_holds_what_a_page_writes(tmp_path):
    """The accessor, run for real: a set is readable, an attribute is ignored
    and a deletion by expiry takes the name out."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    cfg = {"prefix": PREFIX, "tok": TOK, "sch": "h",
           "hostport": "127.0.0.1:3000", "origin": ORIGIN, "sandbox": True,
           "cookies": "seed=1; other=2"}
    harness = """
globalThis.window = globalThis;
globalThis.location = {origin: "null", host: "127.0.0.1:3000", protocol: "http:",
  href: "http://127.0.0.1:3000/", pathname: "/", search: "", hash: ""};
globalThis.origin = "null";
globalThis.Document = function () {};
Object.defineProperty(Document.prototype, "cookie", {configurable: true,
  get() { throw new Error("SecurityError"); },
  set() { throw new Error("SecurityError"); }});
globalThis.document = Object.create(Document.prototype);
document.currentScript = {dataset: {cfg: CFG}};
document.baseURI = "http://127.0.0.1:3000/";
document.addEventListener = function () {};
globalThis.addEventListener = function () {};
globalThis.history = {};
SHIM
const out = [document.cookie];
document.cookie = "a=b; Path=/x; Domain=.example.net; Secure; SameSite=None";
out.push(document.cookie);
document.cookie = "seed=3";
out.push(document.cookie);
document.cookie = "other=; Max-Age=0";
out.push(document.cookie);
document.cookie = "a=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
out.push(document.cookie);
console.log(JSON.stringify(out));
"""
    f = tmp_path / "cookie.js"
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    got = subprocess.run([node, str(f)], check=True, capture_output=True, text=True)
    assert json.loads(got.stdout) == [
        "seed=1; other=2",
        "seed=1; other=2; a=b",
        "seed=3; other=2; a=b",
        "seed=3; a=b",
        "seed=3",
    ]


def test_shim_never_zooms_the_page_it_sits_on():
    # The pane scales its own iframe; nothing scales the document from inside
    # it. CSS zoom on a document is not a scale a page can be positioned
    # against: getBoundingClientRect comes back with the factor already in it
    # and a length written from that rect has the factor applied again, so
    # every overlay a script places from a rect lands at factor times where it
    # meant to. That is what closed the MPG login page's institute list — the
    # dropdown opened on top of the control it hung from, and the mouseup that
    # ended the opening click landed in the list and dismissed it.
    assert "zoom" not in A.BROWSE_SHIM.lower()


def _shim_top_harness(tmp_path, cfg, chain: str):
    """Run the shim under a stub window and report what __pt came out as."""
    harness = """
globalThis.window = globalThis;
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:",
  href: "http://box.example.net:8080/", pathname: "/", search: "", hash: ""};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: "/",
  addEventListener() {}};
globalThis.addEventListener = function () {};
globalThis.history = {};
CHAIN
SHIM
console.log(JSON.stringify({top: window.__pt.top.NAME || "self",
                            parent: window.__pt.parent.NAME || "self"}));
"""
    f = tmp_path / "top.mjs"
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("CHAIN", chain)
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([shutil.which("node"), str(f)], check=True,
                         capture_output=True)
    return json.loads(out.stdout.decode("utf-8"))


TAB_CFG = {"prefix": PREFIX, "tok": TOK, "sch": "h", "hostport": "127.0.0.1:3000",
           "origin": ORIGIN, "sandbox": False}


def test_shim_gives_a_framed_page_itself_as_top_and_parent(tmp_path):
    # The shell above is another origin, so reading its location throws — which
    # is how the walk knows where the chain ends. A page whose parent is the
    # shell is then its own top and its own parent, exactly what it would be in
    # a window the user opened themselves.
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    shell = """
const shell = {NAME: "shell"};
Object.defineProperty(shell, "location", {get() { throw new Error("cross"); }});
shell.parent = shell;
window.parent = shell;
window.NAME = "page";
"""
    assert _shim_top_harness(tmp_path, TAB_CFG, shell) == {"top": "page",
                                                           "parent": "page"}


def test_shim_walks_a_readable_chain_up_to_its_outermost(tmp_path):
    # A view nested inside the page: its parent is readable and the framework
    # page above it is what both names have to mean.
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    chain = """
const shell = {NAME: "shell"};
Object.defineProperty(shell, "location", {get() { throw new Error("cross"); }});
shell.parent = shell;
const framework = {NAME: "framework", location: {href: "x"}, parent: shell};
window.parent = framework;
window.NAME = "view";
"""
    assert _shim_top_harness(tmp_path, TAB_CFG, chain) == {"top": "framework",
                                                           "parent": "framework"}


def test_shim_leaves_the_panes_own_pages_without_the_pair(tmp_path):
    # The sandboxed flavour is untouched: no __pt, no eval patch, nothing.
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    cfg = dict(TAB_CFG, sandbox=True)
    harness = """
globalThis.window = globalThis;
globalThis.location = {origin: "http://box.example.net:8080",
  host: "box.example.net:8080", protocol: "http:",
  href: "http://box.example.net:8080/", pathname: "/", search: "", hash: ""};
globalThis.document = {currentScript: {dataset: {cfg: CFG}}, baseURI: "/",
  addEventListener() {}};
globalThis.addEventListener = function () {};
globalThis.history = {};
window.parent = window;
SHIM
console.log(JSON.stringify({pt: window.__pt === undefined}));
"""
    f = tmp_path / "nopt.mjs"
    f.write_text(harness.replace("CFG", json.dumps(json.dumps(cfg)))
                 .replace("SHIM", A.BROWSE_SHIM), encoding="utf-8")
    out = subprocess.run([shutil.which("node"), str(f)], check=True,
                         capture_output=True)
    assert json.loads(out.stdout.decode("utf-8")) == {"pt": True}


def test_shim_cannot_close_its_own_script_element():
    assert "</script" not in A.BROWSE_SHIM.lower()


def test_shim_tag_config_round_trips():
    tag = A.browse_shim_tag(CTX, ORIGIN)
    raw = re.search(r"data-cfg='([^']*)'", tag).group(1)
    assert json.loads(html.unescape(raw)) == {
        "prefix": PREFIX, "tok": TOK, "sch": "h",
        "hostport": "127.0.0.1:3000", "origin": ORIGIN, "sandbox": True,
        "cookies": ""}


def test_shim_tag_config_says_which_kind_of_document_it_is_in():
    """The tab flavour's page is on this server's real origin, not an opaque
    one, and what the shim may leave to the browser turns on that."""
    ctx = A.BrowseCtx(PREFIX, TOK, "h", "127.0.0.1:3000", ORIGIN, False)
    raw = re.search(r"data-cfg='([^']*)'", A.browse_shim_tag(ctx, ORIGIN)).group(1)
    assert json.loads(html.unescape(raw))["sandbox"] is False


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
