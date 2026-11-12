import backend.util.free_law_tools as fl_tools
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from backend.util.free_law_tools import find_citations, filter_raw_citations, insert_tags, filter_on_casename
from backend.util.free_law_tools import get_cluster, get_docket, get_court, find_fl_citations, get_opinion, extract_holding_from_opinion
from time import time
from backend.util.free_law_tools import annotate_text_with_citations_multiple
import pytest
from celery.exceptions import SoftTimeLimitExceeded


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _mock_no_statutes(monkeypatch):
    monkeypatch.setattr(fl_tools, "find_statues", lambda text: [])
    monkeypatch.setattr(fl_tools, "find_state_codes", lambda text: [])


def _cluster(cluster_id, case_name, citations):
    return {
        "id": cluster_id,
        "case_name": case_name,
        "case_name_short": case_name.split(" v. ", 1)[0] if " v. " in case_name else case_name,
        "case_name_full": case_name,
        "citations": [
            {"volume": vol, "reporter": reporter, "page": page}
            for vol, reporter, page in citations
        ],
    }


def test_courtlistener_headers_include_authorization_only_when_token_present():
    assert fl_tools._build_courtlistener_headers(None) == {"Content-Type": "application/json"}
    assert fl_tools._build_courtlistener_headers("") == {"Content-Type": "application/json"}
    assert fl_tools._build_courtlistener_headers("abc123") == {
        "Content-Type": "application/json",
        "Authorization": "Token abc123",
    }


def test_free_law_tools_source_reads_free_law_token_without_hardcoding():
    source = (_repo_root() / "backend/util/free_law_tools.py").read_text()

    assert 'os.getenv("FREE_LAW_API_TOKEN")' in source
    assert not re.search(r'TOKEN\s*=\s*["\'][0-9a-fA-F]{40}["\']', source)
    assert not re.search(r'Token\s+[0-9a-fA-F]{40}', source)


def test_iter_citation_lookup_chunks_prefers_newline_cut():
    text = ("a" * 80) + "\n" + ("b" * 80)

    chunks = list(fl_tools.iter_citation_lookup_chunks(text, max_chars=100, overlap_chars=10, boundary_search_chars=50))

    assert len(chunks) == 2
    assert all(len(chunk_text) <= 100 for chunk_text, _offset in chunks)
    assert chunks[0][0].endswith("\n")
    assert chunks[0][1] == 0
    assert chunks[1][1] == 71


def test_find_citations_chunked_shifts_indexes(monkeypatch):
    def fake_iter(_text):
        yield "first chunk", 0
        yield "second chunk", 100

    def fake_find_citations(chunk_text):
        if chunk_text == "first chunk":
            return []
        return [{
            "citation": "550 U.S. 544",
            "start_index": 2,
            "end_index": 14,
            "status": 200,
            "clusters": [{"id": 1}],
        }]

    monkeypatch.setattr(fl_tools, "iter_citation_lookup_chunks", fake_iter)
    monkeypatch.setattr(fl_tools, "find_citations", fake_find_citations)

    result = fl_tools.find_citations_chunked("full text")

    assert result[0]["start_index"] == 102
    assert result[0]["end_index"] == 114


def test_find_citations_chunked_dedupes_overlap(monkeypatch):
    def fake_iter(_text):
        yield "first chunk", 0
        yield "overlap chunk", 5

    def fake_find_citations(chunk_text):
        start_index = 10 if chunk_text == "first chunk" else 5
        return [{
            "citation": "550 U.S. 544",
            "start_index": start_index,
            "end_index": start_index + 12,
            "status": 200,
            "clusters": [{"id": 1}],
        }]

    monkeypatch.setattr(fl_tools, "iter_citation_lookup_chunks", fake_iter)
    monkeypatch.setattr(fl_tools, "find_citations", fake_find_citations)

    result = fl_tools.find_citations_chunked("full text")

    assert len(result) == 1
    assert result[0]["start_index"] == 10


@pytest.mark.parametrize(
    "chunked,lookup",
    [
        ("find_citations_chunked", "find_citations"),
        ("find_citations_raw_chunked", "find_citations_raw"),
    ],
)
def test_chunked_citation_lookup_propagates_celery_soft_timeout(
    monkeypatch, chunked, lookup
):
    monkeypatch.setattr(
        fl_tools,
        "iter_citation_lookup_chunks",
        lambda _text: iter([("first chunk", 0)]),
    )

    def timed_out(_text):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(fl_tools, lookup, timed_out)

    with pytest.raises(SoftTimeLimitExceeded):
        getattr(fl_tools, chunked)("full text")


def test_extract_validated_cases_adds_caseviewer_href():
    citations = [{
        "prefix": "/CaseViewer",
        "form": "550 U.S. 544",
        "citationName": "550-U_dot_S_dot_-544",
        "clusterId": 112233,
        "displayName": "Bell Atlantic Corp. v. Twombly\t550 U.S. 544",
        "type": "case",
    }]

    result = fl_tools.extract_validated_cases_from_citations(citations)

    assert result["550 U.S. 544"]["href"] == "/CaseViewer/550-U_dot_S_dot_-544&112233"


def test_verify_citations_with_lookup_tags_short_name(monkeypatch):
    _mock_no_statutes(monkeypatch)
    text = "Blum, 2010 WI 78"
    raw = [{
        "citation": "2010 WI 78",
        "start_index": text.index("2010"),
        "end_index": len(text),
        "status": 200,
        "clusters": [_cluster(8238267, "Blum ex rel. Studinski v. 1st Auto & Casualty Insurance", [("2010", "WI", "78")])],
    }]
    monkeypatch.setattr(fl_tools, "find_citations_raw_chunked", lambda value: (raw, None))

    verified, diagnostics = fl_tools.verify_citations_with_lookup_tags(text)

    assert verified[0]["lookup_tag"] == fl_tools.LOOKUP_TAG_VERIFIED_FL_SHORT_NAME
    assert verified[0]["id"] == 8238267
    assert verified[0]["clusterId"] == 8238267
    assert verified[0]["cluster_id"] == 8238267
    assert diagnostics[0]["href_eligible"] is True


def test_verify_citations_with_lookup_tags_case_name_mismatch_is_linkable(monkeypatch):
    _mock_no_statutes(monkeypatch)
    text = "Wrong Case, 2010 WI 78"
    raw = [{
        "citation": "2010 WI 78",
        "start_index": text.index("2010"),
        "end_index": len(text),
        "status": 200,
        "clusters": [_cluster(8238267, "Blum ex rel. Studinski v. 1st Auto & Casualty Insurance", [("2010", "WI", "78")])],
    }]
    monkeypatch.setattr(fl_tools, "find_citations_raw_chunked", lambda value: (raw, None))

    verified, diagnostics = fl_tools.verify_citations_with_lookup_tags(text)

    assert verified[0]["lookup_tag"] == fl_tools.LOOKUP_TAG_CASE_NAME_MISMATCH
    assert verified[0]["clusterId"] == 8238267
    assert diagnostics[0]["lookup_tag"] == fl_tools.LOOKUP_TAG_CASE_NAME_MISMATCH
    assert diagnostics[0]["href_eligible"] is True
    assert fl_tools.extract_validated_cases_from_citations(verified) == {}


