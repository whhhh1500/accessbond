"""Pure-Python tests of the deterministic checker (no GenVM needed).

The contract module is imported with a tiny stand-in for the `genlayer` SDK so the
HTML scanner, the document merge and the scoring maths can be tested directly."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures"


class _Any:
    def __getattr__(self, name):
        return _Any()

    def __call__(self, *a, **k):
        return a[0] if len(a) == 1 and callable(a[0]) and not k else _Any()

    def __getitem__(self, item):
        return _Any()

    def __mro_entries__(self, bases):
        return (object,)


@pytest.fixture(scope="module")
def ab():
    stub = types.ModuleType("genlayer")
    stub.gl = _Any()
    stub.u256 = int
    stub.TreeMap = _Any()
    stub.Address = str
    stub.allow_storage = lambda c: c
    stub.__all__ = ["gl", "u256", "TreeMap", "Address", "allow_storage"]
    sys.modules["genlayer"] = stub
    spec = importlib.util.spec_from_file_location("accessbond_contract", ROOT / "contracts" / "accessbond.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("genlayer", None)


def page(name):
    return (FIX / f"{name}.html").read_text()


def passing(report):
    return {k for k, v in report.items() if v["pass"]}


def test_broken_fails_everything(ab):
    assert passing(ab.scan_html(page("broken"))) == set()


def test_fixed_passes_everything(ab):
    assert passing(ab.scan_html(page("fixed"))) == set(ab.CHECK_CATALOG)


def test_partial(ab):
    assert passing(ab.scan_html(page("partial"))) == {"html-lang", "doc-title", "img-alt"}


@pytest.mark.parametrize(
    "snippet,check,ok",
    [
        ('<img src="x.png" alt="">', "img-alt", True),  # decorative image with empty alt is fine
        ('<img src="x.png" aria-hidden="true">', "img-alt", True),
        ('<img src="x.png">', "img-alt", False),
        ('<input id="q"><label for="q">Search</label>', "form-labels", True),
        ('<input aria-label="Search">', "form-labels", True),
        ('<label>Q <input></label>', "form-labels", True),
        ("<input>", "form-labels", False),
        ('<input type="hidden"><input type="submit" value="Go">', "form-labels", True),
        ('<a href="/x"><img src="i.svg" alt="Home"></a>', "link-names", True),
        ('<a href="/x" aria-label="Home"></a>', "link-names", True),
        ('<a href="/x"> </a>', "link-names", False),
        ('<a name="anchor"></a>', "link-names", True),  # not a link without href
        ("<button>OK</button>", "button-names", True),
        ('<button aria-label="Close"></button>', "button-names", True),
        ('<button><svg></svg></button>', "button-names", False),
        ("<h1>a</h1><h2>b</h2><h3>c</h3><h2>d</h2>", "heading-order", True),
        ("<h1>a</h1><h3>b</h3>", "heading-order", False),
        ("<h2>a</h2>", "heading-order", False),
        ("<h1>a</h1><h1>b</h1>", "heading-order", False),
    ],
)
def test_single_checks(ab, snippet, check, ok):
    doc = f'<html lang="en"><head><title>t</title></head><body><h1>x</h1>{snippet}</body></html>'
    if check == "heading-order":
        doc = f'<html lang="en"><head><title>t</title></head><body>{snippet}</body></html>'
    assert ab.scan_html(doc)[check]["pass"] is ok


@pytest.mark.parametrize(
    "viewport,ok",
    [
        ("width=device-width, initial-scale=1", True),
        ("width=device-width, maximum-scale=5", True),
        ("width=device-width, maximum-scale=1", False),
        ("width=device-width, user-scalable=no", False),
        ("width=device-width, user-scalable=0", False),
    ],
)
def test_zoom(ab, viewport, ok):
    doc = f'<html><head><meta name="viewport" content="{viewport}"></head><body></body></html>'
    assert ab.scan_html(doc)["zoom-allowed"]["pass"] is ok


def test_script_and_style_text_is_ignored(ab):
    doc = '<html><head><title><script>x</script></title></head><body><a href="/"><script>t()</script></a></body></html>'
    r = ab.scan_html(doc)
    assert r["link-names"]["pass"] is False


def test_merge_uses_head_from_get_and_body_from_render(ab):
    raw = page("fixed")
    rendered_body = "<h1>Only</h1><img src='a.png'>"  # what web.render(mode="html") returns
    merged = ab.merge_document(raw, rendered_body)
    r = ab.scan_html(merged)
    assert r["html-lang"]["pass"] and r["doc-title"]["pass"] and r["zoom-allowed"]["pass"]
    assert r["img-alt"]["detail"] == "1/1 images without alt"  # body comes from the renderer
    assert "Place order" not in merged


def test_merge_accepts_full_document_from_renderer(ab):
    merged = ab.merge_document(page("broken"), page("fixed"))
    assert merged.lower().count("<body") == 1
    r = ab.scan_html(merged)
    assert r["img-alt"]["pass"] and not r["html-lang"]["pass"]  # head stays from the served doc


def test_merge_fallbacks(ab):
    assert ab.merge_document("", "<p>x</p>") == "<p>x</p>"
    raw = page("broken")
    assert ab.merge_document(raw, "  ") == raw


def test_hunter_meta(ab):
    doc = '<html><head><meta name="accessbond:hunter" content="0xAbC0000000000000000000000000000000000001"></head></html>'
    assert ab.page_hunter(doc) == "0xabc0000000000000000000000000000000000001"
    assert ab.page_hunter(page("fixed")) == ""


def _rep(checks, custom=()):
    return {"checks": {k: {"pass": v} for k, v in checks.items()}, "custom": [{"pass": c} for c in custom]}


def test_score(ab):
    base = _rep({"a": False, "b": False, "c": True}, [False])
    assert ab.score(base, _rep({"a": True, "b": False, "c": True}, [True])) == (3, 2, 0)
    assert ab.score(base, _rep({"a": True, "b": False, "c": False}, [False])) == (3, 1, 1)
    assert ab.flags(base) == [False, False, True, False]


def test_custom_results_parsing(ab):
    raw = {"results": [{"id": 2, "pass": True, "reason": "ok"}, {"id": 1, "pass": False, "reason": "no"}]}
    out = ab.parse_custom_results(raw, 2)
    assert [r["pass"] for r in out] == [False, True]
    with pytest.raises(Exception):
        ab.parse_custom_results({"results": []}, 1)
    with pytest.raises(Exception):  # verdicts must be real booleans
        ab.parse_custom_results({"results": [{"id": 1, "pass": "yes"}]}, 1)
    fenced = '```json\n{"results":[{"id":1,"pass":true,"reason":"r"}]}\n```'
    assert ab.parse_custom_results(fenced, 1)[0]["pass"] is True


def test_compact_html_strips_noise_and_truncates(ab):
    big = "<html><head><style>x{}</style><script>evil()</script></head><body>" + "<p>a</p>" * 5000 + "</body></html>"
    out = ab.compact_html(big)
    assert "evil" not in out and len(out) <= ab.MAX_LLM_HTML + 50
