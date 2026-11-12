
from __future__ import annotations

import logging

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import Paragraph, PageTemplate, BaseDocTemplate, Flowable, PageBreak, NextPageTemplate, Indenter
from reportlab.platypus.doctemplate import LayoutError
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfgen import canvas
from datetime import datetime
from backend.util.data_tools import number_to_letter
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.units import inch
from reportlab.platypus import CondPageBreak

from reportlab.platypus import PageBreak
from typing import List, Tuple

from reportlab.platypus import Frame, Spacer
from reportlab.pdfbase.pdfmetrics import stringWidth

import io, re
from PyPDF2 import PdfReader

import unicodedata

logger = logging.getLogger(__name__)
# Map fancy dashes/spaces to ASCII, drop zero-width chars, etc.
_TRANSLATE = {
    0x2010: 0x002D,  # HYPHEN → '-'
    0x2011: 0x002D,  # NON-BREAKING HYPHEN → '-'
    0x2012: 0x002D,  # FIGURE DASH → '-'
    0x2013: 0x002D,  # EN DASH → '-'
    0x2014: 0x002D,  # EM DASH → '-'
    0x2212: 0x002D,  # MINUS SIGN → '-'

    0x00A0: 0x0020,  # NBSP → space
    0x202F: 0x0020,  # NARROW NBSP → space
    0x2007: 0x0020,  # FIGURE SPACE → space

    0x200B: None,    # ZERO WIDTH SPACE → drop
    0x200C: None,    # ZERO WIDTH NON-JOINER → drop
    0x200D: None,    # ZERO WIDTH JOINER → drop
    0x2060: None,    # WORD JOINER → drop
    0xFEFF: None,    # BOM → drop
}

def sanitize_pdf_text(s: str) -> str:
    """
    Normalize Unicode, translate unsupported glyphs to ASCII-safe ones, and remove NULL bytes/control characters.
    ReportLab can also have issues with NULL bytes and control characters.
    """
    if not s:
        return s
    
    # Remove NULL bytes and XML-incompatible control characters first
    # ReportLab's Paragraph can have issues with these too
    text_cleaned = []
    for char in s:
        code = ord(char)
        # Keep printable characters, tabs, line feeds, and carriage returns
        if code >= 32 or code in (9, 10, 13):
            text_cleaned.append(char)
        # Otherwise, skip the character (it's an invalid control character or NULL byte)
    
    s = ''.join(text_cleaned)
    
    # Then normalize Unicode and translate unsupported glyphs
    s = unicodedata.normalize("NFC", s)
    s = s.translate(_TRANSLATE)
    # normalize line breaks to what your code expects
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s


def _san_sec(sections):
    return [(sanitize_pdf_text(txt), sanitize_pdf_text(title)) for txt, title in sections]


def remove_anchor_tags(text: str) -> str:
    """Removes all <a> tags, preserving inner text and ignoring <dot>-like tokens."""
    # Step 1: temporarily mask <dot> to avoid being treated as tag-like
    text = text.replace('<dot>', '[[DOT]]')

    # Step 2: remove full <a ...>...</a> and keep inner text
    text = re.sub(r'<a\b[^>]*?>(.*?)</a>', r'<ref>\1</ref>', text, flags=re.IGNORECASE | re.DOTALL)

    # Step 3: unmask <dot>
    text = text.replace('[[DOT]]', '.')
    return text

class HorizontalLine(Flowable):
    """
    Draw a horizontal line.
    """
    def __init__(self, width, color):
        Flowable.__init__(self)
        self.width = width
        self.color = color

    def wrap(self, availWidth, availHeight):
        # Ensure the line fits within the current frame width
        if self.width is None or self.width <= 0:
            self.width = availWidth
        else:
            self.width = min(self.width, availWidth)
        self.height = 1
        return self.width, self.height

    def draw(self):
        """
        Draw the line.
        """
        self.canv.setLineWidth(1)
        self.canv.setStrokeColor(self.color)
        self.canv.line(0, 0, self.width, 0)


class VerticalLine(Flowable):
    """
    Draw a vertical line.
    """
    def __init__(self, height, color):
        Flowable.__init__(self)
        self.height = height
        self.color = color

    def draw(self):
        """
        Draw the line.
        """
        self.canv.setLineWidth(1)
        self.canv.setStrokeColor(self.color)
        self.canv.line(0, 0, 0, self.height)

class SpacerWLines(Flowable):
    """
    A spacer with legal text vertical lines on the left side.
    """
    def __init__(self, height, color=colors.black):
        Flowable.__init__(self)
        self.height = height
        self.color = color

    def draw(self):
        """
        Draw the line.
        """
        self.canv.setLineWidth(1)
        self.canv.setStrokeColor(self.color)
        # self.canv.line(0, 0, 0, self.height)
        # self.canv.line(5, 0, 5, self.height)


class LegalDocText(Flowable):
    """
    Custom Flowable to draw a vertical line to the left of the text.
    """
    def __init__(self, 
                 text,
                 line_number,
                 style, 
                 line_height=50, # why is this 50?
                 line_color=colors.black, 
                #  line_padding=10
                 line_padding=5):
        Flowable.__init__(self)
        self.text = text
        self.line_number = line_number
        self.line_height = line_height
        self.line_color = line_color
        self.line_padding = line_padding
        self.style = style

    def wrap(self, availWidth, availHeight):
        """Wrap returns the size used."""
        # Determine the space required by the text
        text_obj = Paragraph(self.text, self.style)
        text_width, text_height = text_obj.wrap(availWidth, availHeight)
    
        self.width = availWidth
        self.height = text_height # Adjust this as needed
        return self.width, self.height

    def split(self, availWidth, availHeight):
        p = Paragraph(self.text, self.style)
        w, h = p.wrap(availWidth, availHeight)
        if h <= availHeight:
            return [self]
        return p.split(availWidth, availHeight)

    def draw(self):
        # Create a Paragraph object and draw the text
        text_obj = Paragraph(self.text, self.style)
        text_width, text_height = text_obj.wrap(self.width, self.height)

        # Draw the vertical lines
        self.canv.setLineWidth(1)
        self.canv.setStrokeColor(self.line_color)
        # self.canv.line(0, 0, 0, text_height) # draw two lines
        # self.canv.line(5, 0, 5, text_height)

        # Draw the line number to the left
        # self.canv.drawString(-23, text_height- self.style.fontSize, f"{self.line_number}")
        # the Y is set by the last argument.  
        text_obj.drawOn(self.canv, self.line_padding, 0)


def _cover_page_left_text_width(avail_width: float) -> float:
    return max(1, avail_width / 2 - 20)


class CoverPageLeftTextFragment(Flowable):
    def __init__(self, text_obj: Paragraph, line_padding=10):
        Flowable.__init__(self)
        self.text_obj = text_obj
        self.line_padding = line_padding

    def wrap(self, availWidth, availHeight):
        text_width = _cover_page_left_text_width(availWidth)
        _text_width, text_height = self.text_obj.wrap(text_width, availHeight)
        self.width = availWidth
        self.height = text_height
        return self.width, self.height

    def split(self, availWidth, availHeight):
        text_width = _cover_page_left_text_width(availWidth)
        fragments = self.text_obj.split(text_width, availHeight)
        return [
            CoverPageLeftTextFragment(fragment, self.line_padding)
            for fragment in fragments
        ]

    def draw(self):
        self.text_obj.drawOn(self.canv, self.line_padding, 0)


class CoverPageLeftText(Flowable):
    """
    Custom Flowable to draw a vertical line to the left of the text.
    """
    def __init__(self, 
                 text,
                 line_number,
                 style, 
                 line_height=50, # why is this 50?
                 line_color=colors.black, 
                 line_padding=10,
                 allow_split=False):
        Flowable.__init__(self)
        self.text = text
        self.line_number = line_number
        self.line_height = line_height
        self.line_color = line_color
        self.line_padding = line_padding
        self.style = style
        self.allow_split = allow_split

        # Create the Paragraph object once in the constructor to avoid redundant creation.
        self.text_obj = Paragraph(self.text, self.style)

    def wrap(self, availWidth, availHeight):
        """Wrap returns the size used."""
        # Determine the space required by the text (wrapped once)
        text_width, text_height = self.text_obj.wrap(_cover_page_left_text_width(availWidth), availHeight)
        
        self.width = availWidth
        self.height = text_height  # Adjust this as needed
        return self.width, self.height

    def split(self, availWidth, availHeight):
        if not self.allow_split:
            return []
        text_width = _cover_page_left_text_width(availWidth)
        fragments = self.text_obj.split(text_width, availHeight)
        return [
            CoverPageLeftTextFragment(fragment, self.line_padding)
            for fragment in fragments
        ]

    def draw(self):
        # Draw the wrapped text
        self.text_obj.drawOn(self.canv, self.line_padding, 0)  # why 0 works



class CoverPageRightText(Flowable):
    """
    Custom Flowable to draw a vertical line to the left of the text.
    """
    def __init__(self, 
                 text,
                 line_number,
                 style,
                 line_height=50, # why is this 50?
                 line_color=colors.black, 
                 line_padding=10):
        Flowable.__init__(self)
        self.text = text
        self.line_number = line_number
        self.line_height = line_height
        self.line_color = line_color
        self.line_padding = line_padding
        self.style = style

        self.avaiHeight = None

         # Create the Paragraph object once in the constructor to avoid redundant creation.
        self.text_obj = Paragraph(self.text, self.style)

    def wrap(self, availWidth, availHeight):
        """Wrap returns the size used."""
        # Determine the space required by the text
        self.avaiHeight = availHeight
        text_width, text_height = self.text_obj.wrap(availWidth/2-10, availHeight/2)

        self.width = availWidth
        self.height = text_height # Adjust this as needed
        # print(f"set self.height to {self.height}")
        return self.width, self.height

    def draw(self):
        y_start = min(self.height+150, self.avaiHeight/2)
        # print(f"y: {y_start}")
        self.text_obj.drawOn(self.canv, self.width/2, 0)   # have not figured out how to adjust the height, y_start could be too high