def test_extract_validated_cases_can_include_cluster_resolved_name_mismatches():
    citations = [
        {
            "prefix": "/CaseViewer",
            "form": "209 B.R. 678",
            "citationName": "209-B_dot_R_dot_-678",
            "clusterId": 24001,
            "displayName": "In Re Symington\t209 B.R. 678",
            "type": "case",
            "lookup_tag": fl_tools.LOOKUP_TAG_CASE_NAME_MISMATCH,
        },
        {
            "prefix": "/CaseViewer",
            "form": "494 B.R. 494",
            "citationName": "494-B_dot_R_dot_-494",
            "clusterId": 24002,
            "displayName": "In re Worldwide Education Services, Inc.\t494 B.R. 494",
            "type": "case",
            "lookup_tag": fl_tools.LOOKUP_TAG_CASE_NAME_MISMATCH,
        },
        {
            "prefix": "/CaseViewer",
            "form": "555 U.S. 7",
            "citationName": "555-U_dot_S_dot_-7",
            "clusterId": 24003,
            "displayName": "Winter v. Natural Resources Defense Council, Inc.\t555 U.S. 7",
            "type": "case",
            "lookup_tag": fl_tools.LOOKUP_TAG_CASE_NAME_MISMATCH,
        },
    ]

    assert fl_tools.extract_validated_cases_from_citations(citations) == {}

    result = fl_tools.extract_validated_cases_from_citations(
        citations,
        allowed_tags=fl_tools.LOOKUP_TAGS_ELIGIBLE_FOR_LINKS,
    )

    assert set(result) == {"209 B.R. 678", "494 B.R. 494", "555 U.S. 7"}
    assert result["209 B.R. 678"]["case_name"] == "In Re Symington"
    assert result["494 B.R. 494"]["case_name"] == "In re Worldwide Education Services, Inc."
    assert result["555 U.S. 7"]["case_name"] == "Winter v. Natural Resources Defense Council, Inc."
    assert result["209 B.R. 678"]["href"] == "/CaseViewer/209-B_dot_R_dot_-678&24001"


def test_verify_citations_with_lookup_tags_lacks_opinion_data(monkeypatch):
    _mock_no_statutes(monkeypatch)
    text = "Three Arrows, 647 B.R. 440"
    raw = [{
        "citation": "647 B.R. 440",
        "start_index": text.index("647"),
        "end_index": len(text),
        "status": 404,
        "clusters": [],
    }]
    monkeypatch.setattr(fl_tools, "find_citations_raw_chunked", lambda value: (raw, None))

    verified, diagnostics = fl_tools.verify_citations_with_lookup_tags(text)

    assert verified == []
    assert diagnostics[0]["lookup_tag"] == fl_tools.LOOKUP_TAG_LACKS_OPINION_DATA
    assert diagnostics[0]["clusterId"] is None
    assert diagnostics[0]["href_eligible"] is False


def test_verify_citations_with_lookup_tags_cite_mismatch(monkeypatch):
    _mock_no_statutes(monkeypatch)
    text = "Calderon-Cardona, 821 F.3d 161"
    raw = [{
        "citation": "821 F.3d 161",
        "start_index": text.index("821"),
        "end_index": len(text),
        "status": 200,
        "clusters": [_cluster(3199781, "Walsh v. Teltech Systems, Inc.", [("821", "F.3d", "155")])],
    }]
    monkeypatch.setattr(fl_tools, "find_citations_raw_chunked", lambda value: (raw, None))

    verified, diagnostics = fl_tools.verify_citations_with_lookup_tags(text)

    assert verified == []
    assert diagnostics[0]["lookup_tag"] == fl_tools.LOOKUP_TAG_CITE_MISMATCH
    assert diagnostics[0]["href_eligible"] is False


@pytest.mark.skip(reason="Requires live CourtListener citation lookup")
def test_find_citations():
    instr = '''
Element: Existence of a Trade Secret
Recommendation: For a motion to dismiss, argue that the Plaintiff has not adequately demonstrated the existence of a trade secret, highlighting that the LiDAR technology and its details have been publicly disclosed or are readily ascertainable (Compl. ¶30-31). Cite: Brookhaven Typesetting Servs., Inc. v. Adobe Sys., Inc., 62 F.4th 32, 38 (9th Cir. 2023) (holding that information that is generally known or easily ascertainable is not a trade secret).
Element: Reasonable Measures Taken to Maintain Secrecy
Recommendation: Emphasize the lack of reasonable measures taken by Waymo to maintain the secrecy of its alleged trade secrets, given the significant lapses in security evidenced by unauthorized data downloads by employees (Compl. ¶44-47, 52-53). Cite: InteliClear, LLC v. ETC Glob. Holdings, Inc., 978 F.3d 653, 662 (9th Cir. 2020) (affirming that reasonable measures are required to be proven for trade secret claims).
Element: Misappropriation by the Defendants
Recommendation: Challenge the allegation of misappropriation by Defendants, arguing that the complaint relies on circumstantial evidence and lacks direct proof of Uber's or Otto's engagement in misappropriation activities (Compl. ¶44-47). Cite: Accenture Glob. Servs. GmbH v. Guidewire Software, Inc., 951 F.3d 1541, 1551 (Fed. Cir. 2023) (noting that reliance on speculation and circumstantial evidences is insufficient for claims of misappropriation).
Element: Infringement of one or more claims of the patent
Recommendation: For the patent infringement claims, argue that the accused products do not literally infringe on the patent claims or under the doctrine of equivalents, especially given potential technical differences (Compl. ¶92-98). Cite: Warner-Jenkinson Co. v. Hilton Davis Chem. Co., 520 U.S. 17, 29 (1997) (emphasizing that both literal infringement and doctrine of equivalents require a detailed claim comparison).
Element: Irreparable harm for injunctive relief
Recommendation: Contend that the Plaintiff has failed to demonstrate irreparable harm that cannot be remedied by monetary damages, challenging the sufficiency of claims for injunctive relief (Compl. ¶62-63). Cite: eBay Inc. v. MercExchange, L.L.C., 547 U.S. 388, 391 (2006) (requiring proof of irreparable harm for injunctive relief).
Element: Plaintiff suffered injury in fact and lost money or property
Recommendation: Assert that Waymo has not sufficiently detailed the specific financial or proprietary losses directly attributable to Defendants' alleged actions, which weakens the standing under the California Business and Professions Code § 17200 provision (Compl. ¶134). Cite: Kwikset Corp. v. Superior Court, 51 Cal. 4th 310, 327 (2011) (holding that plaintiffs must demonstrate a tangible loss for standing in unfair competition claims).
'''
    instr = "Halo Elecs., Inc. v. Pulse Elecs., Inc., 579 U.S. 93, 100-101 (2016)” remains plain text"

    instr = "Alameda Newspapers, Inc. v. City of Oakland, 95 Cal. App. 4th 668 (2002) (elements include an agreement, wrongful act, and resultant damages)"

    # instr = "978 F.3d 653, 659"
    # instr = "Ashcroft v. Iqbal, 556 U.S. 662, 678 (2009) (holding that a complaint must contain sufficient factual matter, accepted as true, to state a claim to relief that is plausible on its face)."
    
    # instr = "Conservatorship of Sanderson, 88 Cal. App. 4th 490, 105 Cal. Rptr. 2d 490 (2001) (emphasizing the necessity of clear and convincing evidence in proving the conservatee's incapacity)."
    instr = "Conservatorship of Sanderson,  106 Cal. App. 3d "
    instr = "595 F.3d 391"

    instr = " 595 F.3d 391     595 F.3d 395    596 F.3d 1     596 F.3d 18    596 F.3d 10"
    instr = "2009 WI 99     82 U.S.L.W. 3493"
    instr = "22 Cust. Ct. 349;   42 Cust. Ct. 404;    1949 Cust. Ct. LEXIS 1636;    22 Cust. Ct. 346;  22 Cust. Ct. 321"
    # instr = "22 Cust. Ct. 349;   42 Cust. Ct. 404;    1949 Cust. Ct. LEXIS 1636;    22 Cust. Ct. 346;    22 Cust. Ct. 321;    41 Cust. Ct. 402;    39 Cust. Ct. 480;    1949 Cust. Ct. LEXIS 941;     1949 Cust. Ct. LEXIS 1743;     1949 Cust. Ct. LEXIS 1147;    23 Cust. Ct. 181;    43 Cust. Ct. 398;    1949 Cust. Ct. LEXIS 1754;     23 Cust. Ct. 227;    44 Cust. Ct. 401;     44 Cust. Ct. 443;    45 Cust. Ct. 395;    37 Cust. Ct. 281;    42 Cust. Ct. 303;    47 Cust. Ct. 350"
    instr = "Tungsten Graphics, Inc. v. Deckers Outdoor Corp., 425 F. App’x 607, 610 (9th Cir. 2011)."
    instr = "William H. Helfand, Inc. v. Turner Marketing Corp. 241 F.3d 867, 873 (9th Cir. 2001) (misleading statement must influence purchasing decision)"
    instr = "raw_citations = ['817 F.3d 755',   '19 Cal.App.5th 224']"
    instr = "14 A.L.R. Fed. 819"
    instr = "443 F.2d 1159"
    instr = "126 F.3d 1420"
    instr = "563 U.S. 754"
    instr = "629 F.Supp. 2d 1171, 1183"
    result = find_citations(instr)
    for each in result:
        print(each)
        print()



