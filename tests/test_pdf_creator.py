from reportlab.platypus.doctemplate import LayoutError

from backend import pdf_creator
from backend.pdf_creator import remove_anchor_tags, fix_unbalanced_tags, sanitize_pdf_text
from backend.word_creator import sanitize_word_text


def test_sanitize_pdf_text():
    """Test that sanitize_pdf_text removes NULL bytes, control characters, and normalizes Unicode."""
    
    # Test string with NULL bytes and various control characters
    test_text = (
        "Normal text here\x00"  # NULL byte
        "\x01\x02\x03"  # Control chars 1-3
        "More text\x08"  # Backspace
        "\x0B\x0C"  # Vertical tab, form feed
        "\x0E\x0F\x10"  # Shift out, shift in, DLE
        "\x1F"  # Unit separator
        "\t\n\r"  # Tab, line feed, carriage return (should be kept)
        "End text"
    )
    
    result = sanitize_pdf_text(test_text)
    
    # Should contain only printable chars and \t, \n, \r (and \r\n normalized to \n)
    expected = (
        "Normal text here"
        "More text"
        "\t\n\n"  # Tab, line feed, carriage return normalized to \n
        "End text"
    )
    
    print(f"\nTest 1 - Control characters and NULL bytes:")
    print(f"  IN:  {test_text!r}")
    print(f"  OUT: {result!r}")
    assert result == expected, f"Expected '{expected!r}', got '{result!r}'"
    
    # Test that NULL bytes are removed
    text_with_nulls = "Hello\x00World\x00Test"
    result_nulls = sanitize_pdf_text(text_with_nulls)
    print(f"\nTest 2 - NULL bytes removal:")
    print(f"  IN:  {text_with_nulls!r}")
    print(f"  OUT: {result_nulls!r}")
    assert result_nulls == "HelloWorldTest", f"NULL bytes not removed: {result_nulls!r}"
    
    # Test that normal text is unchanged (except line break normalization)
    normal_text = "This is normal text with numbers 123 and symbols !@#$%"
    result_normal = sanitize_pdf_text(normal_text)
    print(f"\nTest 3 - Normal text (should be unchanged):")
    print(f"  IN:  {normal_text!r}")
    print(f"  OUT: {result_normal!r}")
    assert result_normal == normal_text, f"Normal text was modified: {result_normal!r}"
    
    # Test empty string
    empty_text = ""
    result_empty = sanitize_pdf_text(empty_text)
    print(f"\nTest 4 - Empty string:")
    print(f"  IN:  {empty_text!r}")
    print(f"  OUT: {result_empty!r}")
    assert result_empty == ""
    
    # Test None-like (empty after strip)
    control_only = "\x00\x01\x02"
    result_control = sanitize_pdf_text(control_only)
    print(f"\nTest 5 - Control characters only:")
    print(f"  IN:  {control_only!r}")
    print(f"  OUT: {result_control!r}")
    assert result_control == ""
    
    # Test line break normalization (\r\n and \r should become \n)
    text_with_crlf = "Line 1\r\nLine 2\rLine 3\nLine 4"
    result_crlf = sanitize_pdf_text(text_with_crlf)
    print(f"\nTest 6 - Line break normalization:")
    print(f"  IN:  {text_with_crlf!r}")
    print(f"  OUT: {result_crlf!r}")
    assert result_crlf == "Line 1\nLine 2\nLine 3\nLine 4", f"Line breaks not normalized: {result_crlf!r}"
    
    # Test Unicode normalization (NFC)
    # Using combining characters that should be normalized
    unicode_text = "caf\u00e9"  # é as single character
    result_unicode = sanitize_pdf_text(unicode_text)
    print(f"\nTest 7 - Unicode normalization:")
    print(f"  IN:  {unicode_text!r}")
    print(f"  OUT: {result_unicode!r}")
    assert result_unicode == "café", f"Unicode not normalized: {result_unicode!r}"
    
    # Test special character translation (em dash, en dash, etc.)
    # These should be translated to regular hyphens
    text_with_dashes = "Range 2010–2014"  # En dash
    result_dashes = sanitize_pdf_text(text_with_dashes)
    print(f"\nTest 8 - Special character translation (en dash):")
    print(f"  IN:  {text_with_dashes!r}")
    print(f"  OUT: {result_dashes!r}")
    # The en dash (U+2013) should be translated to regular hyphen
    assert "–" not in result_dashes or "-" in result_dashes, f"Dashes not translated: {result_dashes!r}"
    
    # Test that tabs and newlines are preserved (before line break normalization)
    text_with_whitespace = "Line 1\nLine 2\tTabbed"
    result_whitespace = sanitize_pdf_text(text_with_whitespace)
    print(f"\nTest 9 - Whitespace preservation:")
    print(f"  IN:  {text_with_whitespace!r}")
    print(f"  OUT: {result_whitespace!r}")
    assert "\n" in result_whitespace and "\t" in result_whitespace, f"Whitespace was removed: {result_whitespace!r}"
    
    print("\n✓ All sanitize_pdf_text tests passed!")


