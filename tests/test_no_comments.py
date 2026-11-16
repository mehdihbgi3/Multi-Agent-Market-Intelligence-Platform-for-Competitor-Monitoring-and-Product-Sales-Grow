import io
import re
import tokenize

from conftest import ROOT


def test_python_code_has_no_comments():
    hits = []
    for path in sorted(list(ROOT.glob("borna_pricing/**/*.py")) + list(ROOT.glob("tests/**/*.py"))):
        for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
            if tok.type == tokenize.COMMENT:
                hits.append(f"{path.relative_to(ROOT)}:{tok.start[0]}")
    assert hits == []


def test_templates_styles_and_scripts_have_no_comments():
    hits = []
    for path in sorted(ROOT.glob("borna_pricing/templates/*.html")):
        text = path.read_text(encoding="utf-8")
        if "{#" in text or "<!--" in text:
            hits.append(str(path.relative_to(ROOT)))
    for path in (ROOT / "borna_pricing" / "static" / "style.css", ROOT / "borna_pricing" / "static" / "app.js"):
        if re.search(r"/\*|(?<![:\"'])//", path.read_text(encoding="utf-8")):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