@pytest.mark.skip(reason="Requires live CourtListener citation lookup")
def test_find_citations_retries():

    instr = ["22 Cust. Ct. 349;   42 Cust. Ct. 404;    1949 Cust. Ct. LEXIS 1636;    22 Cust. Ct. 346;  22 Cust. Ct. 321"] * 60
    start = time()
    for i, each_instr in enumerate(instr):
        print(f"processing {i}")
        result = find_citations(each_instr)
        if result:
            for each in result:
                print(each['citation'])
    end = time()
    print(f"took {end-start:.2f} seconds")






@pytest.mark.skip(reason="Exploratory state-code live/debug test")
def test_wisconsin_statutes():
    """Test Wisconsin statute detection and URL generation."""
    from backend.util.state_codes import find_state_codes, get_wis_code_url, WI_CODES
    
    # Test various Wisconsin statute citations
    test_texts = [
        "The defendant violated Wis. Stat. § 100.18 by making fraudulent representations.",
        "Under Wisconsin Statutes § 111.31, fair employment practices are required.",
        "Wis. Stat. Ann. § 402.314 provides for implied warranty of merchantability.",
        "The Wisconsin Consumer Act, Wis. Stat. § 421.101, regulates consumer credit.",
        "Wis. Stat. § 196 governs regulation of public utilities.",
        "Wisconsin Statute § 895.446 provides treble damages for theft by contractor."
    ]
    
    for test_text in test_texts:
        print(f"\nTesting: {test_text}")
        results = find_state_codes(test_text)
        print(f"Found {len(results)} Wisconsin citations:")
        for result in results:
            print(f"  Citation: {result['displayName']}")
            print(f"  URL: {result['prefix']}")
            print(f"  Type: {result['type']}")
    
    # Test direct URL generation
    print("\n=== Testing direct URL generation ===")
    test_cases = [
        ("111.31", "https://docs.legis.wisconsin.gov/statutes/statutes/111/31"),
        ("196", "https://docs.legis.wisconsin.gov/statutes/statutes/196"),
        ("100.18", "https://docs.legis.wisconsin.gov/statutes/statutes/100/18"),
        ("402.314", "https://docs.legis.wisconsin.gov/statutes/statutes/402/314"),
    ]
    
    for chapter_section, expected_url in test_cases:
        url = get_wis_code_url(WI_CODES, chapter_section)
        print(f"  {chapter_section} -> {url}")
        # Note: URL validation may fail if we don't actually check, but structure should be correct
        assert expected_url in url or url == WI_CODES["base_url"], f"URL mismatch for {chapter_section}"


@pytest.mark.skip(reason="Exploratory state-code live/debug test")
def test_new_york_statutes():
    """Test New York statute detection and URL generation."""
    from backend.util.state_codes import find_state_codes, get_ny_code_url, NY_CODES
    
    # Test various New York statute citations
    test_texts = [
        "The defendant violated N.Y. Gen. Bus. Law § 349 by making deceptive practices.",
        "Under N.Y. C.P.L.R. § 3012, service of pleadings is required.",
        "N.Y. Gen. Oblig. Law § 5-701 provides the statute of frauds requirement.",
        "The plaintiff must comply with N.Y. Civ. Prac. Law & Rules § 3012.",
        "N.Y. Penal Law § 120.05 defines assault in the second degree.",
        "N.Y. Real Prop. Law § 226-b governs tenant protections."
    ]
    
    for test_text in test_texts:
        print(f"\nTesting: {test_text}")
        results = find_state_codes(test_text)
        print(f"Found {len(results)} New York citations:")
        for result in results:
            print(f"  Citation: {result['displayName']}")
            print(f"  URL: {result['prefix']}")
            print(f"  Type: {result['type']}")
    
    # Test direct URL generation
    print("\n=== Testing direct URL generation ===")
    test_cases = [
        ("GBS", "349", "https://www.nysenate.gov/legislation/laws/GBS/349"),
        ("CVP", "3012", "https://www.nysenate.gov/legislation/laws/CVP/3012"),
        ("GOB", "5-701", "https://www.nysenate.gov/legislation/laws/GOB/5-701"),
        ("PEN", "120.05", "https://www.nysenate.gov/legislation/laws/PEN/120.05"),
    ]
    
    for section_abbr, section_num, expected_url in test_cases:
        url = get_ny_code_url(NY_CODES, section_abbr, section_num)
        print(f"  {section_abbr} § {section_num} -> {url}")
        # Note: URL validation may fail if we don't actually check, but structure should be correct
        assert expected_url in url or url == NY_CODES["base_url"], f"URL mismatch for {section_abbr} § {section_num}"


@pytest.mark.skip(reason="Exploratory state-code live/debug test")
def test_massachusetts_statutes():
    """Test Massachusetts statute detection and URL generation."""
    from backend.util.state_codes import find_state_codes, get_ma_code_url, MA_CODES
    
    # Test various Massachusetts statute citations
    test_texts = [
        "The defendant violated MGL c. 93A, § 2 by engaging in unfair trade practices.",
        "Under Mass. Gen. Laws c. 269, § 10, carrying a dangerous weapon is prohibited.",
        "M.G.L. c. 93A provides consumer protection provisions.",
        "The plaintiff's claim under G.L. c. 93A, § 9 for unfair business practices fails.",
        "MGL c. 269, § 10 defines firearms violations."
    ]
    
    for test_text in test_texts:
        print(f"\nTesting: {test_text}")
        results = find_state_codes(test_text)
        print(f"Found {len(results)} Massachusetts citations:")
        for result in results:
            print(f"  Citation: {result['displayName']}")
            print(f"  URL: {result['prefix']}")
            print(f"  Type: {result['type']}")
    
    # Test direct URL generation
    print("\n=== Testing direct URL generation ===")
    test_cases = [
        ("93A", "2", "https://malegislature.gov/Laws/GeneralLaws/PartI/TitleXV/Chapter93A/Section2"),
        ("269", "10", "https://malegislature.gov/Laws/GeneralLaws/PartI/TitleXXI/Chapter269/Section10"),
        ("93A", None, "https://malegislature.gov/Laws/GeneralLaws/PartI/TitleXV/Chapter93A"),
    ]
    
    for chapter, section, expected_url in test_cases:
        url = get_ma_code_url(MA_CODES, chapter, section)
        if url:
            print(f"  MGL c. {chapter}" + (f", § {section}" if section else "") + f" -> {url}")
        else:
            print(f"  MGL c. {chapter}" + (f", § {section}" if section else "") + f" -> No ValidURL found")
        # Note: URL validation may fail if we don't actually check, but structure should be correct
        # assert expected_url in url or url == MA_CODES["base_url"], f"URL mismatch for MGL c. {chapter}" + (f", § {section}" if section else "")


