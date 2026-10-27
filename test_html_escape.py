"""
Isolated tests for the HTML-escaping change in app.py (_esc, _js_string and
their call sites). Independent of the login work: imports nothing from the
auth modules and needs neither login nor Supabase. No network.

    python3 test_html_escape.py
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import types

import pandas as pd

REPO = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(REPO, "app.py")).read()
TREE = ast.parse(SRC)

EVIL = '<img src=x onerror=alert(1)>'
EVIL2 = '"><script>alert(2)</script>'
EVIL3 = "`;alert(3);//${x}\\"
EVIL4 = "</script><script>alert(4)</script>"


def _ns():
    import html as _html
    names = {"_safe_cell", "_esc", "_js_string", "build_rowspan_rows_html", "big_banner"}
    parts = [ast.get_source_segment(SRC, n) for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(parts) == len(names)
    out = []
    st = types.SimpleNamespace(markdown=lambda html, unsafe_allow_html=False: out.append(html))
    ns = {"pd": pd, "_html": _html, "json": json, "st": st}
    exec("\n\n".join(parts), ns)
    return ns, out


def test_esc_basics():
    ns, _ = _ns()
    e = ns["_esc"]
    assert e(EVIL) == "&lt;img src=x onerror=alert(1)&gt;"
    assert "<" not in e(EVIL2) and '"' not in e(EVIL2)
    assert e(None) == "-" and e(float("nan")) == "-" and e("  ") == "-" and e("nan") == "-"
    assert e("Sandal & Co") == "Sandal &amp; Co" and e(5) == "5"


def test_rowspan_table_is_escaped_and_structure_intact():
    ns, _ = _ns()
    df = pd.DataFrame([{"o": "A", "u": EVIL, "v": EVIL2, "q": 1}, {"o": "A", "u": EVIL, "v": "x", "q": 2}])
    html = ns["build_rowspan_rows_html"](df, "o", ["o", "u"], ["v", "q"], blank_cols=["k"], right_align_cols=["q"])
    assert "<img" not in html and "<script" not in html
    assert html.count("<tr>") == 2 and 'rowspan="2"' in html and "rowspan-qty-cell" in html and "<td></td>" in html
    assert "&lt;img" in html


def test_banner_escapes_lines_but_keeps_static_text_readable():
    ns, out = _ns()
    ns["big_banner"]([f"STATUS: {EVIL}", "Order siap diverifikasi & di-pack"], "#000")
    html = out[0]
    assert "<img" not in html and "&lt;img" in html
    assert "diverifikasi &amp; di-pack" in html              # renders as '&' in the browser


def test_js_string_cannot_break_out_of_script_or_literal():
    ns, _ = _ns()
    js = ns["_js_string"]
    for evil in (EVIL, EVIL2, EVIL3, EVIL4, "a\nb c'\"`\\"):
        lit = js(evil)
        assert "</" not in lit and not re.search(r"(?i)</script", lit)
        assert "\n" not in lit and " " not in lit
        if shutil.which("node"):
            val = subprocess.run(["node", "-e", f"process.stdout.write(JSON.stringify({lit}))"],
                                 capture_output=True, text=True, check=True).stdout
            assert json.loads(val) == evil.replace("</", "</"), (evil, val)


def test_print_scripts_use_js_string_not_backtick_template():
    assert "w.document.write(`" not in SRC
    assert SRC.count("w.document.write({escaped") == 2
    assert SRC.count("_js_string(") == 3                    # def + 2 call sites


def test_no_unescaped_data_in_html_f_strings():
    """Every interpolation of order data in the HTML blocks goes through _esc."""
    for needle in ("{_order_shop}", "{nama_produk}", "{nama_variasi}", "{sku_induk}", "{line}"):
        assert needle not in SRC, f"unescaped {needle}"
    assert not re.search(r"<td[^>]*>\{(row|p)\.get\(", SRC)
    assert "<title>Order {order_number}" not in SRC
    assert "{_safe_cell(r.get(col))}" not in SRC


def test_dataframe_path_unchanged():
    assert SRC.count('"Shop": _safe_cell(_sr.get("Toko"))') == 1      # st.dataframe cells stay unescaped
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_safe_cell")
    assert "escape" not in ast.get_source_segment(SRC, fn)


# The full-app render check (real login + AppTest) lives in test_auth.py as
# test_app_rendered_markdown_has_no_raw_injected_tags, so this file has no
# dependency on the authentication code and can ship with the XSS change alone.

if __name__ == "__main__":
    fails = []
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try:
            f(); print("PASS", n)
        except Exception as e:  # noqa: BLE001
            import traceback; fails.append(n); print("FAIL", n, type(e).__name__, e); traceback.print_exc(limit=3)
    print(f"\n{len(tests)-len(fails)}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
