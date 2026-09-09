"""The report PDF renders even when the model's Markdown is awkward.

A real report 500'd because the model wrote snake_case identifiers and mixed
emphasis (has_rule_or_require, r_auth, stray * and `) that the old inline
converter turned into badly-nested HTML, crashing ReportLab's strict parser.
The renderer must now produce a PDF regardless, falling back to plain text for
any fragment it cannot mark up.
"""

from types import SimpleNamespace

from app.services.report_pdf import render_report_pdf


def _report(**kw):
    kw.setdefault("summary_md", "")
    kw.setdefault("solutions_md", "")
    kw.setdefault("priorities_md", "")
    kw.setdefault("sections", [])
    return SimpleNamespace(**kw)


def _section(issues_md="", solutions_md=""):
    return SimpleNamespace(
        filename="Sec-U3b_Secure.pdf",
        issues_md=issues_md,
        solutions_md=solutions_md,
    )


def test_renders_with_snake_case_and_mixed_markup():
    # The exact shape that used to crash: underscores read as italics colliding
    # with a <font> (code) span, plus a dangling asterisk.
    nasty = (
        "The engine cites `has_rule_or_require = True` for r_auth on page 25. "
        "While *should* is not a rule, the _intent_ was flagged here_ and there."
    )
    course = SimpleNamespace(title="Security_Engineering *2026*")
    report = _report(
        summary_md=nasty,
        solutions_md="- Rename r_auth\n- Clarify the *should* wording",
        sections=[_section(issues_md=nasty, solutions_md="1. fix has_x\n2. done")],
    )
    pdf = render_report_pdf(course, report)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_renders_empty_report():
    pdf = render_report_pdf(SimpleNamespace(title=None), _report())
    assert pdf.startswith(b"%PDF")