def format_nls(intext: str) -> str:
    """Adds </br> to newline characters."""
    return intext.replace('\n','<br />\n')


MAX_COVER_PARTY_CAPTION_CHARS = 1200
MAX_COVER_PARTY_CAPTION_NAMES = 20


def _is_long_cover_party_caption(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) > MAX_COVER_PARTY_CAPTION_CHARS:
        return True

    semicolon_parts = [part for part in normalized.split(';') if part.strip()]
    comma_parts = [part for part in normalized.split(',') if part.strip()]
    line_parts = [part for part in str(text or "").splitlines() if part.strip()]
    return max(len(semicolon_parts), len(comma_parts), len(line_parts)) > MAX_COVER_PARTY_CAPTION_NAMES


def _cover_party_caption_text(text: str, party_label: str, skip_long_party_caption: bool) -> str:
    if skip_long_party_caption and _is_long_cover_party_caption(text):
        return f'{party_label} party list omitted from PDF caption due to length.<br />\n'
    return f'{text}<br />\n'


def strip_leading_num(intext: str) -> str:
    """Strip one leading ``N.`` paragraph marker only when followed by whitespace."""
    return re.sub(r"^\d+\.(?=\s)", "", intext, count=1)
    
SAMPLE_STYLES = getSampleStyleSheet()

attorney_info_style = ParagraphStyle(
    'AttorneyInfo',
    parent=SAMPLE_STYLES['Normal'],
    fontSize=12,
    leading = 12,
    fontName = 'Times-Roman'
)

title_venue_style = ParagraphStyle(
    'TitleVenue',
    parent=SAMPLE_STYLES['Normal'],
    alignment=TA_CENTER,
    fontSize=12,
    leading = 24,
    fontName = 'Times-Roman'
)

# Define a style for the paragraph
default_style = ParagraphStyle(
    'Normal12',
    parent=SAMPLE_STYLES['Normal'],
    fontSize=12,
    leading = 24,
    firstLineIndent = 30,
    fontName = 'Times-Roman',
    keepTogether = False
)

default_times = ParagraphStyle(
    'NormalTimes12',
    parent=SAMPLE_STYLES['Normal'],
    fontName = 'Times-Roman'
)

# style for doc headings
heading_style = ParagraphStyle(
    'CustomCenteredUnderlined',
    parent=SAMPLE_STYLES['Normal'],
    alignment=TA_CENTER,
    underline=True,
    fontSize=12,
    fontName = 'Times-Roman'
)

heading1_style = ParagraphStyle(
    name='Heading1',
    parent=SAMPLE_STYLES['Heading1'],
    fontSize=14,
    spaceAfter=4,
    fontName = 'Times-Roman',
    alignment=TA_CENTER,
)

heading2_style = ParagraphStyle(
    name='Heading2',
    parent=SAMPLE_STYLES['Heading2'],
    fontSize=12,
    spaceAfter=4,
    fontName = 'Times-Roman',
    alignment=TA_CENTER,
)


# Memo-specific styles: readable fonts, normal spacing, left-aligned headings
memo_body_style = ParagraphStyle(
    'MemoBody',
    parent=SAMPLE_STYLES['Normal'],
    fontSize=12,
    leading=15,            # ~1.25 line height for readability
    fontName='Times-Roman',
    spaceAfter=6,
)

memo_heading_style = ParagraphStyle(
    'MemoHeading',
    parent=SAMPLE_STYLES['Heading2'],
    alignment=TA_LEFT,
    fontSize=14,
    fontName='Times-Bold',
    spaceBefore=12,
    spaceAfter=6,
)

memo_citation_style = ParagraphStyle(
    'MemoCitation',
    parent=SAMPLE_STYLES['Normal'],
    fontSize=12,
    leading=15,
    fontName='Times-Italic',
    spaceAfter=6,
)

## Removed MemoBodyIndent since Source indentation is no longer desired

def create_cover_page(attorneys_contact_info: str,
                      venue: str,
                      plaintiff: str, 
                      defendant: str,
                      doc_type: str,
                      is_defendant=True,
                      split_long_party_caption=False,
                      skip_long_party_caption=False) -> tuple[list[Flowable], int]:
    if 'COMPLAINT' in doc_type:
        n_defendents = len(defendant.split(','))
        n_plaintiffs = len(plaintiff.split(','))
    else:
        n_defendents = len(defendant.split(';'))
        n_plaintiffs = len(plaintiff.split(';'))

    flowables = []
    line_num = 1
        # cover page
    body = format_nls(attorneys_contact_info)
    for line in body.split('\n'):
        flowables.append(LegalDocText(line + '\n', line_num, attorney_info_style)) 
        line_num += 1

    flowables.append(SpacerWLines(12))
    if is_defendant:
        if n_defendents > 1:
            side = "Defendants"
        else:
            side = "Defendant"
    else:
        if n_plaintiffs > 1:
            side = "Plaintiffs"
        else:
            side = "Plaintiff"

    flowables.append(LegalDocText(f"Attorney for {side}<br />\n", line_num, attorney_info_style)) 
    line_num += 1 
    flowables.append(SpacerWLines(12))   

    body = format_nls(venue.upper())
    for line in body.split('\n'):
        flowables.append(LegalDocText(f'<b>{line}</b>\n', line_num, title_venue_style))
        line_num += 1
    flowables.append(SpacerWLines(12))

    # start to write the lower left half
    flowables.append(CoverPageLeftText(
        _cover_party_caption_text(plaintiff, "Plaintiff", skip_long_party_caption),
        line_num,
        attorney_info_style,
        allow_split=split_long_party_caption,
    ))
    line_num += 1
    flowables.append(SpacerWLines(12)) 

    temp_style = ParagraphStyle('Z', parent=attorney_info_style, leftIndent=40)
    if n_plaintiffs > 1:
        flowables.append(CoverPageLeftText('Plaintiffs,<br />\n', line_num, temp_style)) 
    else:
        flowables.append(CoverPageLeftText('Plaintiff,<br />\n', line_num, temp_style)) 
    line_num += 1
    flowables.append(SpacerWLines(12))

    temp_style = ParagraphStyle('Z', parent=attorney_info_style, leftIndent=20)
    flowables.append(CoverPageLeftText('v.<br />\n', line_num, temp_style)) 
    line_num += 1
    flowables.append(SpacerWLines(12))

    flowables.append(CoverPageLeftText(
        _cover_party_caption_text(defendant, "Defendant", skip_long_party_caption),
        line_num,
        attorney_info_style,
        allow_split=split_long_party_caption,
    ))
    line_num += 1
    flowables.append(SpacerWLines(12)) 

    temp_style = ParagraphStyle('Z', parent=attorney_info_style, leftIndent=40)
    if n_defendents > 1:
        flowables.append(CoverPageLeftText('Defendants.<br />\n', line_num, temp_style)) 
    else:   
        flowables.append(CoverPageLeftText('Defendant.<br />\n', line_num, temp_style)) 
    line_num += 1
    flowables.append(SpacerWLines(12))

    # start to write the lower right half
    temp_style = ParagraphStyle('Z', parent=attorney_info_style, leftIndent=20)
    flowables.append(CoverPageRightText(f'Case No. ___________<br />{doc_type}', line_num, temp_style))
    line_num += 1
    flowables.append(SpacerWLines(12))

    flowables.append(PageBreak())

    return flowables, line_num


def create_analysis_cover(attorneys_contact_info: str,
                      venue: str,
                      plaintiff: str, 
                      defendant: str,
                      side: str) -> tuple[list[Flowable], int]:
    flowables = []
    line_num = 0

    # Heading
    flowables.append(Spacer(1, 6))
    flowables.append(Paragraph('Case Overview', memo_heading_style))
    flowables.append(Spacer(1, 6))

    # Case metadata
    case_name = f"{plaintiff} vs. {defendant}"
    side_str = side.capitalize()
    now = datetime.now()
    date_str = now.strftime("%A, %B %d, %Y")

    flowables.append(Paragraph(f'<b>Case Name:</b> {case_name}', memo_body_style))
    flowables.append(Paragraph(f'<b>Perspective:</b> {side_str}', memo_body_style))
    flowables.append(Paragraph(f'<b>Prepared On:</b> {date_str}', memo_body_style))

    flowables.append(Spacer(1, 8))
    flowables.append(HorizontalLine(500, colors.black))
    flowables.append(Spacer(1, 8))

    return flowables


def format_affirmative_defenses(paragraph_txt, relative_line_num) -> list[Flowable]:
    """
    Special formatting for the answer ADs
    - No paragraph numbers
    - center lines in parentheses
    - bold, and centered lines ending with: 'Affirmative Defense'
    """
    ret_flowables = []

    paragraph = strip_leading_num(paragraph_txt)
    paragraph = paragraph.strip()
    if paragraph[0] == '(' and paragraph[-1] == ')':
        ret_flowables.append(LegalDocText(f'{paragraph.title()}<br />\n', relative_line_num, heading_style))
        ret_flowables.append(SpacerWLines(12))
    elif paragraph.endswith('Affirmative Defense'):
        ret_flowables.append(LegalDocText(f'<b>{paragraph}</b><br />\n', relative_line_num, title_venue_style))
    else:    
        ret_flowables.append(LegalDocText(f'{paragraph}<br />\n', relative_line_num, default_style)) 

    return ret_flowables

def format_rfps(paragraph_txt: str , relative_line_num: int) -> list[Flowable]:
    """
    Special formatting for RFP section:
    1. RFP number is right aligned.
    2. No paragraph numbers on text
    """
    ret_flowables = []
    paragraph = paragraph_txt.strip()
    ret_flowables.append(LegalDocText(f'{paragraph}<br />\n', relative_line_num, default_style)) 

    return ret_flowables