def test_sanitize_word_text():
    """Test that sanitize_word_text removes NULL bytes and non-XML-compatible control characters."""
    
    # Test string with NULL bytes and various control characters
    # Control characters that should be removed: \x00 (NULL), \x01-\x08, \x0B-\x0C, \x0E-\x1F
    # Control characters that should be kept: \x09 (tab), \x0A (line feed), \x0D (carriage return)
    test_text = (
        "Normal text here\x00"  # NULL byte
        "\x01\x02\x03"  # Control chars 1-3
        "More text\x08"  # Backspace
        "\x0B\x0C"  # Vertical tab, form feed
        "\x0E\x0F\x10"  # Shift out, shift in, DLE
        "\x1F"  # Unit separator
        "\t\n\r"  # Tab, line feed, carriage return (should be kept)
        "End text"
    )
    
    result = sanitize_word_text(test_text)
    
    # Should contain only printable chars and \t, \n, \r
    expected = (
        "Normal text here"
        "More text"
        "\t\n\r"  # These should be preserved
        "End text"
    )
    
    print(f"\nTest 1 - Control characters and NULL bytes:")
    print(f"  IN:  {test_text!r}")
    print(f"  OUT: {result!r}")
    assert result == expected, f"Expected '{expected!r}', got '{result!r}'"
    
    # Test that NULL bytes are removed
    text_with_nulls = "Hello\x00World\x00Test"
    result_nulls = sanitize_word_text(text_with_nulls)
    print(f"\nTest 2 - NULL bytes removal:")
    print(f"  IN:  {text_with_nulls!r}")
    print(f"  OUT: {result_nulls!r}")
    assert result_nulls == "HelloWorldTest", f"NULL bytes not removed: {result_nulls!r}"
    
    # Test that normal text is unchanged
    normal_text = "This is normal text with numbers 123 and symbols !@#$%"
    result_normal = sanitize_word_text(normal_text)
    print(f"\nTest 3 - Normal text (should be unchanged):")
    print(f"  IN:  {normal_text!r}")
    print(f"  OUT: {result_normal!r}")
    assert result_normal == normal_text, f"Normal text was modified: {result_normal!r}"
    
    # Test empty string
    empty_text = ""
    result_empty = sanitize_word_text(empty_text)
    print(f"\nTest 4 - Empty string:")
    print(f"  IN:  {empty_text!r}")
    print(f"  OUT: {result_empty!r}")
    assert result_empty == ""
    
    # Test None-like (empty after strip)
    control_only = "\x00\x01\x02"
    result_control = sanitize_word_text(control_only)
    print(f"\nTest 5 - Control characters only:")
    print(f"  IN:  {control_only!r}")
    print(f"  OUT: {result_control!r}")
    assert result_control == ""
    
    # Test with newlines and tabs (should be preserved)
    text_with_whitespace = "Line 1\nLine 2\tTabbed\rCarriage return"
    result_whitespace = sanitize_word_text(text_with_whitespace)
    print(f"\nTest 6 - Whitespace preservation:")
    print(f"  IN:  {text_with_whitespace!r}")
    print(f"  OUT: {result_whitespace!r}")
    assert result_whitespace == text_with_whitespace, f"Whitespace was modified: {result_whitespace!r}"
    
    print("\n✓ All sanitize_word_text tests passed!")


