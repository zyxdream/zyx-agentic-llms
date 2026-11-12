import os
import pytest
from backend.util.data_tools import (
    extract_from_json,
    get_complaint_text_from_file,
    prepare_complaint_text_for_memory,
)

from backend.util.data_tools import check_valid_file_length


def test_extract_from_json():
    output = '''{"Affirmative Defenses":[
{"Failure to State a Claim": "The defense of 'Failure to State a Claim' is applicable here because even if the allegations of false advertising and trademark infringement are true, HomeLight must sufficiently demonstrate all the elements necessary under the Lanham Act, such as proving that the claims are indeed false, misleading, and that they have caused actual consumer confusion or harm. If these elements are not adequately alleged, then the claim may fail at the legal threshold. See Ashcroft v. Iqbal, 556 U.S. 662 (2009), which sets the standard that claims must be plausible on their face to survive a motion to dismiss."},
{"Truth as Defense (Defense to Defamation or False Advertising)": "Under the Lanham Act, a statement is not actionable if it is true. If HomeOpenly can demonstrate that its statements about HomeLight are substantially true, then this can serve as a valid defense. Truth has long been a defense in defamation actions and by extension, false advertising under the Lanham Act. The case Boule v. Hutton, 328 F.3d 84 (2d Cir. 2003) supports the assertion that truth is a complete defense to claims of defamation."},
{"Fair Use Doctrine": "The Fair Use Doctrine permits the use of a trademark under a nominative fair use where the defendant uses the plaintiff's mark to describe the plaintiff's product for purposes such as criticism and commentary. HomeOpenly's use of HomeLight's trademark, if for legitimate commentary or criticism, may not necessarily constitute infringement. This doctrine is embedded in case law such as New Kids on the Block v. News Am. Publ'g, Inc., 971 F.2d 302 (9th Cir. 1992), which outlines the criteria for nominative fair use and its protection."},
{"Lack of Consumer Confusion": "A core element of trademark infringement under the Lanham Act is that the use of the mark creates a likelihood of consumer confusion about the source or sponsorship of the goods or services. If HomeOpenly's branding and advertising can be demonstrated to be clear, distinct, and not likely to confuse consumers about the source, sponsorship, or affiliation of their services compared to HomeLight's, this would be a strong defense, referencing the seminal case of Polaroid Corp. v. Polarad Elecs. Corp., 287 F.2d 492 (2d Cir. 1961), which provides the factors for determining likelihood of confusion."},
{"No Commercial Speech": "HomeOpenly may argue that its statements regarding HomeLight do not qualify as 'commercial advertising or promotion' in the context of the Lanham Act and thus shouldn't be deemed actionable under the statute. The Lanham Act only targets false or misleading statements made in commercial advertising or promotion. The distinction between commercial and non-commercial speech is exemplified in the case Gordon & Breach Sci. Publishers S.A. v. Am. Inst. of Physics, 859 F. Supp. 1521 (S.D.N.Y. 1994), which discusses the boundaries of what constitutes commercial promotion."}
]}'''
    output_l = extract_from_json("Affirmative Defenses", output)
    print(output_l)

    extract_from_json("whateverkey", None)


def test_prepare_complaint_text_for_memory_keeps_full_pdf_text_and_metadata_alignment():
    source_lines = ["Court", "Case No. 123", "1. First allegation"]
    line_metadata = [
        {"line_number": 100, "page_number": 1, "page_line_number": 0, "bold": True, "ocr": False},
        {"line_number": 101, "page_number": 1, "page_line_number": 1, "bold": False, "ocr": False},
        {"line_number": 102, "page_number": 2, "page_line_number": 0, "bold": None, "ocr": True},
    ]

    cover_text, text_by_lines, normalized_metadata = prepare_complaint_text_for_memory(
        "complaint.pdf",
        "\n".join(source_lines),
        line_metadata,
        lines_per_chunk_pdf=2,
    )

    assert cover_text == "Court\nCase No. 123"
    assert text_by_lines == source_lines
    assert normalized_metadata == [
        {"line_number": 0, "page_number": 1, "page_line_number": 0, "bold": True, "ocr": False},
        {"line_number": 1, "page_number": 1, "page_line_number": 1, "bold": False, "ocr": False},
        {"line_number": 2, "page_number": 2, "page_line_number": 0, "bold": None, "ocr": True},
    ]


def test_prepare_complaint_text_for_memory_builds_default_metadata_when_missing():
    cover_text, text_by_lines, normalized_metadata = prepare_complaint_text_for_memory(
        "complaint.docx",
        "Caption\n1. First allegation\n2. Second allegation",
    )

    assert cover_text == "Caption 1. First allegation 2. Second allegation"
    assert text_by_lines == ["Caption", "1. First allegation", "2. Second allegation"]
    assert normalized_metadata == [
        {"line_number": 0, "page_number": None, "page_line_number": None, "bold": None, "ocr": None},
        {"line_number": 1, "page_number": None, "page_line_number": None, "bold": None, "ocr": None},
        {"line_number": 2, "page_number": None, "page_line_number": None, "bold": None, "ocr": None},
    ]


def test_prepare_complaint_text_for_memory_replaces_legacy_cover_token_with_line_break():
    _cover_text, text_by_lines, normalized_metadata = prepare_complaint_text_for_memory(
        "complaint.pdf",
        "Cover text<CoverPage>1. First allegation",
    )

    assert text_by_lines == ["Cover text", "1. First allegation"]
    assert normalized_metadata == [
        {"line_number": 0, "page_number": None, "page_line_number": None, "bold": None, "ocr": None},
        {"line_number": 1, "page_number": None, "page_line_number": None, "bold": None, "ocr": None},
    ]



def test_get_complaint_text_from_file():
    # filename = "backend/static/Tarantino_v_Gawker-doc.docx"
    filename = "backend/static/RFP_Uber.pdf"
    if not os.path.exists(filename):
        pytest.skip(f"Test file not found: {filename}")
    complaint_text, _, _ = get_complaint_text_from_file(filename)
    print(complaint_text)
    # print(complaint_text[:500])
    # print("====")
    # print(complaint_text[-500:])
    print("# words= ", len(complaint_text.split()))



def test_check_valid_file_length():
     word_file = "backend/static/word.docx"
     if not os.path.exists(word_file):
         pytest.skip(f"Test file not found: {word_file}")
     is_valid_length, page_or_word_number = check_valid_file_length(word_file)
     print(page_or_word_number)
     assert is_valid_length == True

     word_file = "backend/static/X v. Wachtell.pdf"
     if not os.path.exists(word_file):
         pytest.skip(f"Test file not found: {word_file}")
     is_valid_length, page_or_word_number = check_valid_file_length(word_file)
     print(page_or_word_number)
     assert is_valid_length == False


# test_extract_from_json()
# test_get_complaint_text_from_file()
# test_check_valid_file_length()