def create_section_text_rfp(sections: dict) -> list[Flowable]:
    section_flowables = []
    relative_line_num = 1
    rel_para_i = 1

    # everything up to prayer_for_relief should have continous paragraph nummbers
    for section_txt, title in sections:

        if len(section_txt) < 1:  # skip blank sections
            continue

        # check if the 2nd paragraph is all caps, if so dont use our title -> use the LLM title
        paragraphs = section_txt.split('\n')

        if not paragraphs[0].isupper():
            section_flowables.append(LegalDocText(f'<u><b>{title}</b></u><br />\n', relative_line_num, heading_style)) 
            section_flowables.append(SpacerWLines(12)) 
            relative_line_num += 1

        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.lstrip()
            if len(paragraph.strip()) < 1:
                continue

            if paragraph.isupper():  # title if it is given
                if paragraph.startswith("REQUEST FOR PRODUCTION NO.") or paragraph.startswith("INTERROGATORY NO.") or paragraph.startswith("RESPONSE TO"):
                    # section_flowables.append(LegalDocText(f'<u><b>{paragraph}</b></u><br />\n', relative_line_num, default_times))
                    section_flowables.append(LegalDocText(f'<u><b>{paragraph}</b></u><br />\n', relative_line_num, heading_style))
                    section_flowables.append(SpacerWLines(12))
                else:
                    section_flowables.append(LegalDocText(f'<u><b>{paragraph}</b></u><br />\n', relative_line_num, heading_style)) 
                    section_flowables.append(SpacerWLines(12))
            elif title in ["REQUESTS FOR PRODUCTION OF DOCUMENTS", "INTERROGATORIES", "REQUESTS FOR ADMISSIONS", "SPECIFIC OBJECTIONS AND RESPONSES"]:
                section_flowables.extend(format_rfps(paragraph, relative_line_num))
                continue
            else:
                # add paragraph numbers here, remove LLM provided numbers (if any)
                paragraph = strip_leading_num(paragraph)
                paragraph = paragraph.strip()
                if paragraph[0] == '(' and paragraph[-1] == ')':
                    section_flowables.append(LegalDocText(f'{paragraph}<br />\n', relative_line_num, heading_style))
                    section_flowables.append(SpacerWLines(12))
                elif title == "" or title == "DEFINITIONS" and i < 3:
                    logger.info("request_pdf title_empty_or_definition index=%s", i)
                    section_flowables.append(LegalDocText(f'{paragraph}<br />\n', relative_line_num, default_style))
                else:    
                    section_flowables.append(LegalDocText(f'{rel_para_i}. {paragraph}<br />\n', relative_line_num, default_style)) 
                    rel_para_i += 1
            relative_line_num += 1

    return section_flowables 
     

def complaint_create_section_text(sections: List[Tuple[str, str]]) -> Tuple[List["Flowable"], str, int]:
    """
    Builds flowables for a complaint. Returns:
      - A list of flowables for ReportLab
      - A concatenated string of paragraphs
      - The last paragraph number used (rel_para_i - 1)
    """
    section_flowables = []
    relative_line_num = 1
    rel_para_i = 1  # Tracks paragraph numbering across sections
    all_paras_text = ""

    for sec_index, (section_txt, title) in enumerate(sections):
        if not section_txt.strip():
            continue  # skip blank sections

        # Split into paragraphs
        paragraphs = section_txt.split('\n')

        # Insert a heading if the first line is not in ALL CAPS
        # (or if you specifically want to override with your own heading)
        if paragraphs and not paragraphs[0].isupper():
            section_flowables.append(LegalDocText(f'<u><b>{title}</b></u><br />\n', relative_line_num, heading_style))
            section_flowables.append(SpacerWLines(12))
            relative_line_num += 1

        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # If the paragraph is in ALL CAPS, treat as subheading
            if paragraph.isupper():
                # e.g., "FACTUAL ALLEGATIONS" if it wasn't the explicit title
                section_flowables.append(LegalDocText(f'<u><b>{paragraph}</b></u><br />\n',relative_line_num, heading_style))
                section_flowables.append(SpacerWLines(12))

            # If it’s a “# FACTUAL ALLEGATIONS” line, format differently
            elif re.match(r"#+\s+[A-Z]", paragraph) and title == "FACTUAL ALLEGATIONS":
                paragraph = paragraph.replace('#', '').strip()
                section_flowables.append(LegalDocText(f'<b>{paragraph}</b>\n',relative_line_num, heading_style))
                section_flowables.append(SpacerWLines(12))

            # Paragraph logic (normal complaint paragraphs)
            else:
                # Remove any leading paragraph number from LLM output
                paragraph = strip_leading_num(paragraph)
                paragraph = paragraph.strip()

                # For parentheses only lines, format differently
                if paragraph.startswith('(') and paragraph.endswith(')'):
                    section_flowables.append(LegalDocText(f'<b>{paragraph}</b>\n',relative_line_num, heading_style))
                    section_flowables.append(SpacerWLines(12))
                # If the paragraph starts with WHEREFORE, we typically don't want to renumber
                elif paragraph.startswith('WHEREFORE') or title == "":
                    section_flowables.append(LegalDocText(f'{paragraph}\n',relative_line_num, default_style))
                else:
                    # if this is cause of action section, replace llm generated "paragraphs 1 through xxx" with prev rel_para_i
                    if title == "FIRST CAUSE OF ACTION": 
                        logger.info("complaint_pdf current_section_title=%s", title)
                        para_ref_pattern = re.compile(r'(paragraphs 1 through )\d+')
                        if para_ref_pattern.search(paragraph):
                            replacement = r'\g<1>' + str(rel_para_i - 1)
                            logger.info(
                                "complaint_pdf paragraph_before_reference_update=%s",
                                paragraph,
                            )
                            paragraph = para_ref_pattern.sub(replacement, paragraph)
                            logger.info(
                                "complaint_pdf paragraph_after_reference_update=%s",
                                paragraph,
                            )
                    # Add your own paragraph number    
                    numbered_para = f'{rel_para_i}. {paragraph}'
                    section_flowables.append(LegalDocText(f'{numbered_para}\n',relative_line_num, default_style))
                    # Keep track of all text in a string if needed
                    all_paras_text += numbered_para + "\n"
                    rel_para_i += 1

            relative_line_num += 1

    return section_flowables, all_paras_text, rel_para_i - 1


def draw_vertical_lines(canvas, doc):
    # print(f"Drawing vertical lines on page {doc.page}")
    # Left vertical line (further from left edge)
    left_line_x1 = 72  # 1 inch from left edge
    left_line_x2 = 74
    canvas.setStrokeColor(colors.black)
    canvas.setLineWidth(1)
    # Increase the vertical line length slightly at the bottom
    top_extension = 30
    bottom_extension = 30  # Extend the line 20 points below the bottom margin
    canvas.line(left_line_x1, doc.bottomMargin - bottom_extension, left_line_x1, doc.height + doc.topMargin + top_extension)
    canvas.line(left_line_x2, doc.bottomMargin - bottom_extension, left_line_x2, doc.height + doc.topMargin + top_extension)
    
    # Right vertical line (matches right margin)
    page_width = doc.width + doc.leftMargin + doc.rightMargin
    right_line_x = page_width - 0.35 * inch  # 0.25 inch from right edge
    canvas.line(right_line_x, doc.bottomMargin - bottom_extension, right_line_x, doc.height + doc.topMargin + top_extension)

    # Adjust the vertical offset to match the starting position of the first text line
    vertical_offset = 15  # Adjust this value to adjust the height
    # Add line numbers outside the left vertical line
    num_lines = 28  # Total number of lines
    line_spacing = (doc.height + doc.topMargin - doc.bottomMargin) / (num_lines - 1)  # Calculate spacing
    # print(f"line spacing = {line_spacing}")
    line_number_y = doc.height + doc.topMargin - vertical_offset # Start at the top
    for i in range(1, num_lines + 1):  # Assuming 28 lines per page
        canvas.drawRightString(left_line_x1 - 5, line_number_y, str(i))  # Position line numbers
        line_number_y -= line_spacing  # Move down by the calculated spacing


def answer_create_section_text(sections: List[Tuple[str, str]]) -> Tuple[List["Flowable"], str]:
    """
    Builds flowables for an answer. Returns:
      - A list of flowables for ReportLab
      - A concatenated string of paragraphs (if needed)
    """
    section_flowables = []
    relative_line_num = 1
    rel_para_i = 1
    all_paras_text = ""

    for sec_index, (section_txt, title) in enumerate(sections):
        if not section_txt.strip():
            continue  # skip blank sections

        paragraphs = section_txt.split('\n')

        # If the first line is not in ALL CAPS, use our title
        if paragraphs and not paragraphs[0].isupper():
            section_flowables.append(LegalDocText(f'<u><b>{title}</b></u>\n', relative_line_num, heading_style))
            section_flowables.append(SpacerWLines(12))
            relative_line_num += 1

        in_response = False
        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # For headings in ALL CAPS
            if paragraph.isupper():
                section_flowables.append(LegalDocText(f'<u><b>{paragraph}</b></u>\n',relative_line_num, heading_style))
                section_flowables.append(SpacerWLines(12))
                in_response = False

            # Check for Affirmative Defenses / General Denial / Answer
            elif title in ["AFFIRMATIVE DEFENSES", "GENERAL DENIAL", "ANSWER"]:
                section_flowables.extend(format_affirmative_defenses(paragraph, relative_line_num))
                continue

            elif paragraph.startswith("Response:"):
                section_flowables.append(LegalDocText(f'{paragraph}\n', relative_line_num, default_style))
                all_paras_text += paragraph + "\n"
                in_response = True

            elif in_response:
                section_flowables.append(LegalDocText(f'{paragraph}\n', relative_line_num, default_style))
                all_paras_text += paragraph + "\n"
                in_response = False

            else:
                paragraph = strip_leading_num(paragraph)
                paragraph = paragraph.strip()
                numbered_para = f'{rel_para_i}. {paragraph}'
                section_flowables.append(LegalDocText(f'{numbered_para}\n',relative_line_num, default_style))
                all_paras_text += numbered_para + "\n"
                rel_para_i += 1

            relative_line_num += 1

    return section_flowables, all_paras_text