@pytest.mark.skip(reason="Requires live CourtListener citation lookup")
def test_filter_raw_citations():
    raw_citations = ['Alameda Newspapers, Inc. v. City of Oakland, 95 Cal. App. 4th 668 (2002) (elements include an agreement, wrongful act, and resultant damages)',
                     'inteliClear,LLC v ETC Glob.holdings,Inc 978 F.3d 653', '42 U.S.C. § 1983', 'Cal. Civ. Code § 1714']
    raw_citations = ["Bell Atl. Corp. v. Twombly, 550 U.S. 544, 555 (2007) (holding that a plaintiff's obligation to provide the 'grounds' of his 'entitle[ment] to relief' requires more than labels and conclusions, and a formulaic recitation of the elements of a cause of action will not do).", 
                      "Ashcroft v. Iqbal, 556 U.S. 662, 678 (2009) (holding that a complaint must contain sufficient factual matter, accepted as true, to state a claim to relief that is plausible on its face)."]
    raw_citations = ['''HomeLight's false advertising claim under 15 U.S.C. § 1125(a) should be dismissed due to its failure to allege a substantive causal connection between HomeOpenly's purported false advertising and any actual injury to HomeLight. To establish such a claim, a plaintiff must demonstrate both causation and actual harm resulting from the alleged false advertisements. Here, HomeLight merely asserts potential losses of business and goodwill without specific allegations linking this purported loss to HomeOpenly's conduct (Compl. ¶ 72).

The complaint lacks specific instances where HomeLight alleges its business suffered directly from HomeOpenly's statements. Although it claims the statements were intended to deceive real estate agents and potential clients (Compl. ¶ 69), there is no factual support or examples demonstrating that such alleged deception resulted in a tangible loss. Such conjecture fails to satisfy the legal requirements for false advertising claims under 15 U.S.C. § 1125(a), as claims must be more than speculative or conclusory.''']
    
    raw_citations = ['''The Plaintiff, HomeLight, Inc., has inadequately supported its trademark infringement claims against Defendants Dmitry Shkipin and HomeOpenly, Inc. under 15 U.S.C. § 1114, due to the absence of concrete evidence of actual consumer confusion (Compl. ¶55-56). A critical element for such claims under the Lanham Act is demonstrating actual consumer confusion, which HomeLight has failed to do. See AMF, Inc. v. Sleekcraft Boats, 599 F.2d 341, 348-49 (9th Cir. 1979) (highlighting actual confusion as a critical consideration when assessing likelihood of confusion claims under the Lanham Act).''']
    
    raw_citations = [" In In re Facebook, Inc. Internet Tracking Litigation, 956 F.3d 589 (9th Cir. 2020), the court required demonstrable expectation of privacy specific to the nature and context of the collected data."]
    raw_citations = ["Following *Spokeo, Inc. v. Robins*, 578 U.S. 330, 342 (2016), a cognizable injury must be both concrete and particularized."]
    
    raw_citations = ["Tungsten Graphics, Inc. v. Deckers Outdoor Corp., 425 F. App’x 607, 610 (9th Cir. 2011)."]
    # raw_citations = ["William H. Helfand, Inc. v. Turner Marketing Corp. 241 F.3d 867, 873 (9th Cir. 2001) (misleading statement must influence purchasing decision)"]
    raw_citations = ['817 F.3d 755', '19 Cal.App.5th 224']
    raw_citations = ['286 F.3d 246, 260', '550 U.S. 544, 556 (2007)']
    raw_citations = ['194 L. Ed. 2d 635']
    raw_citations = ['164 Cal. App. 4th 1583']
    raw_citations = ['96 F.3d 1151']
    for raw in raw_citations:
        citation_info_filtered = filter_raw_citations(raw)
        print(citation_info_filtered)


@pytest.mark.skip(reason="Exploratory citation annotation test")
def test_insert_tags():
    batch_revised_citations_info = [{'prefix': '/CaseViewer', 'form': '960 F.2d 294', 'citationName': '960-F<dot>2d-294', 'clusterId': 9006520, 'displayName': 'Johnson & Johnson v. Smithkline Beecham Corp.\t960 F.2d 294', 'start_index': 408, 'end_index': 420, 'type': 'case'}, {'prefix': '/CaseViewer', 'form': '227 F.3d 489', 'citationName': '227-F<dot>3d-489', 'clusterId': 22001, 'displayName': "Pizza Hut, Inc. v. Papa John's International, Inc.\t227 F.3d 489", 'start_index': 1084, 'end_index': 1096, 'type': 'case'}]
    argument = '''**Failure to Demonstrate Material Impact of Alleged Misrepresentation on Consumer Decisions**

HomeLight, Inc.'s complaint fails to establish that the alleged false statements made by Defendants, Dmitry Shkipin and HomeOpenly, Inc., materially impacted consumer purchasing decisions, a requirement for a false advertising claim under the Lanham Act. See <i>Johnson & Johnson v. Smithkline Beecham Corp.</i>, 960 F.2d 294, 297 (2d Cir. 1992). The court has made clear that plaintiffs are obligated to show that the deception is likely to influence the purchasing decision. HomeLight has not done so. The complaint lacks specificity in connecting the alleged misrepresentations directly to consumer actions (Compl. ¶ 67-69). Without demonstrating a concrete alteration in consumer behavior, the materiality requirement is unmet, necessitating dismissal.

Furthermore, the complaint merely speculates on the potential effects of these false statements rather than presenting tangible evidence of altered consumer behavior. According to <i>Pizza Hut, Inc. v. Papa John's Int'l, Inc.</i>, 227 F.3d 489, 501 (5th Cir. 2000), it is imperative that plaintiffs provide evidence that the misleading information had actual material impact on consumer's purchasing decisions. HomeLight cites reputational harm and business loss (Compl. ¶ 72, 75) but fails to articulate how these alleged harms concretely influenced consumers' choices relative to HomeLight services.

There is also inconsistency regarding the dissemination and impact of these statements. Although the complaint notes the nationwide distribution of statements (Compl. ¶ 70-71), there is a significant period of content modification (Compl. ¶ 38-39) that undermines the claims of a consistent material impact. Additionally, despite referencing a cease and desist letter (Compl. ¶ 41), the complaint does not demonstrate how the defendants' refusal to remove content materially affected consumer behavior or business decisions.

Given these significant gaps, the argument that the alleged misrepresentations inherently influence consumer choices is speculative at best. HomeLight has not amended the complaint to address this critical missing element per Rule 12(b)(6). Without evidence that the alleged statements materially influenced consumer purchasing behavior, the false advertising claim under the Lanham Act remains unsupported and warrants dismissal.'''
    offst = 0
    annotated_result = argument
    for citation in batch_revised_citations_info:
        annotated_result, next_offset = insert_tags(annotated_result, citation, offst)
        offst += next_offset

    arg_citations = "William H. Helfand, Inc. v. Turner Marketing Corp. 241 F.3d 867, 873 (9th Cir. 2001) (misleading statement must influence purchasing decision)"



