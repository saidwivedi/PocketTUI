"""The src/mobile -> mobile_app.html assembly.

The app is written split and served whole, so the thing worth pinning is that
assembly is a pure concatenation: deterministic, lossless, and leaving the
placeholders for the consumers that substitute them. Content is deliberately not
hashed — every UI change would move the hash and the test would only ever be
updated to match, which proves nothing.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "mobile"


def _load_build_mobile():
    spec = importlib.util.spec_from_file_location(
        "build_mobile", REPO / "build_mobile.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_mobile"] = mod
    spec.loader.exec_module(mod)
    return mod


build_mobile = _load_build_mobile()


@pytest.fixture(scope="module")
def doc():
    return build_mobile.assemble()


def test_assembly_is_deterministic(doc):
    assert build_mobile.assemble() == doc


def test_every_fragment_is_a_file():
    assert (SRC / "index.src.html").is_file()
    assert (SRC / "boot-theme.js").is_file()
    assert (SRC / "styles.css").is_file()
    for frag in build_mobile.JS_FRAGMENTS:
        assert (SRC / "js" / frag).is_file(), frag


def test_fragment_list_matches_the_directory():
    """A fragment on disk that no one includes is dead code that still looks live."""
    on_disk = {
        str(p.relative_to(SRC / "js"))
        for p in (SRC / "js").rglob("*.js")
    }
    assert on_disk == set(build_mobile.JS_FRAGMENTS)


def test_no_include_markers_survive(doc):
    assert "@include" not in doc


def test_every_fragment_appears_verbatim(doc):
    """Concatenation, not transformation: each fragment's bytes are in the output."""
    for rel in ["boot-theme.js", "styles.css"] + [
            "js/" + f for f in build_mobile.JS_FRAGMENTS]:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text in doc, rel


def test_fragments_appear_in_listed_order(doc):
    positions = [
        doc.index((SRC / "js" / f).read_text(encoding="utf-8"))
        for f in build_mobile.JS_FRAGMENTS
    ]
    assert positions == sorted(positions)


def test_document_structure(doc):
    assert doc.startswith("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
    assert doc.rstrip().endswith("</html>")
    for tag in ("<head>", "</head>", "<body>", "</body>", "<style>", "</style>"):
        assert doc.count(tag) == 1, tag
    # One inline script for the pre-paint theme, one for the app.
    assert doc.count("</script>") == doc.count("<script")


def test_style_block_holds_the_whole_stylesheet(doc):
    css = (SRC / "styles.css").read_text(encoding="utf-8")
    assert css.startswith("<style>\n")
    assert css.rstrip().endswith("</style>")
    head = doc[:doc.index("</head>")]
    assert css in head


def test_app_script_is_one_block(doc):
    """All the js fragments land inside a single <script> element."""
    body = doc[doc.index("<body>"):]
    first = (SRC / "js" / build_mobile.JS_FRAGMENTS[0]).read_text(encoding="utf-8")
    last = (SRC / "js" / build_mobile.JS_FRAGMENTS[-1]).read_text(encoding="utf-8")
    start, end = body.index(first), body.index(last)
    assert start < end
    # Nothing closes the script between the first fragment and the last.
    assert "</script>" not in body[start:end]


def test_placeholders_survive_assembly(doc):
    """app.py and build_mobile.py substitute these on the assembled document."""
    assert "__BACKEND_URL__" in doc
    assert doc.count("__CACHE_VERSION__") >= 2


def test_vendor_script_tags_survive(doc):
    for name in ("xterm.js", "addon-fit.js", "addon-webgl.js"):
        assert f'src="vendor/{name}?v=__CACHE_VERSION__"' in doc
    assert 'href="vendor/xterm.css?v=__CACHE_VERSION__"' in doc


def test_no_fragment_starts_mid_statement():
    """Each cut sits on a section banner, so fragments stay independently readable."""
    banner = re.compile(r"^// ={10,}\n|^// -{4,} ")
    for frag in build_mobile.JS_FRAGMENTS:
        text = (SRC / "js" / frag).read_text(encoding="utf-8")
        assert banner.match(text), frag


def test_emit_runtime_writes_the_flat_set(tmp_path, doc):
    build_mobile.emit_runtime(tmp_path, doc, "// sw")
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"mobile_app.html", "sw.js", "icon-192.png", "icon-512.png"}
    assert (tmp_path / "mobile_app.html").read_text(encoding="utf-8") == doc


def test_root_runtime_copy_is_current(doc):
    """The gitignored root copy is what app.py serves from a checkout."""
    root = REPO / "mobile_app.html"
    if not root.exists():
        pytest.skip("no build has been run in this checkout")
    assert root.read_text(encoding="utf-8") == doc