class PageNumCanvas(canvas.Canvas):
    """
    Modify Canvas to compute total page number
    """
    #----------------------------------------------------------------------
    def __init__(self, doc, *args, **kwargs):
        """Constructor"""
        canvas.Canvas.__init__(self, *args, **kwargs)
        self.doc = doc
        self.pages = []
        
    #----------------------------------------------------------------------
    def showPage(self):
        """
        On a page break, add information to the list
        """
        self.pages.append(dict(self.__dict__))
        self._startPage()
        
    #----------------------------------------------------------------------
    def save(self):
        """
        Add the page number to each page (page x of y)
        """
        page_count = len(self.pages)
        
        for page in self.pages:
            self.__dict__.update(page)
            self.draw_page_number(page_count)
            canvas.Canvas.showPage(self)
            
        canvas.Canvas.save(self)
        
    #----------------------------------------------------------------------
    def draw_page_number(self, page_count):
        """
        Add the page number
        """
        page = "Page %s of %s" % (self._pageNumber, page_count)
        self.setFont("Helvetica", 9)
        self.draw_footer(page_count)

     #----------------------------------------------------------------------
    def draw_footer(self, total_pages):
        """Draw footer on each page."""
        footer_y = self.doc.bottomMargin - 30  # Position of the horizontal line for footer
        self.setLineWidth(1)
        self.line(self.doc.leftMargin, footer_y, self.doc.width + self.doc.leftMargin, footer_y)  # Horizontal line

        # Add footer text
        footer_text = f"Page {self._pageNumber} of {total_pages}"  # Example footer text
        self.setFont("Helvetica", 10)
        self.drawCentredString(self.doc.width / 2 + self.doc.leftMargin, footer_y - 15, footer_text)  # Centered footer text


def create_complaint_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        parties: str,
        jurisdiction_n_venue: str,
        factual_allegations: str,
        cause_of_action: str,
        damages: str,
        prayer_for_relief: str):
    """Drafts a complaint and returns the filename."""

     # Create a SimpleDocTemplate object  -> BaseDocTemplate
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.1 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])

    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, "COMPLAINT<br />DEMAND FOR JURY TRIAL", False)

    if len(introduction.split("INTRODUCTION")) > 1:
        sections = [(introduction.split("INTRODUCTION")[0], ""),
                    (introduction.split("INTRODUCTION")[1], "INTRODUCTION"),
                    (parties, "THE PARTIES"),
                    (jurisdiction_n_venue, "JURISDICTION AND VENUE"),
                    (factual_allegations, "FACTUAL ALLEGATIONS"),
                    (cause_of_action, "FIRST CAUSE OF ACTION"),
                    (prayer_for_relief, "REQUEST FOR RELIEF")
                    ]
    else:
        sections = [(introduction, "INTRODUCTION"),
                    (parties, "THE PARTIES"),
                    (jurisdiction_n_venue, "JURISDICTION AND VENUE"),
                    (factual_allegations, "FACTUAL ALLEGATIONS"),
                    (cause_of_action, "FIRST CAUSE OF ACTION"),
                    (prayer_for_relief, "REQUEST FOR RELIEF")
                    ]
    
    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)
    
    # create flowables for every sections
    flowables_l, paras, rel_para_i = complaint_create_section_text(sections)
    flowables.extend(flowables_l)
   
    # Build the document with these flowables
    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn, paras, rel_para_i



def _is_answer_cover_caption_layout_error(exc: LayoutError) -> bool:
    return "CoverPageLeftText" in str(exc) or "CoverPageLeftTextFragment" in str(exc)


def _build_answer_pdf_once(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        responses: str,
        affirmative_defenses: str,
        counter_claims: str,
        split_long_party_caption: bool,
        skip_long_party_caption: bool) -> str:
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.1 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

     # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])

    flowables, line_num = create_cover_page(
        attorneys_contact_info,
        venue,
        plaintiff,
        defendant,
        "ANSWER",
        split_long_party_caption=split_long_party_caption,
        skip_long_party_caption=skip_long_party_caption,
    )

    para_i = 1
    if responses.lstrip().startswith("GENERAL"):
        responses_title = "GENERAL DENIAL"
        responses = responses.replace('GENERAL DENIAL', '', 1)
        # print("general responses: ", responses)
    else:
        responses_title = "RESPONSES TO ALLEGATIONS"

    sections = [(introduction, "ANSWER"),
                (responses, responses_title),
                (affirmative_defenses, "AFFIRMATIVE DEFENSES"),
                (counter_claims, "REQUEST FOR RELIEF")
                ]
    
    # Debug: print raw content before sanitization
    logger.info("%s", "=" * 80)
    logger.info("ANSWER PDF - RAW CONTENT BEFORE SANITIZATION:")
    logger.info("%s", "=" * 80)
    for sec_txt, sec_title in sections:
        logger.info("\n--- Section: %s ---", sec_title)
        logger.info("Raw text (first 500 chars): %r", sec_txt[:500])
        # Check for problematic Unicode characters
        for i, char in enumerate(sec_txt[:500]):
            if ord(char) > 127 and char not in "—–" and not char.isprintable():
                logger.info(
                    "Found non-ASCII char at position %s: %r (U+%04X)",
                    i,
                    char,
                    ord(char),
                )
    
    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)
    
    logger.info("\n%s", "=" * 80)
    logger.info("ANSWER PDF - CONTENT AFTER SANITIZATION:")
    logger.info("%s", "=" * 80)
    for sec_txt, sec_title in sections:
        logger.info("\n--- Section: %s ---", sec_title)
        logger.info("Sanitized text (first 500 chars): %r", sec_txt[:500])
    
    # create flowables for every sections
    flowables_l, paras = answer_create_section_text(sections)
    flowables.extend(flowables_l)

    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_answer_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        responses: str,
        affirmative_defenses: str,
        counter_claims: str):
    """Drafts an answer and returns the filename."""
    try:
        return _build_answer_pdf_once(
            fn=fn,
            plaintiff=plaintiff,
            defendant=defendant,
            attorneys_contact_info=attorneys_contact_info,
            venue=venue,
            introduction=introduction,
            responses=responses,
            affirmative_defenses=affirmative_defenses,
            counter_claims=counter_claims,
            split_long_party_caption=True,
            skip_long_party_caption=False,
        )
    except LayoutError as exc:
        if not _is_answer_cover_caption_layout_error(exc):
            raise

        logger.warning(
            "Answer PDF cover caption was too large; retrying with long party caption placeholder.",
            exc_info=True,
        )
        return _build_answer_pdf_once(
            fn=fn,
            plaintiff=plaintiff,
            defendant=defendant,
            attorneys_contact_info=attorneys_contact_info,
            venue=venue,
            introduction=introduction,
            responses=responses,
            affirmative_defenses=affirmative_defenses,
            counter_claims=counter_claims,
            split_long_party_caption=True,
            skip_long_party_caption=True,
        )


def create_rfp_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        definitions: str,
        instructions: str,
        rfps: str,
        is_defendant: bool):
    """Drafts an RFP and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

     # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])
    doc_title = "DEFENDANT'S REQUESTS FOR PRODUCTION OF DOCUMENTS" if is_defendant else "PLAINTIFF'S REQUESTS FOR PRODUCTION OF DOCUMENTS"
    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, doc_title, is_defendant)

    sections = [(introduction, ""),
                (definitions, "DEFINITIONS"),
                (instructions, "INSTRUCTIONS"),
                (rfps, "REQUESTS FOR PRODUCTION OF DOCUMENTS"),
                ]
    
    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)
    
    # create flowables for every sections
    flowables.extend(create_section_text_rfp(sections))

    # doc.build(flowables)
    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_rfa_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        definitions: str,
        instructions: str,
        rfas: str,
        is_defendant: bool):
    """Drafts an RFP and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

     # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])
    doc_title = "DEFENDANT'S REQUESTS FOR ADMISSIONS" if is_defendant else "PLAINTIFF'S REQUESTS FOR ADMISSIONS"
    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, doc_title, is_defendant)

    sections = [(introduction, ""),
                (definitions, "DEFINITIONS"),
                (instructions, "INSTRUCTIONS"),
                (rfas, "REQUESTS FOR ADMISSIONS"),
                ]

    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)

    # create flowables for every sections
    flowables.extend(create_section_text_rfp(sections))

    # doc.build(flowables)
    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_rog_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        definitions: str,
        instructions: str,
        rogs: str,
        is_defendant: bool):
    """Drafts Interrogatories and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])
    doc_title = "DEFENDANT'S INTERROGATORIES" if is_defendant else "PLAINTIFF'S INTERROGATORIES"
    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, doc_title, is_defendant)

    sections = [(introduction, ""),
                (definitions, "DEFINITIONS"),
                (instructions, "INSTRUCTIONS"),
                (rogs, "INTERROGATORIES"),
                ]
    
    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)
    
    # create flowables for every sections
    flowables.extend(create_section_text_rfp(sections))

    # doc.build(flowables)
    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_rfp_response_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        general_objections: str,
        rfp_response: str,
        is_defendant: bool):
    """Drafts an RFP and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])
    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, 'RESPONSES TO REQUESTS FOR PRODUCTION OF DOCUMENTS', is_defendant)

    sections = [(introduction, ""),
                (general_objections, "GENERAL OBJECTIONS"),
                (rfp_response, "SPECIFIC OBJECTIONS AND RESPONSES"),
                ]

    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)

    # create flowables for every sections
    flowables.extend(create_section_text_rfp(sections))

    # doc.build(flowables)
    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_rog_response_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        introduction: str,
        general_objections: str,
        rog_response: str,
        is_defendant: bool):
    """Drafts responses to interrogatories and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
    doc.addPageTemplates([template])

    flowables, line_num = create_cover_page(
        attorneys_contact_info,
        venue,
        plaintiff,
        defendant,
        'RESPONSES TO INTERROGATORIES',
        is_defendant)

    sections = _san_sec([
        (introduction, ""),
        (general_objections, "GENERAL OBJECTIONS"),
        (rog_response, "SPECIFIC OBJECTIONS AND RESPONSES"),
    ])
    
    flowables.extend(create_section_text_rfp(sections))

    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_analysis_pdf(
        fn: str,
        plaintiff: str,
        defendant: str,
        attorneys_contact_info: str,
        venue: str,
        factual_allegations: str,
        elements: str,
        recomendations: str,
        side: str):
    """Drafts a complaint analyzer and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)

    # Explicit margins (left: 1.25 inches, right: 0.35 inches)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Instead of using draw_vertical_lines, just define a basic template
    template = PageTemplate(id='NormalPageTemplate', frames=frame)

    

    # Add the PageTemplate to the document
    doc.addPageTemplates([template])


    flowables = create_analysis_cover(attorneys_contact_info, venue, plaintiff, defendant, side)

    sections = [
            (factual_allegations, "Summary"),
            (elements, "Elements Analysis"),
            (recomendations, 'Recommendations')
            ]

    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)

    # create flowables for every sections
    flowables.extend(create_memo_sections(sections))

    # doc.build(flowables)
    # Pass PageNumberCanvas to show footer
    doc.build(flowables, canvasmaker=lambda *args, **kwargs: PageNumCanvas(doc, *args, **kwargs))

    return fn