@pytest.mark.skip(reason="Requires live CourtListener cluster lookup")
def test_get_cluster():
    cID = 7
    cID = 435
    cID = 1847537
    cID = 8132550
    cluster_data = get_cluster(cID)
    print(cluster_data)
    print("cluster_data.keys: ", cluster_data.keys())
    print(cluster_data['docket_id'])
    print(cluster_data['slug'])
    print(cluster_data['case_name'])
    print(cluster_data['case_name_full'])




@pytest.mark.skip(reason="Requires live CourtListener docket/court lookup")
def test_get_docket_and_get_court():
    docketUrl = 'https://www.courtlistener.com/api/rest/v4/dockets/3045845/'
    docket_raw = get_docket(docketUrl)
    if docket_raw:
        courtUrl = docket_raw["court"]
        if courtUrl:
            court_raw = get_court(courtUrl)
            if court_raw:
                court_cit_str = court_raw["citation_string"]
                print(court_cit_str)


@pytest.mark.skip(reason="Requires live CourtListener opinion lookup")
def test_get_opinion():
    """Test function to fetch opinion data and see what legal proposition info is available."""
    # Example opinion URL from Payne v. Progressive Financial Services case
    # opinion_url = "https://www.courtlistener.com/api/rest/v4/opinions/2669095/"
    opinion_url = "https://www.courtlistener.com/api/rest/v4/opinions/112568/"
    
    print("=" * 80)
    print("Fetching opinion from:", opinion_url)
    print("=" * 80)
    
    opinion_data = get_opinion(opinion_url)
    
    if opinion_data:
        return opinion_data
    else:
        print("Failed to fetch opinion data")
        return None


def test_extract_holding_returns_empty_string_when_completion_is_unavailable(monkeypatch):
    from backend.ai import base as ai_base

    def fake_completion(prompt):
        assert "Court affirmed the judgment." in prompt
        return None

    monkeypatch.setattr(ai_base, "chat_completion", fake_completion)

    assert extract_holding_from_opinion({
        "html_with_citations": "Court affirmed the judgment.",
    }) == ""


def test_extract_case_synopsis_uses_one_typed_call_and_shared_excerpt_limit(monkeypatch):
    from backend.ai import base as ai_base

    parsed = fl_tools.OpinionCaseSynopsis(
        holding="The court affirmed dismissal.",
        case_overview="The court affirmed dismissal after considering the stated facts.",
    )
    parse_call = MagicMock(return_value=parsed)
    monkeypatch.setattr(ai_base, "chat_completion_parse", parse_call)
    excerpt = "A" * fl_tools.OPINION_EXCERPT_CHAR_LIMIT

    result = fl_tools.extract_holding_and_case_overview_from_opinion({
        "html_with_citations": excerpt + "TAIL_SENTINEL",
        "html": "LOWER_PRIORITY_SENTINEL",
    })

    assert result is parsed
    parse_call.assert_called_once()
    prompt, response_model = parse_call.call_args.args
    assert response_model is fl_tools.OpinionCaseSynopsis
    assert "Opinion excerpt:" in prompt
    assert excerpt in prompt
    assert "TAIL_SENTINEL" not in prompt
    assert "LOWER_PRIORITY_SENTINEL" not in prompt
    assert parse_call.call_args.kwargs == {
        "model": ai_base.DEFAULT_MODEL,
        "reasoning_effort": None,
    }


def test_extract_case_synopsis_provider_path_uses_sol_medium(monkeypatch):
    from backend.ai import base as ai_base

    parsed = fl_tools.OpinionCaseSynopsis(holding="The court affirmed.", case_overview="A short overview.")
    provider_parse = MagicMock(return_value=SimpleNamespace(
        id="resp-case-synopsis",
        status="completed",
        output=[],
        output_parsed=parsed,
    ))
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=provider_parse)),
    )
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", MagicMock())
    monkeypatch.setattr(ai_base, "record_guided_llm_response", MagicMock())

    result = fl_tools.extract_holding_and_case_overview_from_opinion({
        "plain_text": "The court affirmed dismissal.",
    })

    assert result is parsed
    provider_parse.assert_called_once()
    request = provider_parse.call_args.kwargs
    assert set(request) == {"model", "input", "text_format", "reasoning"}
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text_format"] is fl_tools.OpinionCaseSynopsis
    assert request["input"][1]["role"] == "user"
    assert "The court affirmed dismissal." in request["input"][1]["content"]


@pytest.mark.parametrize(
    ("opinion", "selected_text"),
    [
        ({"html_with_citations": "CITED", "html": "HTML", "plain_text": "PLAIN"}, "CITED"),
        ({"html_with_citations": "", "html": "HTML", "plain_text": "PLAIN"}, "HTML"),
        ({"html_with_citations": "", "html": "", "plain_text": "PLAIN"}, "PLAIN"),
    ],
)
def test_extract_case_synopsis_preserves_opinion_text_fallback(monkeypatch, opinion, selected_text):
    from backend.ai import base as ai_base

    parse_call = MagicMock(return_value=fl_tools.OpinionCaseSynopsis(holding="H", case_overview="O"))
    monkeypatch.setattr(ai_base, "chat_completion_parse", parse_call)

    fl_tools.extract_holding_and_case_overview_from_opinion(opinion)

    assert f"Opinion excerpt:\n{selected_text}" in parse_call.call_args.args[0]


def test_extract_case_synopsis_returns_none_without_text_or_parsed_result(monkeypatch):
    from backend.ai import base as ai_base

    parse_call = MagicMock(return_value=None)
    monkeypatch.setattr(ai_base, "chat_completion_parse", parse_call)

    assert fl_tools.extract_holding_and_case_overview_from_opinion(None) is None
    assert fl_tools.extract_holding_and_case_overview_from_opinion({"html": ""}) == (
        fl_tools.OpinionCaseSynopsis(holding="", case_overview="")
    )
    parse_call.assert_not_called()
    assert fl_tools.extract_holding_and_case_overview_from_opinion({"plain_text": "Opinion"}) is None
    parse_call.assert_called_once()


def test_holding_only_helper_still_returns_plain_string(monkeypatch):
    from backend.ai import base as ai_base

    completion_call = MagicMock(return_value="The court affirmed dismissal.")
    monkeypatch.setattr(ai_base, "chat_completion", completion_call)

    result = extract_holding_from_opinion({"plain_text": "The court affirmed dismissal."})

    assert result == "The court affirmed dismissal."
    assert isinstance(result, str)
    assert "Below is an excerpt" in completion_call.call_args.args[0]


@pytest.mark.skip(reason="Requires live CourtListener opinion lookup and LLM call")
def test_extract_holding():
    """Test extracting holding from an opinion."""
    # First get the opinion data
    opinion_data = test_get_opinion()
    
    if opinion_data:
        # Extract the holding
        holding = extract_holding_from_opinion(opinion_data)
        
        print("\n" + "=" * 80)
        print("=== EXTRACTED HOLDING ===")
        print("=" * 80)
        print(holding)
        print("=" * 80)
        
        return holding
    else:
        print("Could not extract holding - no opinion data")
        return None



