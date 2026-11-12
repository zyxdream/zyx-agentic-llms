import pytest

from backend.pdf_creator import strip_leading_num


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1. this has line num", " this has line num"),
        ("14. Waymo invested in research.", " Waymo invested in research."),
        ("2.\tA numbered paragraph.", "\tA numbered paragraph."),
        ("3.\u00a0A numbered paragraph.", "\u00a0A numbered paragraph."),
        ("No line num here.", "No line num here."),
        ("trailing number 1. ", "trailing number 1. "),
        ("2016, the project became Waymo.", "2016, the project became Waymo."),
        ("2016 marked a milestone.", "2016 marked a milestone."),
        ("2016-10-01 was the filing date.", "2016-10-01 was the filing date."),
        ("14,000 miles were logged.", "14,000 miles were logged."),
        ("3.5 million miles were driven.", "3.5 million miles were driven."),
        ("8,836,922 is the patent number.", "8,836,922 is the patent number."),
        ("42 U.S.C. § 1983 applies.", "42 U.S.C. § 1983 applies."),
        ("1.No separating whitespace.", "1.No separating whitespace."),
        ("1)", "1)"),
        ("1.", "1."),
        (" 1. Leading whitespace.", " 1. Leading whitespace."),
        ("1. 2. Nested numbering.", " 2. Nested numbering."),
        ("", ""),
    ],
)
def test_strip_only_a_plain_numbered_paragraph_prefix(source, expected):
    # Keep the helper's whitespace contract; renderers already strip the result.
    assert strip_leading_num(source) == expected