def create_analysis_sections(sections: dict) -> list[Flowable]:
    # Deprecated for analysis PDF; retained for backward compatibility if referenced elsewhere
    section_flowables = []
    for section_txt, title in sections:
        section_flowables.append(Paragraph(f'<b>{title}</b>', memo_heading_style))
        for para in section_txt.split('\n'):
            para = para.strip()
            if not para:
                continue
            section_flowables.append(Paragraph(para, memo_body_style))
        section_flowables.append(Spacer(1, 10))
    return section_flowables


def render_simple_markdown(md_text: str) -> list[Flowable]:
    """Very small markdown subset renderer tailored for the user's analysis format.
    Supports:
      - H2 '##' → bold heading (left aligned)
      - H3 '###' → bold subheading
      - Bold '**text**'
      - Italic '*text*' or '_text_'
      - Bulleted lists '- ' lines
      - Em dashes '---' as a horizontal rule
      - Paragraphs
    """
    flow: list[Flowable] = []
    lines = md_text.split('\n')

    bullet_buffer: list[str] = []

    def flush_bullets():
        nonlocal bullet_buffer
        for item in bullet_buffer:
            # indent bullet and render inline formatting
            txt = sanitize_inline(item)
            flow.append(Paragraph(f'• {txt}', memo_body_style))
        bullet_buffer = []

    def sanitize_inline(t: str) -> str:
        # bold
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        # italic (single *)
        t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", t)
        # italic underscores
        t = re.sub(r"_(.+?)_", r"<i>\1</i>", t)
        return t

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_bullets()
            continue

        if line.strip() == '---':
            flush_bullets()
            flow.append(HorizontalLine(500, colors.black))
            continue

        if line.startswith('# '):
            flush_bullets()
            txt = sanitize_inline(line[2:].strip())
            flow.append(Paragraph(f'<b>{txt}</b>', memo_heading_style))
            continue

        if line.startswith('## '):
            flush_bullets()
            txt = sanitize_inline(line[3:].strip())
            flow.append(Paragraph(f'<b>{txt}</b>', memo_heading_style))
            continue

        if line.startswith('### '):
            flush_bullets()
            txt = sanitize_inline(line[4:].strip())
            flow.append(Paragraph(f'<b>{txt}</b>', memo_body_style))
            continue

        if line.lstrip().startswith('- '):
            bullet_text = line.lstrip()[2:].strip()
            bullet_buffer.append(bullet_text)
            continue

        # default paragraph
        flush_bullets()
        flow.append(Paragraph(sanitize_inline(line), memo_body_style))

    flush_bullets()
    return flow

def create_memo_sections(sections: dict) -> list[Flowable]:
    section_flowables = []
    for idx, (section_txt, title) in enumerate(sections):
        if not section_txt:
            continue

        # No page break before Recommendations

        # Optional section heading for clarity/consistency
        if title:
            section_flowables.append(Paragraph(f'<b>{title}</b>', memo_heading_style))

        # Render entire section as markdown, using simple renderer
        section_flowables.extend(render_simple_markdown(section_txt))

        section_flowables.append(Spacer(1, 10))

    return section_flowables