def test_remove_anchor_tags():
    text = '''Under <a href="https://www.law.cornell.edu/uscode/text/17/411" target="_blank" rel="noopener noreferrer">17 U.S.C. § 411</a>(a)'''
    text = '''<para>31. HomeLight\'s false advertising claim under 15 U<a href="https://www.law.cornell.edu/uscode/text/15/1125" target="_blank" rel="noopener noreferrer">.S.C. \xa7 1125(</a>a) should be dismissed due to its failure to allege a substantive causal connection between HomeOpenly\'s purported false advertising and any actual injury to HomeLight. To establish such a claim, a plaintiff must demonstrate both causation and actual harm resulting from the alleged false advertisements. Here, HomeLight merely asserts potential losses of business and goodwill without specific allegations linking this purported loss to HomeOpenly\'s conduct (Compl. \xb6 72).</para>'''
    
    text = '<para>10. HomeLight, Inc. has failed to substantiate its claim of federal trademark infringement unde<a href="https://www.law.cornell.edu/uscode/text/15/1114" target="_blank" rel="noopener noreferrer">r 15 U.S.C. \xa7 1114</a>, particularly regarding the distinctiveness of its logo and the alleged secondary meaning. According to the Complaint, HomeLight contends that its logo is either inherently distinctive or has acquired secondary meaning in the marketplace (Compl. \xb6 52-53). However, the Complaint falls short in providing the necessary factual details to support these assertions.</para>'
    
    text = '''<a href="/CaseViewer/676-F<dot>3d-at-38&626721" target="_blank" rel="noopener noreferrer">676 F.3d at 38,</a> '''
    
    clean_text = remove_anchor_tags(text)
    print(clean_text)


def test_fix_unbalanced_tags():
    text = '''<para>LLC v. Marcus & Millichap Co.</i>, 751 F.3d 990, 995 (9th Cir. 2014).</para>' caused exception Parse error: saw </i> instead of expected </para>'''
    print(fix_unbalanced_tags(text))


def test_create_answer_pdf_splits_long_cover_party_caption(tmp_path):
    defendant = ";".join(
        f"Very Long Healthcare Defendant Entity Number {index} Incorporated"
        for index in range(1, 90)
    )
    fn = tmp_path / "answer-long-caption.pdf"

    result = pdf_creator.create_answer_pdf(
        fn=str(fn),
        plaintiff="State of Example",
        defendant=defendant,
        attorneys_contact_info="Test Counsel",
        venue="United States District Court\nEastern District of Pennsylvania",
        introduction="",
        responses="Defendant denies the allegations in Paragraph 1.",
        affirmative_defenses="",
        counter_claims="",
    )

    assert result == str(fn)
    assert fn.exists()
    assert fn.stat().st_size > 0


def test_create_answer_pdf_retries_with_caption_placeholder_on_layout_error(monkeypatch, tmp_path):
    calls = []
    fn = tmp_path / "answer-fallback.pdf"

    def fake_build_answer_pdf_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise LayoutError("Flowable <CoverPageLeftText> too large")
        fn.write_bytes(b"%PDF-1.4\n%fallback\n")
        return kwargs["fn"]

    monkeypatch.setattr(pdf_creator, "_build_answer_pdf_once", fake_build_answer_pdf_once)

    result = pdf_creator.create_answer_pdf(
        fn=str(fn),
        plaintiff="State of Example",
        defendant=";".join(f"Defendant {index}" for index in range(1, 90)),
        attorneys_contact_info="Test Counsel",
        venue="United States District Court",
        introduction="",
        responses="Defendant denies the allegations in Paragraph 1.",
        affirmative_defenses="",
        counter_claims="",
    )

    assert result == str(fn)
    assert calls[0]["split_long_party_caption"] is True
    assert calls[0]["skip_long_party_caption"] is False
    assert calls[1]["split_long_party_caption"] is True
    assert calls[1]["skip_long_party_caption"] is True
    assert fn.read_bytes().startswith(b"%PDF-1.4")


# test_sanitize_pdf_text()
# test_sanitize_word_text()
# test_remove_anchor_tags()
# test_fix_unbalanced_tags()
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_RIGHT
from reportlab.pdfbase.pdfmetrics import stringWidth
import io