@pytest.mark.skip(reason="Requires live CourtListener citation lookup")
def test_filter_on_casename():
    print("test_filter_on_casename")
    # Create a dict with case_name and citation keys (like LLM would return)
    raw_cit = {
        'case_name': 'General Elec. Co. v. Johnson', 
        'citation': '595 F. Supp. 2d 8, 17 (D.D.C. 2009).'
    }
    # Filter the citation string using FreeLaw API
    filtered_citation_info = filter_raw_citations(raw_cit['citation'])
    print("filtered_citation_info: ", filtered_citation_info)
    # Now filter based on case name match
    filtered_citation_info_filtered = filter_on_casename(raw_cit, filtered_citation_info)
    print("filtered_citation_info_filtered: ", filtered_citation_info_filtered)



from backend.util.free_law_tools import semantic_search_get, semantic_search_post

@pytest.mark.skip(reason="Requires live CourtListener semantic search")
def test_semantic_search():
    print("test_semantic_search")
#     query = """I've brought a personal injury case in Idaho. Idaho Code § 6-1603 caps non-economic damages in personal injury (including wrongful death) cases, but provides an exception: the cap does not apply where the cause of action arises out of “willful or reckless misconduct.” Please find cases where the plaintiff showed “reckless” or “willful and wanton” misconduct sufficient to avoid/reach over Idaho’s cap on non-economic damages under Idaho Code § 6-1603. Give as much detail as possible on the specific facts that supported the finding of “reckless” or “willful and wanton” misconduct. Ensure all cases are real cases. Provide citations in bluebook format. Focus on post-2003 cases (after the 2003 amendment to § 6-1603) that involve personal injury or wrongful death. You can include appellate decisions as well as trial court opinions and jury verdicts.
# In your response, please also provide legal citations, like case names, published reporter citations, and years filed. i want to know the Relevant Cases & Statutes. Please cite them. if you can not, please explain why.
# # """
    query = """"Idaho Code 6-1603" reckless OR "willful and wanton" damages cap after:2003"""
    query = """Hayward v. Jack's Pharmacy, Inc."""
    result_list = semantic_search_get(query)
    print(len(result_list))
    for each in result_list[:5]:
        print(each)
        print()
    # post_result = semantic_search_post(result)
    # print("post_result: ", post_result)



# test_find_citations()
# test_find_citations_retries()
# test_get_casetext_url()
# test_filter_raw_citations()

# test_insert_tags()
# test_get_cluster()

# test_get_docket_and_get_court()
# instr = "629 F.Supp. 2d 1171, 1183"
# instr = "Hayward v. Jack's Pharmacy, Inc., 167 Idaho 70, 467 P.3d 398 (2020) "
# instr = "Seppi v. Betty, 99 Idaho 186, 579 P.2d 683 (1978) (Idaho Supreme Court)"
# instr = "Cenarrusa v. Andrews, No. CV-OC-2013-11363, 2015 WL 13744965 (Idaho Dist. Ct. June 3, 2015) (Idaho state trial court, summary judgment)"
# instr = """Relevant Cases & Statutes

# Hayward v. Jack's Pharmacy, Inc., 167 Idaho 70, 467 P.3d 398 (2020) (summary judgment, Idaho Supreme Court)
# (Plaintiff claimed pharmacy's dispensing errors—repeatedly filling prescriptions incorrectly for a minor patient—amounted to willful or reckless misconduct. Idaho Supreme Court clarified that even where multiple errors were made, evidence did not rise to the level of willful or reckless misconduct, absent proof of conscious disregard for patient safety—it was, at most, gross negligence. Summary judgment affirming application of the damages cap.)
# Holding: Evidence must show more than multiple failures or negligence, but rather awareness and disregard of a serious and probable risk of harm.
# Seppi v. Betty, 99 Idaho 186, 579 P.2d 683 (1978) (Idaho Supreme Court)
# While pre-2003, repeatedly cited post-2003 as controlling. (Describes "willful and wanton," and "reckless" as conduct indicating a conscious disregard for safety, adopting RESTATEMENT (SECOND) OF TORTS § 500.)
# Holding: Mere inadvertence, lack of due care, or even gross negligence, does not satisfy the standard.
# Cenarrusa v. Andrews, No. CV-OC-2013-11363, 2015 WL 13744965 (Idaho Dist. Ct. June 3, 2015) (Idaho state trial court, summary judgment)
# (Defendant struck and killed pedestrian; plaintiff claimed conduct was willful or reckless due to excessive speed and inattentiveness. Court held that speeding and inattentive driving did not, without more (e.g., racing, intoxication, or intentional conduct), meet the threshold for “reckless” under § 6-1603. Damages cap applied.)
# Key fact: Absence of proof of knowledge of a specific imminent risk and conscious disregard thereof.
# Eldridge v. West, 166 Idaho 303, 458 P.3d 172 (2020) (Idaho Supreme Court)
# (While primarily a punitive damages case, adopted the same standard: "reckless constitutes a conscious disregard of a known or obvious risk," requiring more than gross negligence. Relevant because courts making § 6-1603(4) assessments cite this definition.)
# Rogers v. Smith, 143 Idaho 391, 146 P.3d 258 (Ct. App. 2006) (jury verdict, motion for new trial)
# (Car accident case. Plaintiff argued for punitive and uncapped non-economic damages due to alleged reckless driving—speeding, running stop sign. The court held that ordinary traffic violations are not sufficient; the evidence must show actual knowledge and disregard of high probability of harm.)
# Statute: Idaho Code § 6-1603 (Idaho Legislature, 2003; as amended)"""

# instr = "Cal. Bus. & Prof. Code § 17500      Cal. Civ. Code § 1714"
# instr = "88 Cal.App.4th 490"
# instr = "105 Cal. Rptr. 2d 490"
# instr = "payne v. progressive. 748 F.3d. 605"
# result = find_citations(instr)

# result_str = ""
# for each in result:
#     print(each)
#     # for each result json, we want to get 'citation', 'start_index', 'end_index', the first item in 'clusters' and its
#     # 'resource_uri', 'absolute_url', 'case_name', 'case_name_full', 'date_filed', 'judges', and connect these value with their name into a single string
#     citation = each['citation']
#     start_index = each['start_index']
#     end_index = each['end_index']
#     cluster = each['clusters'][0]
#     resource_uri = cluster['resource_uri']
#     absolute_url = cluster['absolute_url']
#     case_name = cluster['case_name']

#     result_str += f"citation:{citation}\n start_index: {start_index}, end_index: {end_index}\n resource_uri: {resource_uri}\n absolute_url: {absolute_url}\n case_name: {case_name}\n"
#     result_str += "\n"
#     print(result_str)



# from backend.util.free_law_tools import find_casename

# # print(find_casename("Osborn v. Salmon River Propane, LLC"))
# print(find_casename("Carrillo v. Boise Tire Co."))
# print(find_casename("Whitted v. Canyon County"))
# # print(find_casename("Barry v. PacifiCorp"))
# print(find_casename("Perry v. Magic Valley Regional Medical Center"))