def create_dismiss_pdf_no_toc(fn, 
                        plaintiff, defendant, attorneys_contact_info, venue,
                        introduction, background, standard, arguments, conclusion):
    """Drafts an Motion to Dismiss and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    # Create a frame for the content
    frame = Frame(doc.leftMargin, doc.bottomMargin, usable_w, usable_h, id='normal')
    # Define a PageTemplate and pass the draw_vertical_lines function to onPage
    template = PageTemplate(id='VerticalLineTemplate', frames=frame)
    template = PageTemplate(id='VerticalLineTemplate', frames=frame, onPage=draw_vertical_lines)
   
    # Add the PageTemplate to the document
    doc.addPageTemplates([template])

    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, "MOTION TO DISMISS")

    sections = [(remove_anchor_tags(introduction), ""),
                (remove_anchor_tags(background), "BACKGROUND"),
                (remove_anchor_tags(standard), "LEGAL STANDARD"),
                (remove_anchor_tags(arguments), "ARGUMENTS"),
                (remove_anchor_tags(conclusion), "CONCLUSION")
                ]
    
    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)
    
    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin
    
    frame = Frame(doc.leftMargin, doc.bottomMargin,
              usable_w, usable_h, id="normal")
    # create flowables for every sections
    flowables.extend(create_section_text_dismiss(sections, usable_w, usable_h))

    doc.build(flowables)

    return fn



heading3_style = ParagraphStyle(
    name='Heading3',
    parent=SAMPLE_STYLES['Heading3'],
    fontSize=14,
    spaceAfter=10,
    fontName = 'Times-Roman',
)


heading4_style = ParagraphStyle(
    name='Heading4',
    parent=SAMPLE_STYLES['Heading4'],
    fontSize=12,
    spaceAfter=10,
    fontName = 'Times-Roman',
)

default_style.keepTogether = False


DOT_TAB = (6.25 * inch, TA_RIGHT, '.')

toc_h3_style = ParagraphStyle(
    name='TOC-Heading3',
    parent=heading3_style,
    leftIndent=10,
    firstLineIndent=0,
    tabStops=[(6.25 * inch, TA_RIGHT, '.')]
)

toc_h4_style = ParagraphStyle(
    name='TOC-Heading4',
    parent=heading4_style,
    leftIndent=20,
    firstLineIndent=0,
    tabStops=[(6.25 * inch, TA_RIGHT, '.')]
)

toc_title_style = ParagraphStyle(
    name='TOCTitle',
    parent=SAMPLE_STYLES['Normal'],
    fontSize=14,
    leading=20,
    alignment=1,  # centre
    spaceAfter=10,
    fontName='Times-Roman'
)

def create_dismiss_pdf(fn, 
                        plaintiff, defendant, attorneys_contact_info, venue,
                        introduction, background, standard, arguments, conclusion):
    """Drafts an Motion to Dismiss and returns the filename."""
    page_w, page_h = letter
    doc = BaseDocTemplate(fn, pagesize=letter)
    doc.leftMargin = inch + 0.25 * inch  # 1.25 inches from left edge
    doc.rightMargin = 0.35 * inch
    doc.topMargin = inch
    doc.bottomMargin = inch

    # Compute usable dimensions explicitly (don't trust doc.width after changing margins)
    # Add small buffer to create gap between text and right vertical line
    right_buffer = 0.05 * inch  # Small gap between text and right line
    usable_w = page_w - doc.leftMargin - doc.rightMargin - right_buffer
    usable_h = page_h - doc.topMargin - doc.bottomMargin

    extra_space = 4.75 * inch

    # — TOC template —
    toc_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin - extra_space,
        usable_w,
        usable_h + extra_space,
        id="TOCFrame"
    )
    toc_template = PageTemplate(id="TOCTemplate", frames=[toc_frame])

    # — BODY template —
    body_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        usable_w,
        usable_h,
        id="BodyFrame"
    )
    body_template = PageTemplate(id="BodyTemplate", frames=[body_frame])

    # Register both templates; default is the first one (TOC)
    doc.addPageTemplates([toc_template, body_template])

    flowables, line_num = create_cover_page(attorneys_contact_info, venue, plaintiff, defendant, "MOTION TO DISMISS")
    
    # Table of Contents setup
    toc = TableOfContents()
    toc.dotsMinLevel = 0
    toc.levelStyles = [toc_h3_style, toc_h4_style]
    # Insert TOC into flowables
    flowables.append(Paragraph("Table of Contents", toc_title_style))
    flowables.append(toc)
    

    # 3) Switch back to the BODY template
    flowables.append(NextPageTemplate("BodyTemplate"))
    flowables.append(PageBreak())

    doc.afterFlowable = make_after_flowable(doc)

    sections = [(remove_anchor_tags(introduction), ""),
                (remove_anchor_tags(background), "BACKGROUND"),
                (remove_anchor_tags(standard), "LEGAL STANDARD"),
                (remove_anchor_tags(arguments), "ARGUMENTS"),
                (remove_anchor_tags(conclusion), "CONCLUSION")
                ]

    # Sanitize sections to handle Unicode characters
    sections = _san_sec(sections)

    # create flowables for every sections
    flowables.extend(create_section_text_dismiss(sections, usable_w, usable_h))

    doc.multiBuild(flowables)   # == no footer

    return fn


def create_section_text_dismiss_0(sections: List[Tuple[str, str]]) -> Tuple[List["Flowable"], str]:
    """Builds flowables for an Motion to dismiss. Returns:
      - A list of flowables for ReportLab """
    section_flowables = []
    relative_line_num = 1
    rel_para_i = 1

    for sec_index, (section_txt, title) in enumerate(sections):
        if not section_txt.strip():
            continue  # skip blank sections

        paragraphs = section_txt.split('\n')

        section_flowables.append(Paragraph(f'<u><b>{title}</b></u>', heading1_style))
        section_flowables.append(SpacerWLines(12))    

        prev_letter_i = 0
        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # For headings in ALL CAPS or ** subheading **
            if paragraph.isupper():
                if paragraph.startswith('**'):
                    paragraph = paragraph[2:-2]
                # print("write para 2: ", paragraph)
                section_flowables.append(Paragraph(f'<u><b>{paragraph}</b></u>',heading1_style))
                section_flowables.append(SpacerWLines(12))
                # reset prev_letter_i
                prev_letter_i = 0
            elif paragraph.startswith('**') and paragraph.endswith('**'):
                # print("write para 3: ", paragraph[2:-2].strip())
                section_flowables.append(Paragraph(f'<b>{number_to_letter(prev_letter_i +1)}. {paragraph[2:-2].strip().title()}</b>',heading2_style))
                section_flowables.append(SpacerWLines(12))
                prev_letter_i += 1
            else:
                # Remove LLM-provided numbers
                paragraph = strip_leading_num(paragraph)
                paragraph = paragraph.strip()
                # Keep or skip numbering logic as desired
                # For example, you might still want to number paragraphs:
                numbered_para = f'{rel_para_i}. {paragraph}'
                section_flowables.append(Paragraph(f'{numbered_para}',default_style))
                rel_para_i += 1

            relative_line_num += 1

    return section_flowables


def create_section_text_dismiss(sections: List[Tuple[str, str]], avail_w: float| None = None, avail_h: float| None = None) -> Tuple[List["Flowable"], str]:
    """Builds flowables for an Motion to dismiss. Returns:
      - A list of flowables for ReportLab """
    section_flowables = []
    relative_line_num = 1
    rel_para_i = 1

    for sec_index, (section_txt, title) in enumerate(sections):
        if not section_txt.strip():
            continue  # skip blank sections

        paragraphs = section_txt.split('\n')

        section_flowables.append(CondPageBreak(heading1_style.leading))
        section_flowables.append(Paragraph(f'<u><b>{title}</b></u>', heading1_style))

        prev_letter_i = 0
        for i, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            # For headings in ALL CAPS or ** subheading **
            if paragraph.isupper():
                if paragraph.startswith('**'):
                    paragraph = paragraph[2:-2]
                # print("write para 2: ", paragraph)
                section_flowables.append(CondPageBreak(heading3_style.leading))
                section_flowables.append(Paragraph(f'<u><b>{paragraph}</b></u>',heading3_style))

                # reset prev_letter_i
                prev_letter_i = 0
            elif paragraph.startswith('**') and paragraph.endswith('**'):
                # print("write para 3: ", paragraph[2:-2].strip())
                section_flowables.append(CondPageBreak(heading4_style.leading))
                section_flowables.append(Paragraph(f'<b>{number_to_letter(prev_letter_i +1)}. {paragraph[2:-2].strip().title()}</b>',heading4_style))
            
                prev_letter_i += 1

            else:
                # Strip AI numbering and add our own
                paragraph = strip_leading_num(paragraph).strip()
                numbered   = f"{rel_para_i}. {paragraph}"
                numbered   = fix_unbalanced_tags(numbered)

                probe = Paragraph(numbered, default_style)

                # Use the template’s available frame size
                _w, p_height = probe.wrap(avail_w, avail_h)

                section_flowables.append(CondPageBreak(p_height))
                section_flowables.append(probe)

                rel_para_i += 1

            relative_line_num += 1

    return section_flowables


def make_after_flowable(doc):
    def after(flowable):
        if isinstance(flowable, Paragraph):
            text = flowable.getPlainText().strip()
            if not text:
                return
            style_name = flowable.style.name
            if 'Heading1' in style_name or 'Heading3' in style_name:
                # “\t” triggers the tab-stop that has leader='.'.
                doc.notify('TOCEntry', (0, f"\t{text}", doc.page))
            elif 'Heading4' in style_name:
                doc.notify('TOCEntry', (1, f"\t{text}", doc.page))

    return after



# -------------------------------------------------------------
#  add_table_of_authorities.py
# -------------------------------------------------------------
import re, io,  collections
from pathlib import Path
from typing import Sequence, DefaultDict

from PyPDF2 import PdfReader, PdfWriter          # ←  pip install pypdf
from reportlab.pdfgen import canvas               # ←  pip install reportlab

from pathlib import Path
# -----------------------------------------------------------------
#  1.  helpers
# -----------------------------------------------------------------
def _auth_key(auth: str) -> str:
    """
    Normalise a citation so that the test ‘if key in page_text’
    survives small formatting differences:
      "Ashcroft v. Iqbal (556 U.S. 662)"  →  "ashcroft v. iqbal"
    """
    # key = auth.split('(', 1)[0].rstrip(', ').lower()
    # key = re.sub(r'\s+', ' ', key)          # squeeze spaces
    splits = auth.split('\t(')
    if len(splits) > 1:
        key = splits[1].rstrip(')')
    else:
        key = auth
    logger.info("auth=%s -> _auth_key=%s", auth, key.lower())
    return key.lower()


# -----------------------------------------------------------------
#  2.  find_citation_pages body_anchor is **mandatory**  scan the *finished* pdf, find which pages cite which cases
# -----------------------------------------------------------------

def _norm(s: str) -> str:
    """Collapse any whitespace (spaces, tabs, newlines) to a single space."""
    return re.sub(r'\s+', ' ', s).strip().lower()


def find_citation_pages(
        finished_pdf_path: str | Path,
        authorities      : Sequence[str],
        body_anchor      : str,                         # REQUIRED
) -> tuple[DefaultDict[str, set[int]], int, int]:
    """
    Scan the *finished* brief to learn
      • which pages cite which authorities
      • how many pages the TOC spans
      • where the body starts  (first page after the TOC)

    body_anchor – pass the first 15-30 chars of the opening paragraph.
                  Must appear verbatim (modulo whitespace) in the PDF.
    """
    rdr       = PdfReader(str(finished_pdf_path))
    n_pages   = len(rdr.pages)

    txt_page = lambda i: _norm(rdr.pages[i].extract_text() or "")

    # ---------- 1️⃣  locate the TOC start -----------------------------------
    toc_start = next(i for i in range(n_pages)
                     if "table of contents" in txt_page(i))
    logger.info("TOC starts at PDF-page %s", toc_start + 1)

    # ---------- 2️⃣  locate the body start  ---------------------------------
    anchor_norm = _norm(body_anchor)

    body_start_idx = toc_start + 1
    for i in range(toc_start + 1, n_pages):
    # for i in range(toc_start, n_pages):
        # print("txt_page(i) =  ", i, "; text = ", txt_page(i))
        if anchor_norm and anchor_norm in txt_page(i):
            body_start_idx = i
            break
            
    logger.info(
        "body_anchor «%s…» found on PDF-page %s",
        body_anchor[:50],
        body_start_idx + 1,
    )

    toc_pages = body_start_idx - toc_start

    # ---------- 3️⃣  build a quick lookup map for authorities ---------------
    key_map = { _auth_key(a): a for a in authorities }

    citation_pages: DefaultDict[str, set[int]] = collections.defaultdict(set)

    # ---------- 4️⃣  scan every page ----------------------------------------
    for page_no in range(n_pages):
        # ✱ Skip any page that is still part of the TOC
        if page_no < body_start_idx:          # ← new guard
            continue
        page_txt = txt_page(page_no)
        for key, full in key_map.items():
            if key and key in page_txt:
                citation_pages[full].add(page_no + 1)   # 1-based numbering

    # Optional verbose check
    logger.info("citation_pages ->")
    for auth, pages in citation_pages.items():
        logger.info("  %s -> %s", auth, sorted(pages))

    return citation_pages, toc_pages, toc_start, body_start_idx


# -----------------------------------------------------------------
#  3.  Build a TOA pdf from the mapping we just produced
# -----------------------------------------------------------------

class IndexLine(Flowable):
    def __init__(self, title, pages, width,
                 fontName='Times-Roman', fontSize=12, leading=14, dot=' .'):
        super().__init__()
        self.title, self.pages, self.width = title, pages, width
        self.fontName = fontName
        self.fontSize = fontSize
        self.leading  = leading
        self.dot      = dot
        self.lines    = self._wrap_title()
        self.height   = self.leading * len(self.lines)

    # ── width helpers ────────────────────────────────────────────────────
    def _str_w(self, txt):
        return stringWidth(txt, self.fontName, self.fontSize)

    def _wrap_title(self):
        words, lines, cur = self.title.split(), [], []
        # allow ≈​6 dot-chars breathing-room on the leader
        max_left = self.width - self._str_w(self.pages) - 6 * self._str_w(self.dot)
        for w in words:
            if self._str_w(' '.join(cur + [w])) <= max_left:
                cur.append(w)
            else:
                lines.append(' '.join(cur));  cur = [w]
        lines.append(' '.join(cur))
        return lines

    # ── Platypus API ─────────────────────────────────────────────────────
    def wrap(self, availW, availH):  return self.width, self.height

    def draw(self):
        c = self.canv
        c.setFont(self.fontName, self.fontSize)
        y = (len(self.lines) - 1) * self.leading

        # all precursor lines (no dots)
        for line in self.lines[:-1]:
            c.drawString(0, y, line);  y -= self.leading

        # final line with leader
        last = self.lines[-1]
        w_left, w_right = self._str_w(last), self._str_w(self.pages)
        w_dot  = self._str_w(self.dot)
        gap    = max(self.width - w_left - w_right, 0)
        dots   = self.dot * int(max(gap / w_dot - .5, 0))

        c.drawString(0, y, last)
        c.drawString(w_left, y, dots)
        c.drawRightString(self.width, y, self.pages)


# ───────── new build_toa_pdf() using IndexLine ───────────────────────────
def build_toa_pdf(citation_pages, style_body=None, style_head=None):

    buf = io.BytesIO()
    FOOTER_H = 0.5 * inch

    # 1) Doc + frame (unchanged)
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=72, rightMargin=72,
        topMargin=72,  bottomMargin=72 + FOOTER_H
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width,      doc.height - FOOTER_H,
                  id="TOAFrame")
    doc.addPageTemplates([PageTemplate(id="TOA", frames=[frame])])

    # 2) Heading/body styles (still honoured for font/size)
    RIGHT_TAB = (doc.width - 2, TA_RIGHT, '.')  # still used by heading only
    style_body = style_body or ParagraphStyle(
        'TOABody', fontName='Times-Roman', fontSize=12, leading=14,
        leftIndent=20, firstLineIndent=0, spaceBefore=2, spaceAfter=12)
    style_head = style_head or ParagraphStyle(
        'TOAHead', parent=style_body, fontName='Times-Bold',
        alignment=1, fontSize=14, leading=16, spaceAfter=14,
        tabStops=[RIGHT_TAB])

    # 3) Build the flowables list
    flows = [
        Paragraph("Table of Authorities", style_head),
        Spacer(1, 0.10 * inch)
    ]

    # sort citations as before (same key)
    for auth in sorted(citation_pages, key=lambda a: (a.startswith('1'), a.lower())):
        clean  = re.sub(r'\s+', ' ', auth.replace('\t', ' ')).strip()
        pages  = ", ".join(str(p) for p in sorted(citation_pages[auth]))
        flows.append(
            IndexLine(
                clean, pages, doc.width,
                fontName=style_body.fontName,
                fontSize=style_body.fontSize,
                leading =style_body.leading
            )
        )
        flows.append(Spacer(1, 0.10 * inch))

    # 4) Build & return (unchanged)
    doc.build(flows)
    return buf.getvalue(), len(PdfReader(buf).pages)


# ───────────────────────────────────────────────────────────────
#  build_toc_pdf()  – returns (pdf-bytes, page_count)
# ───────────────────────────────────────────────────────────────
def build_toc_pdf(
        toc_entries: list[tuple],
        page_line_counts: list[int]      # one int = lines that fit on that page
) -> tuple[bytes, int]:
    """
    Build a stand-alone TOC with dotted leaders that wrap exactly the
    same way the first-pass TOC did.
    """
    buf = io.BytesIO()
    FOOTER_H = 0.5 * inch

    # 1 ── document & frame (unchanged)
    doc = BaseDocTemplate(
        buf, pagesize=letter,
        leftMargin=72, rightMargin=72,
        topMargin=72,  bottomMargin=72 + FOOTER_H
    )
    extra_space = 4.75 * inch
    toc_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin - extra_space,   # overshoot so long lines fit
        doc.width,
        doc.height + extra_space,
        id="TOCFrame"
    )
    doc.addPageTemplates([PageTemplate(id="TOCTemplate", frames=[toc_frame])])

    # 2 ── title
    flows = [
        Paragraph("Table of Contents", toc_title_style),
        Spacer(1, 0.10 * inch)
    ]

    # 3 ── pagination bookkeeping
    page_idx     = 0
    line_cap     = page_line_counts[0]       # how many lines fit on this page
    lines_so_far = 0                         # how many we've added

    # 4 ── build each TOC entry with IndexLine
    for lvl, title, pg in toc_entries:
        style  = toc_h3_style if lvl == 0 else toc_h4_style
        indent = getattr(style, "leftIndent", 0)

        clean  = re.sub(r'\s+', ' ', title).strip()   # normalise whitespace

        line   = IndexLine(
            clean, str(pg), doc.width - indent,
            fontName = style.fontName,
            fontSize = style.fontSize,
            leading  = style.leading,
            dot      = '.',
        )

        # apply indent for sub-levels
        if indent:
            flows.append(Indenter(indent, 0))
        flows.append(line)
        flows.append(Spacer(1, style.spaceAfter))
        if indent:
            flows.append(Indenter(-indent, 0))

        # ── update pagination counters
        lines_so_far += len(line.lines)
        while lines_so_far >= line_cap and (page_idx + 1) < len(page_line_counts):
            flows.append(PageBreak())
            page_idx     += 1
            line_cap      = page_line_counts[page_idx]
            lines_so_far  = 0

    # 5 ── build and return
    doc.build(flows)
    return buf.getvalue(), len(PdfReader(buf).pages)


# ─────────────────────────────────────────────────────────────
#  shift_toc_numbers  (new version)
# ─────────────────────────────────────────────────────────────
import re
from pathlib import Path
from PyPDF2 import PdfReader, PdfWriter
import io

def shift_toc_numbers(pdf_in: str | Path,
                      pdf_out: str | Path,
                      toc_start: int,
                      toc_pages: int,
                      shift: int):
    """
    • pdf_in/pdf_out  – input & output paths
    • toc_start       – 0-based index of first TOC page
    • toc_pages       – how many pages the TOC spans
    • shift           – pages inserted between TOC and body
    """

    rdr = PdfReader(str(pdf_in))
    toc_entries = []
    logger.info("shift=%s", shift)

    combined_re = re.compile(
    r'^([A-Z].*?)\s'       # Title must start with an uppercase letter
    r'((?:\.\s*){1,})\s*'  # At least one dot-leader repeats
    r'(\d+)$'              # Page number
)
    dots_only_re  = re.compile(r'^((?:\.\s*){0,})(\d+)$')

    started        = False
    pending_title  = None    # type: str|None
    pending_dots = None    # type: tuple[str,int]|None
    
    page_line_counts: list[int] = []      # e.g. [11, 11, 7]

    for page_idx in range(toc_start, toc_start + toc_pages):
        lines_this_page = 0
        text = rdr.pages[page_idx].extract_text() or ""
        for raw_line in text.splitlines():
            ln = raw_line.strip()
            if not ln:
                continue

            # ➊ haven’t hit “Table of Contents” yet?
            if not started:
                if ln.upper() == "TABLE OF CONTENTS":
                    started = True
                # skip everything else until the header appears
                continue

            # ➋ now that started=True, we try our patterns:
            #  (1) full "Title … num" on one line
            logger.info("TOC line=%s", ln)
            if started:
                logger.info("start started")
                m = combined_re.match(ln)
                if m:
                    title, dots, old = m.group(1), m.group(2), int(m.group(3))
                    if title.isupper():
                        level = 0
                    else:
                        level = 1

                    new_page_num = old + shift
                    toc_entries.append((level, title, new_page_num))
                    logger.info("append: level %s, %s %s%s", level, title, dots, old + shift)
                    lines_this_page += 1
                    pending_title = None
                    pending_dots  = None
                    continue

                # 2)  Line is just dots + page number ──────────────────────
                m = dots_only_re.match(ln)
                if m:
                    logger.info("match dots_only_re")
                    dots, old = m.group(1), int(m.group(2))
                    if pending_title:                         # have title already
                        full_title = " ".join(pending_title)
                        if full_title.isupper():
                            level = 0
                        else:
                            level = 1

                        new_page_num = old + shift
                        toc_entries.append((level, full_title, new_page_num))
                        logger.info(
                            "append: level %s, %s %s%s",
                            level,
                            full_title,
                            dots,
                            old + shift,
                        )
                        lines_this_page += 1
                        pending_title = None
                    else:                                     # dots first → buffer
                        pending_dots = (dots, old)
                    continue

                # 3)  Otherwise it's part of a (possibly wrapped) title ────
                if pending_title is None:
                    pending_title = [ln]
                else:
                    pending_title.append(ln)

                # If dots came first, pair them now
                if pending_dots:
                    dots, old     = pending_dots
                    full_title    = " ".join(pending_title)
                    if full_title.isupper():
                        level = 0
                    else:
                        level = 1

                    new_page_num = old + shift
                    toc_entries.append((level, full_title, new_page_num))
                    logger.info(
                        "append: level %s, %s %s%s",
                        level,
                        full_title,
                        dots,
                        old + shift,
                    )
                    lines_this_page += 1
                    pending_title = None
                    pending_dots  = None

        page_line_counts.append(lines_this_page)
        logger.info("TOC lines: page %s has # lines: %s", page_idx, lines_this_page)
    logger.info("After extracting TOC lines:")
    for each in toc_entries:
        logger.info("%s", each)

    # rebuild the TOC with shifted numbers
    toc_pdf, new_toc_pages = build_toc_pdf(toc_entries, page_line_counts)
    logger.info("new_toc_pages=%s", new_toc_pages)
    rdr_newtoc = PdfReader(io.BytesIO(toc_pdf))

    # assemble: cover + new TOC + rest
    w = PdfWriter()
    # copy everything before the original TOC
    for i in range(toc_start):
        w.add_page(rdr.pages[i])
    # insert new TOC
    for p in rdr_newtoc.pages:
        w.add_page(p)
    # copy remaining pages (skip old TOC)
    for i in range(toc_start + toc_pages, len(rdr.pages)):
        w.add_page(rdr.pages[i])

    with open(pdf_out, "wb") as f:
        w.write(f)

    logger.info("TOC rebuilt (+%s) -> %s", shift, pdf_out)


# -----------------------------------------------------------------
#  4.  Assemble: cover + TOC + **new TOA** + body  ------------------
# -----------------------------------------------------------------
def insert_toa_into_brief(
        finished_pdf_path : str | Path,
        authorities       : Sequence[str],
        output_path       : str | Path,
        body_anchor: str
):
    # --- scan the original pdf -------------------------------------
    citation_pages, toc_pages, toc_start_idx, body_start_idx= find_citation_pages(
                                                finished_pdf_path,
                                                authorities, 
                                                body_anchor)
    logger.info("toc_start_idx=%s", toc_start_idx)
    logger.info("body_start_idx=%s", body_start_idx)
    # --- build TOA pdf & know how many pages it will add -----------
    toa_pdf_bytes, toa_pages = build_toa_pdf(citation_pages)
    logger.info("toa_pages=%s", toa_pages)

    # --- now shift every recorded page that lies *after* the TOA -----------
    shift = toa_pages
    citation_pages_shifted = {
        auth: { (p + shift) if p >= body_start_idx + 1 else p   # ← just +1
                for p in pages }
        for auth, pages in citation_pages.items()
    }

    # --- now *update* the old TOC so its leaders point past the new TOA ----
    orig = Path(output_path) 
    tmp_shifted_toc = orig.parent / f"{Path(output_path).stem}_with-shifted-toc.pdf"
    shift_toc_numbers(
        pdf_in=finished_pdf_path,
        pdf_out=tmp_shifted_toc,
        toc_start=toc_start_idx,
        toc_pages=toc_pages,
        shift=toa_pages
    )

# --- reopen PDF that now has a correctly-numbered TOC -------------
    rdr_original = PdfReader(str(tmp_shifted_toc))

    # rebuild TOA with correct numbers (second pass)
    toa_pdf_bytes, toa_pages = build_toa_pdf(citation_pages_shifted)

    # --- merge: cover+TOC, then TOA, then body ---------------------
    # rdr_original = PdfReader(str(finished_pdf_path))
    rdr_toa      = PdfReader(io.BytesIO(toa_pdf_bytes))
    w            = PdfWriter()

    logger.info("body_start_idx=%s", body_start_idx)
    for i in range(body_start_idx):
        w.add_page(rdr_original.pages[i])

    # (b) then insert the new TOA pages you built:
    logger.info("len(rdr_toa.pages)=%s", len(rdr_toa.pages))
    for p in rdr_toa.pages:
        w.add_page(p)

    # (c) then copy the rest of the brief:
    for i in range(body_start_idx, len(rdr_original.pages)):
        w.add_page(rdr_original.pages[i])

    with open(output_path, 'wb') as f:
        w.write(f)

    logger.info("TOA inserted -> %s", output_path)

    logger.info("returning toc_pages=%s toa_pages=%s", toc_pages, toa_pages)
    return toc_pages, toa_pages


def draw_vertical_lines_on_canvas(c: canvas.Canvas,
                                  width: float, height: float,
                                  left_margin: float = 72,
                                  right_margin: float = 560,
                                  top_margin: float = 72,
                                  bottom_margin: float = 72,
                                  num_lines: int = 28):
    """
    Draw your two vertical rules and line‐numbers,
    but use explicit dims rather than canvas+doc.
    """
    # left lines
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    top_ext    = 30
    bottom_ext = 30
    x1, x2     = left_margin, left_margin + 2
    c.line(x1, bottom_margin - bottom_ext, x1, height - top_margin + top_ext)
    c.line(x2, bottom_margin - bottom_ext, x2, height - top_margin + top_ext)

    # right line (matches right margin - 0.35 inch from edge)
    right_line_x = width - 0.35 * inch
    c.line(right_line_x, bottom_margin - bottom_ext,
           right_line_x, height - top_margin + top_ext)

    # line numbers
    avail_height = height - top_margin - bottom_margin
    # we'll leave one extra so they sit exactly at top and bottom
    spacing = avail_height / (num_lines - 1)
    y = height - top_margin
    for i in range(1, num_lines+1):
        c.drawRightString(x1 - 5, y, str(i))
        y -= spacing


FOOTER_Y    = 0.5 * inch
FOOTER_FONT = ("Times-Roman", 10)

def _make_overlay(page_no: int | None,
                  total   : int | None,
                  width   : float,
                  height  : float):
    buf = io.BytesIO()
    c   = canvas.Canvas(buf, pagesize=(width, height))

    # draw the margin lines & numbers
    draw_vertical_lines_on_canvas(
        c, width, height,
        left_margin   = 72,
        top_margin    = 72,
        bottom_margin = 72,
        num_lines     = 28,     # or compute from your line-height
    )

    # draw the footer if requested
    if page_no is not None and total is not None:
        c.setFont(*FOOTER_FONT)
        txt = f"{page_no} of {total}"
        tw  = c.stringWidth(txt, *FOOTER_FONT)
        c.drawString((width-tw)/2, FOOTER_Y, txt)

    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def apply_frame_and_footers(pdf_in : str | Path,
                            pdf_out: str | Path,
                            first_numbered: int = 0):
    """
    Adds vertical lines + line-numbers on every page, and
    “n of N” footers on pages ≥ first_numbered.
    """
    rdr         = PdfReader(str(pdf_in))
    total_pages = len(rdr.pages) - first_numbered
    writer      = PdfWriter()

    for idx, page in enumerate(rdr.pages):
        # choose whether to draw footer
        if idx >= first_numbered:
            page_no = idx - first_numbered + 1
            overlay = _make_overlay(page_no, total_pages,
                                    page.mediabox.width,
                                    page.mediabox.height)
        else:
            overlay = _make_overlay(None, None,
                                    page.mediabox.width,
                                    page.mediabox.height)

        page.merge_page(overlay)
        writer.add_page(page)

    with open(pdf_out, "wb") as f:
        writer.write(f)

    logger.info("frame + footers written -> %s", pdf_out)



def fix_unbalanced_tags(text: str) -> str:
    """
    Closes unbalanced HTML tags like <i>, <b>, <u> in a best-effort way.
    Currently supports <i>, <b>, <u>, <font>, etc.
    """
    tag_stack = []
    output = []
    tag_pattern = re.compile(r'</?([a-zA-Z0-9]+)[^>]*>')

    last_pos = 0
    for match in tag_pattern.finditer(text):
        tag = match.group(0)
        name = match.group(1)
        is_closing = tag.startswith('</')

        output.append(text[last_pos:match.start()])
        output.append(tag)
        last_pos = match.end()

        if not is_closing:
            tag_stack.append(name)
        elif name in tag_stack:
            tag_stack.reverse()
            tag_stack.remove(name)
            tag_stack.reverse()

    output.append(text[last_pos:])

    # Close any leftover open tags
    for tag in reversed(tag_stack):
        output.append(f'</{tag}>')

    return ''.join(output)


if __name__ == "__main__":
 
    gen_pdf_status = create_complaint_pdf(
        fn = "backend/static/z_lextext_complaint.pdf",
        plaintiff="ASHLEIGH GODDARD",
        defendant='''WELLINGTON EXTERMINATORS, INC., a
California Corporation, individually and d/b/a
DISCOVERY PEST CONTROL; and Does 1-20,
inclusive''',
        attorneys_contact_info='''EREMY PASTERNAK, ESQ., BAR NO: 181618
jdp@pasternaklaw.com
DEANNA L. MAXFIELD, ESQ., BAR NO: 291913
dm@pasternaklaw.com
LAW OFFICES OF JEREMY PASTERNAK
A Professional Corporation
354 Pine Street, Fifth Floor
San Francisco, CA 94104
Telephone: (415) 693-0300
Facsimile: (415) 693-0393''',
        venue='''UNITED STATES DISTRICT COURT
NORTHERN DISTRICT OF CALIFORNIA''',
        introduction=''' Now comes ASHLEIGH GODDARD, Plaintiff in this action, and files this Complaint, and
further alleges as follows: ''',
        jurisdiction_n_venue='''Venue is proper before this court as a substantial part of the actions and injuries
giving rise to this Complaint took place in Brentwood, California, and because Plaintiff performed
her labors for Defendants and/or Does 1-20 in Brentwood, California.
Plaintiff at all relevant times herein was an employee protected by the provisions of
the California Fair Employment and Housing Act, California Government Code §12900 et seq., and
Title VII of the Civil Rights Act of 1964, 42 U.S.C. §§ 2000e, et seq.''',
        factual_allegations='''Plaintiff began working for Defendants and/or Does 1-20 on March 16, 2020, as a Route Manager.
Plaintiff is a Black woman.
Shortly after the killing of George Floyd in Minneapolis in late May, 2020, and the
ensuing protests, management of Defendant WELLINGTON EXTERMINATORS, INC., and Plaintiff’s coworkers began making derogatory comments about Black people.''',
        cause_of_action='''FIRST CLAIM FOR RELIEF
Discrimination on the Basis of Race in Violation of the Fair Employment and Housing Act
(Cal. Gov. Code §§ 12940, et seq.)
As Against All Defendants and/or Does 1-20
Plaintiff hereby incorporates each and every paragraph set forth above as if fully set forth herein.
Plaintiff is and at all times relevant to this complaint was protected from
discrimination on the basis of her race by California Government Code §§12940, et seq.
THIRD CLAIM FOR RELIEF
Retaliation in Violation of the California Fair Employment and Housing Act (Cal. Gov.
Code §§ 12940 et seq.)
Against All Defendants and/or Does 1-20
By this reference, Plaintiff hereby incorporates each and every paragraph set forth
above as though fully set forth herein.
''',
        damages="",
        prayer_for_relief='''WHEREFORE, Plaintiff makes the following demand:
As to the First through Seventh Claims for Relief set forth herein, Plaintiff prays:
a) That process be issued and served as provided by law, requiring Defendants to appear
and answer or face judgment;
b) That Plaintiff have and recover a judgment against Defendants in an amount to be
determined at trial as special damages;''')
    

    # # test answer PDF 
    # fn = "debug.pdf"
    # gen_pdf_status = create_answer_pdf(fn="backend/static/" + fn,
    #                                 plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
    #                                 defendant=mem.get('inputs/defendant', "specify defendant"),
    #                                 attorneys_contact_info=mem.get('inputs/attorneys_contact_info', "No Attorneys_contact_info"),
    #                                 venue=mem.get('inputs/venue', "Venue not yet specified"),
    #                                 introduction=mem.get('drafted_sections/intro', ''),
    #                                 responses=mem.get('drafted_sections/responses', ''),
    #                                 affirmative_defenses=mem.get('drafted_sections/affirmative_defenses', ''),
    #                                 counter_claims=mem.get('drafted_sections/counter_claims', ''),
                                        # )
