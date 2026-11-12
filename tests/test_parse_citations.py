
from backend.complaint_drafter import find_USC_citation

def test_extract_citations():
    text0 = "10. The Defendant failed and refused to comply with the Gawker DMCA Notice, and refused to remove from its website the direct URL links to the infringing material in violation of 17 U.S.C. § 512(c). This non-compliance with the DMCA Notice constitutes a separate cause of action under the DMCA."

    citation0 = find_USC_citation(text0)
    assert citation0 is not None
    assert len(citation0) == 1
    title, section, start_index, end_index = citation0[0]
    assert (title, section) == ('17', '512')
    assert isinstance(start_index, int)
    assert isinstance(end_index, int)
    assert start_index < end_index


def test_no_citations():
    text = "5.  There were no citations, you ass."
    citation0 = find_USC_citation(text)

    assert citation0 is None
