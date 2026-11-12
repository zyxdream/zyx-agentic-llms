from backend.ai.complaint_parser import (
    clean_pdf_page_records_to_full_text,
    extracted2kv,
    make_pdf_line_record,
    ocr_text_to_pdf_line_records,
)


def test_simple():
    intext = """
    ```json
{
    "PLAINTIFF": "Waymo LLC",
    "DEFENDANT": "Uber Technologies, Inc.; Ottomotto LLC; Otto Trucking LLC",
    "VENUE": "UNITED STATES DISTRICT COURT NORTHERN DISTRICT OF CALIFORNIA, SAN FRANCISCO DIVISION"
}
```
    """
    extractValues = extracted2kv(intext, {"PLAINTIFF", "DEFENDANT", "VENUE"})
    print(extractValues)
    assert "Waymo LLC" in extractValues["PLAINTIFF"]
    assert "Uber" in extractValues["DEFENDANT"]
    assert "NORTHERN DISTRICT OF CALIFORNIA" in extractValues["VENUE"]

    intext = """
    PLAINTIFF : Waymo LLC,
    DEFENDANT : Uber Technologies, Inc.; Ottomotto LLC; Otto Trucking LLC
    VENUE :  UNITED STATES DISTRICT COURT NORTHERN DISTRICT OF CALIFORNIA, SAN FRANCISCO DIVISION
        """
    extractValues = extracted2kv(intext, ["PLAINTIFF", "DEFENDANT", "VENUE"])
    print(extractValues)
    assert "Waymo LLC" in extractValues["PLAINTIFF"]
    assert "Uber" in extractValues["DEFENDANT"]
    assert "NORTHERN DISTRICT OF CALIFORNIA" in extractValues["VENUE"]


def test_clean_pdf_page_records_to_full_text_aligns_metadata_after_cleanup():
    case_stamp = r"Case [^\n]*?Page [0-9]+ of [0-9]+"
    page_records = {
        0: [
            make_pdf_line_record("Case No. 123", 0, False, False),
            make_pdf_line_record("<B>INTRODUCTION</B>", 0, True, False),
            make_pdf_line_record("1. First allegation. 2. Second allegation", 0, False, False),
            make_pdf_line_record("12", 0, False, False),
        ],
        1: ocr_text_to_pdf_line_records(
            "3. OCR allegation\n -2- footer",
            1,
            case_stamp,
        ),
    }

    full_text, case_number, line_metadata = clean_pdf_page_records_to_full_text(page_records, 2)

    assert full_text.splitlines() == [
        "Case No. 123",
        "<B>INTRODUCTION</B>",
        "1. First allegation.",
        "2. Second allegation",
        "3. OCR allegation",
    ]
    assert case_number == "123"
    assert line_metadata == [
        {"line_number": 0, "page_number": 1, "page_line_number": 0, "bold": False, "ocr": False},
        {"line_number": 1, "page_number": 1, "page_line_number": 1, "bold": True, "ocr": False},
        {"line_number": 2, "page_number": 1, "page_line_number": 2, "bold": False, "ocr": False},
        {"line_number": 3, "page_number": 1, "page_line_number": 3, "bold": False, "ocr": False},
        {"line_number": 4, "page_number": 2, "page_line_number": 0, "bold": None, "ocr": True},
    ]


test_simple()