instr = "31 Cal. 4th 864"   
instr = "152 F.3d 1161"
instr = "55 Cal. 4th 747"
instr  = '''
Core Ninth Circuit cases:
[[CASE_START]]Fonovisa, Inc. v. Cherry Auction, Inc., 76 F.3d 259 (9th Cir. 1996).
[[CASE_START]]A&M Records, Inc. v. Napster, Inc., 239 F.3d 1004 (9th Cir. 2001).
[[CASE_START]]Perfect 10, Inc. v. Amazon.com, Inc., 508 F.3d 1146 (9th Cir. 2007).
[[CASE_START]]Perfect 10, Inc. v. Visa Int’l Serv. Ass’n, 494 F.3d 788 (9th Cir. 2007).
[[CASE_START]]Perfect 10, Inc. v. Giganews, Inc., 847 F.3d 657 (9th Cir. 2017).
The Supreme Court’s inducement framework is:
[[CASE_START]]MGM Studios Inc. v. Grokster, Ltd., 545 U.S. 913 (2005).
[[CASE_START]]MGM Studios Inc. v. Grokster, Ltd., 545 U.S. 913 (2005).
Direct infringement requirement
Contributory liability is secondary; there must be underlying direct infringement by someone:
[[CASE_START]]A&M Records, Inc. v. Napster, Inc., 239 F.3d 1004, 1013–14 (9th Cir. 2001) (“Secondary liability for copyright infringement does not exist in the absence of direct infringement by a third party.”).
For your situation (users uploading allegedly infringing content to a landing page), the plaintiffs must plausibly allege that specific works were copied, distributed, or publicly displayed without authorization by site users.
Knowledge requirement
A. Actual or constructive knowledge
The Ninth Circuit requires that the defendant “knew or had reason to know” of the specific infringing activity:
[[CASE_START]]Fonovisa, Inc. v. Cherry Auction, Inc., 76 F.3d 259, 264 (9th Cir. 1996).
[[CASE_START]]A&M Records, Inc. v. Napster, Inc., 239 F.3d 1004, 1020 (9th Cir. 2001).
'''

instr = '''CUTSA provides the substantive law for trade secret claims in California. In the NDCA, claims under CUTSA are often adjudicated at the motion to dismiss or summary judgment stage, where courts closely scrutinize the specificity with which the information is identified and the facts demonstrating misappropriation (see, e.g., [[CASE_START]] Imax Corp. v. Cinema Technologies, Inc., 152 F.3d 1161 (9th Cir. 1998)). Federal courts sitting in diversity apply Federal Rules of Civil Procedure for pleading and discovery but rely on California trade secret law for substantive standards ([[CASE_START]] Sargon Enterprises, Inc. v. Univ. of S. California, 55 Cal. 4th 747 (2012)).
Controlling Legal Standards
Existence of a Trade Secret: Under Cal. Civ. Code § 3426.1(d), a trade secret is “information…that: (1) derives independent economic value…from not being generally known… and (2) is the subject of efforts that are reasonable under the circumstances to maintain its secrecy.” Courts require particularity in identifying the alleged secret; broad or vague descriptions invite dismissal or defeat at summary judgment ([[CASE_START]]Diodes, Inc. v. Franzen, 260 Cal. App. 2d 244 (1968)).
'''
if False:
    lex_results = filter_raw_citations(instr)
    print("filter_raw_citations results: ")
    print(lex_results)

    if lex_results:
        # final_response = annotate_text_with_citations(final_response, cite_info)
        final_response = annotate_text_with_citations_multiple(instr, lex_results)
        print("annotated_result: ")
        print(final_response)

# Uncomment to test Wisconsin statutes
# test_wisconsin_statutes()

@pytest.mark.skip(reason="Requires live CourtListener/state citation lookup")
def test_wisconsin_in_filter_raw_citations():
    """Test that Wisconsin statutes are detected in filter_raw_citations."""
    from backend.util.free_law_tools import filter_raw_citations
    
    test_text = """
    The plaintiff's claim under Wis. Stat. § 100.18 for fraudulent representations fails because 
    the statements were not material. Additionally, the defendant complied with Wis. Stat. § 111.31 
    regarding fair employment practices. The warranty claim under Wis. Stat. § 402.314 also fails.
    """
    
    results = filter_raw_citations(test_text)
    print("\n=== Wisconsin statutes in filter_raw_citations ===")
    print(f"Found {len(results)} total citations:")
    for result in results:
        print(f"  Type: {result['type']}, Citation: {result.get('displayName', result.get('citationName', 'N/A'))}")
        if result['type'] == 'state':
            print(f"    URL: {result['prefix']}")
    
    # Check that we found at least some Wisconsin citations
    wisconsin_citations = [r for r in results if r['type'] == 'state' and 'Wis' in r.get('displayName', '')]
    print(f"\nFound {len(wisconsin_citations)} Wisconsin statute citations")
    assert len(wisconsin_citations) > 0, "Should find at least one Wisconsin citation"


@pytest.mark.skip(reason="Requires live CourtListener/state citation lookup")
def test_illinois_in_filter_raw_citations():
    """Test that Illinois statutes are detected in filter_raw_citations."""
    from backend.util.free_law_tools import filter_raw_citations
    
    test_text = """
    The plaintiff's claim under Ill. Comp. Stat. § 100.18 for fraudulent representations fails because 
    the statements were not material. Additionally, the defendant complied with Ill. Comp. Stat. § 111.31 
    regarding fair employment practices. The warranty claim under Ill. Comp. Stat. § 402.314 also fails.
    """
    
    results = filter_raw_citations(test_text)
    print("\n=== Illinois statutes in filter_raw_citations ===")
    print(f"Found {len(results)} total citations:")
    for result in results:
        print(f"  Type: {result['type']}, Citation: {result.get('displayName', result.get('citationName', 'N/A'))}")
        if result['type'] == 'state':
            print(f"    URL: {result['prefix']}")


@pytest.mark.skip(reason="Requires live CourtListener/state citation lookup")
def test_new_york_in_filter_raw_citations():
    """Test that New York statutes are detected in filter_raw_citations."""
    from backend.util.free_law_tools import filter_raw_citations
    
    test_text = """
    The plaintiff's claim under N.Y. Gen. Bus. Law § 349 for deceptive practices fails because 
    the statements were not material. Additionally, the defendant complied with N.Y. C.P.L.R. § 3012 
    regarding service of pleadings. The contract claim under N.Y. Gen. Oblig. Law § 5-701 also fails.
    """
    
    results = filter_raw_citations(test_text)
    print("\n=== New York statutes in filter_raw_citations ===")
    print(f"Found {len(results)} total citations:")
    for result in results:
        print(f"  Type: {result['type']}, Citation: {result.get('displayName', result.get('citationName', 'N/A'))}")
        if result['type'] == 'state':
            print(f"    URL: {result['prefix']}")
    
    # Check that we found at least some New York citations
    new_york_citations = [r for r in results if r['type'] == 'state' and 'N.Y.' in r.get('displayName', '')]
    print(f"\nFound {len(new_york_citations)} New York statute citations")
    assert len(new_york_citations) > 0, "Should find at least one New York citation"


@pytest.mark.skip(reason="Requires live CourtListener/state citation lookup")
def test_massachusetts_in_filter_raw_citations():
    """Test that Massachusetts statutes are detected in filter_raw_citations."""
    from backend.util.free_law_tools import filter_raw_citations
    
    test_text = """
    The plaintiff's claim under MGL c. 93A, § 2 for unfair trade practices fails because 
    the conduct was not deceptive. Additionally, the defendant complied with Mass. Gen. Laws c. 269, § 10 
    regarding firearm regulations. The consumer protection claim under G.L. c. 93A, § 9 also fails.
    """
    
    results = filter_raw_citations(test_text)
    print("\n=== Massachusetts statutes in filter_raw_citations ===")
    print(f"Found {len(results)} total citations:")
    for result in results:
        print(f"  Type: {result['type']}, Citation: {result.get('displayName', result.get('citationName', 'N/A'))}")
        if result['type'] == 'state':
            print(f"    URL: {result['prefix']}")
    
    # Check that we found at least some Massachusetts citations
    massachusetts_citations = [r for r in results if r['type'] == 'state' and ('MGL' in r.get('displayName', '') or 'Mass' in r.get('displayName', '') or 'G.L.' in r.get('displayName', ''))]
    print(f"\nFound {len(massachusetts_citations)} Massachusetts statute citations")
    assert len(massachusetts_citations) > 0, "Should find at least one Massachusetts citation"