def make_dot_leader_line(title: str,
                         page_nums: str,
                         font_name: str,
                         font_size: float,
                         available_width: float) -> str:
    """
    Returns a string with dot-leaders and flush-right page numbers.
    If title and page numbers fit on one line, returns "title\tpage_nums".
    Otherwise, splits page numbers into minimal extra lines,
    grouping contiguous numbers until width exceeded, each line indented with a tab.
    """
    # measure widths
    title_w = stringWidth(title, font_name, font_size)
    pages_list = [p.strip() for p in page_nums.split(",")]
    # try single line
    joined = ", ".join(pages_list)
    total_w = title_w + stringWidth(joined, font_name, font_size) + stringWidth("\t", font_name, font_size)
    if total_w <= available_width:
        return f"{title}\t{joined}"

    # otherwise, split pages into lines
    lines = []
    current = []
    for p in pages_list:
        candidate = ", ".join(current + [p])
        w = title_w + stringWidth(candidate, font_name, font_size) + stringWidth("\t", font_name, font_size) if not current else stringWidth(candidate, font_name, font_size) + stringWidth("\t", font_name, font_size)
        # for first extra line, ignore title width
        if not current or w <= available_width:
            current.append(p)
        else:
            lines.append(", ".join(current))
            current = [p]
    if current:
        lines.append(", ".join(current))

    # build multi-line string
    result = [title]
    for ln in lines:
        result.append(f"\t{ln}")
    return "\n".join(result)

import io, math, textwrap
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.pagesizes import letter
from reportlab.lib.units    import inch
from reportlab.platypus     import BaseDocTemplate, Frame, PageTemplate, Spacer, Flowable, Paragraph
from reportlab.lib.styles   import ParagraphStyle

FONT, SIZE, LEAD, DOT = 'Times-Roman', 12, 14, '.'

class IndexLine(Flowable):
    def __init__(self, title, pages, width):
        super().__init__()
        self.title, self.pages, self.width = title, pages, width
        self.lines = self._wrap_title()
        self.height = LEAD * len(self.lines)

    # ---------- helpers ----------------------------------------------------
    def _str_w(self, txt):
        return stringWidth(txt, FONT, SIZE)

    def _wrap_title(self):
        """Return a list of lines for the left-hand text."""
        words = self.title.split()
        lines, cur = [], []
        max_left = self.width - self._str_w(self.pages) - 6 * self._str_w(DOT)

        for w in words:
            if self._str_w(' '.join(cur + [w])) <= max_left:
                cur.append(w)
            else:
                lines.append(' '.join(cur))
                cur = [w]
        lines.append(' '.join(cur))
        return lines

    # ---------- Platypus API ----------------------------------------------
    def wrap(self, availW, availH):
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.setFont(FONT, SIZE)
        y = (len(self.lines) - 1) * LEAD

        # draw all but the last wrapped line (no dots yet)
        for line in self.lines[:-1]:
            c.drawString(0, y, line)
            y -= LEAD

        # final line: dotted leader + page number
        last = self.lines[-1]
        w_left  = self._str_w(last)
        w_right = self._str_w(self.pages)
        w_dot   = self._str_w(DOT)
        gap     = max(self.width - w_left - w_right, 0)
        dots    = DOT * int(math.floor(gap / w_dot - .5))

        c.drawString(0, y, last)
        c.drawString(w_left, y, dots)
        c.drawRightString(self.width, y, self.pages)

# ----------------------- demo driver --------------------------------------
def build_toa(entries):
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=letter,
                          leftMargin=72, rightMargin=72,
                          topMargin=72,  bottomMargin=72)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='F')
    doc.addPageTemplates(PageTemplate(id='P', frames=[frame]))

    hdr = Paragraph("<b>Table of Authorities</b>",
                    ParagraphStyle('hdr', fontName=FONT, fontSize=14, leading=18))
    story = [hdr, Spacer(1, 18)]

    for title, pages in entries:
        story.append(IndexLine(title, pages, doc.width))
        story.append(Spacer(1, 6))

    doc.build(story)
    with open("toa_wrap_custom.pdf", "wb") as f:
        f.write(buf.getvalue())
    print("→ wrote toa_wrap_custom.pdf")

if __name__ == "__main__":
    demo = [
        ("In re VeryVeryVeryLongCitationNameWithMultipleParties and Several Appellants "
         "v. A Whole Bunch of Appellees et al.", "123 – 145"),
        ("Short cite", "9"),
    ]
    build_toa(demo)

    test_sanitize_word_text()
    test_sanitize_pdf_text()