def test_renders_priorities_and_checklist():
    report = _report(
        priorities_md="- **a.pdf** [high · structural] — Add reasoning questions",
    )
    pdf = render_report_pdf(
        SimpleNamespace(title="C"),
        report,
        checklist=["[a.pdf] Add reasoning questions", "[b.pdf] Fix the typo"],
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_includes_diagnostics_when_metrics_present():
    """The PDF leads with the headline scores so it opens with where the course
    stands (the same numbers as the web gauges)."""
    import json

    import pymupdf

    metrics = {
        "health_score": 72, "passed": 18, "checked": 25,
        "ai_readiness_score": 60, "high_severity_failures": 4,
    }
    report = _report(metrics_json=json.dumps(metrics))
    pdf = render_report_pdf(SimpleNamespace(title="Course X"), report)
    text = "".join(p.get_text() for p in pymupdf.open(stream=pdf, filetype="pdf"))
    assert "Diagnostics" in text
    assert "72/100" in text
    assert "AI-readiness" in text and "60/100" in text
    # Bold must render as bold, never leak literal markup into the printed page.
    assert "<b>" not in text and "**" not in text


def test_markdown_never_renders_raw_html():
    """A saved suggestion is rendered unescaped into the page, so it must not be
    able to carry markup.

    render_markdown wraps its result in Markup (no escaping downstream), and its
    inputs are the model's output and the professor's own edits. Neither is
    trusted HTML: saving "<script>…</script>" into an editable block used to
    store a script that ran on every later view of that report.
    """
    from app.templating import render_markdown

    hostile = [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<iframe src='//evil'></iframe>",
        "<a href='javascript:alert(1)'>click</a>",
        "<svg/onload=alert(1)>",
    ]
    for payload in hostile:
        out = str(render_markdown(payload))
        assert "<script" not in out.lower(), payload
        assert "<img" not in out.lower(), payload
        assert "<iframe" not in out.lower(), payload
        assert "onerror" not in out.lower() or "&lt;" in out, payload
        # The text itself survives, escaped, so nothing the professor wrote is lost.
        assert "&lt;" in out, payload


def test_markdown_still_formats_normally():
    """Escaping HTML must not cost the Markdown the reports actually use."""
    from app.templating import render_markdown

    out = str(render_markdown("**bold** and *italic*\n\n- one\n- two"))
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert out.count("<li>") == 2
    # An ampersand in ordinary prose reads as itself.
    assert "Grading &amp; feedback" in str(render_markdown("Grading & feedback"))


def test_empty_list_items_are_dropped():
    """A stray Enter in the editor leaves a marker with no text behind it.

    Rendered as-is it shows a bullet floating with nothing beside it (and, when
    the emphasis markers survive alone, a literal "****").
    """
    from app.templating import render_markdown

    out = str(render_markdown("- \n- Real item."))
    assert out.count("<li>") == 1
    assert "Real item." in out
    for junk in ("- \n- **\n- Real.", "1. \n2. Real.", "- ****\n- Real."):
        rendered = str(render_markdown(junk))
        assert "Real" in rendered, junk
        assert "<li></li>" not in rendered.replace("\n", ""), junk


def test_pdf_checklist_lines_are_shortened_at_a_word_boundary():
    """The checklist is scanned down a page, so it keeps the code and the opening
    of the rule rather than repeating the full sentence printed above it."""
    from app.services.report_pdf import _checklist_line

    short = "[a7] Each assessment must contain learning outcomes."
    assert _checklist_line(short) == short  # already scannable, untouched

    long = "[a2] " + "word " * 60
    out = _checklist_line(long)
    assert len(out) <= 121
    assert out.startswith("[a2]")
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")  # cut at a word, no dangling space


def test_markdown_escapes_survive_the_latex_stripper():
    """The editor escapes block markers so typed prose is not re-parsed.

    Those escapes are Markdown's, not LaTeX's, but "\\#" and "\\%" are also
    LaTeX commands: the maths stripper used to turn "\\#" back into "#", handing
    it to the parser as a heading and undoing the escape. That is what put a
    stray horizontal rule and an extra bullet into a suggestion block.
    """
    from app.templating import render_markdown, strip_latex

    assert "<h1" not in str(render_markdown(r"\# not a heading"))
    assert "<hr" not in str(render_markdown(r"\-\-\-"))
    assert "<li>" not in str(render_markdown(r"\* not a bullet"))
    # A real bullet is still a bullet, and escaped text inside it reads literally.
    out = str(render_markdown("- Real advice.\n- \\- looks nested"))
    assert out.count("<li>") == 2
    assert "- looks nested" in out
    # Genuine LaTeX is still flattened for the reader.
    assert "×" in strip_latex(r"Energy: $60 \times 0.20$ kg")


def test_markdown_editor_round_trip_is_stable():
    """Saving a block the model wrote, unchanged, must store it unchanged.

    The editor serialises the rendered HTML back to Markdown, so a save with no
    edits is a full render -> serialise cycle. The escaping that keeps typed
    prose literal must not touch the serialiser's OWN markers: escaping the
    finished line broke the "**" around a bold lead-in, so "**[sy1] …:**" was
    stored as "\\**[sy1] …:**" and displayed as literal asterisks.

    This pins the Python half: that the Markdown those cases produce renders
    back to the same HTML the editor started from. The JS serialiser itself is
    not covered by this suite.
    """
    from app.templating import render_markdown

    pairs = [
        ("- **[sy1] Incorporate objectives:** Update the outcomes.",
         "<strong>[sy1] Incorporate objectives:</strong>"),
        ("1. **First:** do it\n2. Then this", "<ol>"),
        ("Consider **this** carefully.", "<strong>this</strong>"),
    ]
    for md, expected in pairs:
        assert expected in str(render_markdown(md)), md
    # Text the professor typed stays literal, and does not become structure:
    # the asterisk shows AS an asterisk rather than turning into emphasis.
    assert "<h1" not in str(render_markdown(r"\#sex -fdf"))
    assert str(render_markdown(r"2 \* 3")) == "<p>2 * 3</p>"
    assert "<em>" not in str(render_markdown(r"a \* b \* c"))