@pytest.mark.skip(reason="Exploratory state-code live/debug test")
def test_washington_statutes():
    """Test Washington state statute (RCW) detection and URL generation."""
    from backend.util.state_codes import find_state_codes, get_wa_code_url, WA_CODES
    
    # Test various Washington statute citations
    test_texts = [
        "The defendant violated RCW 19.86.020 by engaging in unfair business practices.",
        "Under Wash. Rev. Code § 46.61.502, driving under the influence is prohibited.",
        "RCW 9A.36.021 defines assault in the second degree.",
        "The Consumer Protection Act, RCW 19.86.090, provides for private actions.",
        "R.C.W. § 79A.25.030 governs state parks.",
        # Test the three main citation formats
        "Wash. Rev. Code § 19.86.020 is the consumer protection statute.",
        "RCW 19.86.020 prohibits unfair practices.",
        "RCW § 19.86.020 provides the cause of action."
    ]
    
    for test_text in test_texts:
        print(f"\nTesting: {test_text}")
        results = find_state_codes(test_text)
        print(f"Found {len(results)} Washington citations:")
        for result in results:
            print(f"  Citation: {result['displayName']}")
            print(f"  URL: {result['prefix']}")
            print(f"  Type: {result['type']}")
    
    # Test direct URL generation
    print("\n=== Testing direct URL generation ===")
    test_cases = [
        ("19.86.020", "https://app.leg.wa.gov/RCW/default.aspx?cite=19.86.020"),
        ("46.61.502", "https://app.leg.wa.gov/RCW/default.aspx?cite=46.61.502"),
        ("9A.36.021", "https://app.leg.wa.gov/RCW/default.aspx?cite=9A.36.021"),
        ("79A.25.030", "https://app.leg.wa.gov/RCW/default.aspx?cite=79A.25.030"),
    ]
    
    for cite, expected_url in test_cases:
        url = get_wa_code_url(WA_CODES, cite)
        if url:
            print(f"  RCW {cite} -> {url}")
        else:
            print(f"  RCW {cite} -> No valid URL found")
        # Note: URL validation may fail if we don't actually check, but structure should be correct


@pytest.mark.skip(reason="Requires live CourtListener/state citation lookup")
def test_washington_in_filter_raw_citations():
    """Test that Washington statutes are detected in filter_raw_citations."""
    from backend.util.free_law_tools import filter_raw_citations
    
    test_text = """
    The plaintiff's claim under RCW 19.86.020 for unfair business practices fails because 
    the conduct was not deceptive. Additionally, the defendant complied with Wash. Rev. Code § 46.61.502 
    regarding driving regulations. The state parks claim under RCW 79A.25.030 also fails.
    """
    
    results = filter_raw_citations(test_text)
    print("\n=== Washington statutes in filter_raw_citations ===")
    print(f"Found {len(results)} total citations:")
    for result in results:
        print(f"  Type: {result['type']}, Citation: {result.get('displayName', result.get('citationName', 'N/A'))}")
        if result['type'] == 'state':
            print(f"    URL: {result['prefix']}")
    
    # Check that we found at least some Washington citations
    washington_citations = [r for r in results if r['type'] == 'state' and ('RCW' in r.get('displayName', '') or 'Wash' in r.get('displayName', ''))]
    print(f"\nFound {len(washington_citations)} Washington statute citations")
    assert len(washington_citations) > 0, "Should find at least one Washington citation"

@pytest.mark.skip(reason="Exploratory state-code live/debug test")
def test_oregon_statutes():
    """Test Oregon state statute (ORS) detection and URL generation."""
    from backend.util.state_codes import find_state_codes, get_or_code_url, OR_CODES
    
    # Test various Oregon statute citations
    test_texts = [
        "The defendant violated ORS 646.608 by engaging in unlawful trade practices.",
        "Under Or. Rev. Stat. § 30.275, notice of claim is required.",
        "ORS § 659A.030 prohibits employment discrimination.",
        "The Oregon Tort Claims Act, ORS 30.260, limits liability.",
        "Or. Rev. Stat. Ann. § 20.080 governs attorney fees.",
        # Test the three main citation formats
        "Or. Rev. Stat. § 646.608 is the trade practices statute.",
        "ORS 30.020 provides for wrongful death actions.",
        "ORS § 161.095 defines criminal liability requirements."
    ]
    
    for test_text in test_texts:
        print(f"\nTesting: {test_text}")
        results = find_state_codes(test_text)
        print(f"Found {len(results)} Oregon citations:")
        for result in results:
            print(f"  Citation: {result['displayName']}")
            print(f"  URL: {result['prefix']}")
            print(f"  Type: {result['type']}")
    
    # Test direct URL generation
    print("\n=== Testing direct URL generation ===")
    test_cases = [
        ("30", "https://www.oregonlegislature.gov/bills_laws/ors/ors030.html"),
        ("646", "https://www.oregonlegislature.gov/bills_laws/ors/ors646.html"),
        ("646A", "https://www.oregonlegislature.gov/bills_laws/ors/ors646a.html"),
        ("838", "https://www.oregonlegislature.gov/bills_laws/ors/ors838.html"),
    ]
    
    for chapter, expected_url in test_cases:
        url = get_or_code_url(OR_CODES, chapter)
        if url:
            print(f"  ORS {chapter} -> {url}")
        else:
            print(f"  ORS {chapter} -> No valid URL found")


@pytest.mark.skip(reason="Requires live CourtListener/state citation lookup")
def test_oregon_in_filter_raw_citations():
    """Test that Oregon statutes are detected in filter_raw_citations."""
    from backend.util.free_law_tools import filter_raw_citations
    
    test_text = """
    The plaintiff's claim under ORS 646.608 for unlawful trade practices fails because 
    the conduct was not deceptive. Additionally, the defendant complied with Or. Rev. Stat. § 30.275 
    regarding notice of claim. The tort claims under ORS 30.260 also fails.
    """
    
    results = filter_raw_citations(test_text)
    print("\n=== Oregon statutes in filter_raw_citations ===")
    print(f"Found {len(results)} total citations:")
    for result in results:
        print(f"  Type: {result['type']}, Citation: {result.get('displayName', result.get('citationName', 'N/A'))}")
        if result['type'] == 'state':
            print(f"    URL: {result['prefix']}")
    
    # Check that we found at least some Oregon citations
    oregon_citations = [r for r in results if r['type'] == 'state' and ('ORS' in r.get('displayName', '') or 'Or.' in r.get('displayName', ''))]
    print(f"\nFound {len(oregon_citations)} Oregon statute citations")
    assert len(oregon_citations) > 0, "Should find at least one Oregon citation"


# Run the test
# test_get_opinion()
# test_extract_holding()

# test_filter_on_casename()
# test_semantic_search()
# test_wisconsin_statutes()
# test_wisconsin_in_filter_raw_citations()

# test_illinois_in_filter_raw_citations()
# test_new_york_statutes()

# test_new_york_in_filter_raw_citations()

# test_massachusetts_statutes()
# test_massachusetts_in_filter_raw_citations()

# test_washington_statutes()
# test_washington_in_filter_raw_citations()

# test_oregon_statutes()
# test_oregon_in_filter_raw_citations()
