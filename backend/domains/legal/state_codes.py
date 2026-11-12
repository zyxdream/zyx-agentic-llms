import json
import re
import requests
import logging
import re
from typing import Optional, Tuple


# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def check_url_status(url):
    """Check if a URL returns a valid status code."""
    try:
        response = requests.get(url, timeout=5)  # Set a timeout to handle unresponsive URLs
        if response.status_code == 200:
            print(f"The URL is valid and returned status code {response.status_code}.")
        else:
            print(f"The URL returned status code {response.status_code}.")
        return response.status_code
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")
        return 404


def file2state_patterns(fn: str) -> list:
    """Load state code patterns from a JSON file."""
    with open(fn, 'r') as f:
        state_codes = json.load(f)
    return [re.compile(each + state_codes["number_pattern"]) for each in state_codes["code_patterns"]], state_codes


def file2CA_patterns(filepath: str) -> list:
    """Load California/New York style code patterns from a JSON file."""
    with open(filepath, 'r') as f:
        california_codes = json.load(f)
    code_patterns = []
    section_abbrs = []
    for each_code, section_abbr in california_codes["code_patterns"].items():
        code_patterns.append(re.compile(each_code + california_codes["number_pattern"]))
        section_abbrs.append(section_abbr)

    return code_patterns, section_abbrs, california_codes


# Load state code patterns
AZ_STATUES_PATTERNS, AZ_CODES = file2state_patterns("backend/util/state_statues/Arizona")
CA_STATUES_PATTERNS, CA_ABBRS, CA_CODES = file2CA_patterns("backend/util/state_statues/California")
WI_STATUES_PATTERNS, WI_CODES = file2state_patterns("backend/util/state_statues/Wisconsin")
IL_STATUES_PATTERNS, IL_CODES = file2state_patterns("backend/util/state_statues/Illinois")
NY_STATUES_PATTERNS, NY_ABBRS, NY_CODES = file2CA_patterns("backend/util/state_statues/NY")


# Load MA patterns like the others
MA_STATUES_PATTERNS, MA_CODES = file2state_patterns("backend/util/state_statues/MA")

# Load WA (Washington) patterns
WA_STATUES_PATTERNS, WA_CODES = file2state_patterns("backend/util/state_statues/Washington")

# Load OR (Oregon) patterns
OR_STATUES_PATTERNS, OR_CODES = file2state_patterns("backend/util/state_statues/Oregon")

# ---- Chapter -> Part/Title mapper (simple: N or N + single letter) ----

CHAPTER_RANGES = [
    ("PartI",  "TitleI",    "1",   "5"),
    ("PartI",  "TitleII",   "6",   "28A"),
    ("PartI",  "TitleIII",  "29",  "30B"),
    ("PartI",  "TitleIV",   "31",  "32B"),
    ("PartI",  "TitleV",    "33",  "33A"),
    ("PartI",  "TitleVI",   "34",  "38"),
    ("PartI",  "TitleVII",  "39",  "49A"),
    ("PartI",  "TitleVIII", "50",  "57"),
    ("PartI",  "TitleIX",   "58",  "65C"),
    ("PartI",  "TitleX",    "66",  "66A"),
    ("PartI",  "TitleXI",   "67",  "68A"),
    ("PartI",  "TitleXII",  "69",  "78A"),
    ("PartI",  "TitleXIII", "79",  "80A"),
    ("PartI",  "TitleXIV",  "81",  "92A"),  # tail ignored by parser below
    ("PartI",  "TitleXV",   "93",  "110H"),
    ("PartI",  "TitleXVI",  "111", "114"),
    ("PartI",  "TitleXVII", "115", "123B"),
    ("PartI",  "TitleXVIII","124", "127"),
    ("PartI",  "TitleXIX",  "128", "132B"),
    ("PartI",  "TitleXX",   "133", "148A"),
    ("PartI",  "TitleXXI",  "149", "154"),
    ("PartI",  "TitleXXII", "155", "182"),
    ("PartII", "TitleI",    "183", "189"),
    ("PartII", "TitleII",   "190", "206"),
    ("PartII", "TitleIII",  "207", "210"),
    ("PartIII","TitleI",    "211", "222"),
    ("PartIII","TitleII",   "223", "236"),
    ("PartIII","TitleIII",  "237", "245"),
    ("PartIII","TitleIV",   "246", "258F"),
    ("PartIII","TitleV",    "259", "260"),
    ("PartIII","TitleVI",   "261", "262"),
    ("PartIV", "TitleI",    "263", "274"),
    ("PartIV", "TitleII",   "275", "280"),
    ("PartV",  "TitleII",   "281", "281"),
]

_CH_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]?)")

def _chapter_key(ch: str) -> Tuple[int, int]:
    """
    Supports only: digits + optional single letter.
    Examples: 28 -> (28,0), 28A -> (28,1), 110H -> (110,8)
    Ignores any extra tail beyond the first optional letter.
    """
    m = _CH_RE.match(ch)
    if not m:
        raise ValueError(f"Bad chapter: {ch!r}")
    base = int(m.group(1))
    letter = (m.group(2) or "").upper()
    suffix = (ord(letter) - ord("A") + 1) if letter else 0
    return (base, suffix)

def part_title_for_chapter(chapter: str) -> Optional[str]:
    ck = _chapter_key(chapter)
    # print("ck: ", ck)
    for part, title, start, end in CHAPTER_RANGES:
        # print("part: ", part, "title: ", title, "start: ", start, "end: ", end)
        if _chapter_key(start) <= ck <= _chapter_key(end):
            return f"{part}/{title}"
    return None


def get_ma_code_url(ma_codes: dict, chapter: str, section: Optional[str] = None) -> str:
    """
    chapter: like '269' or '93A'
    section: like '10' or '2A' (optional)
    """
    part_title = part_title_for_chapter(chapter)
    print("part_title: ", part_title)
    if not part_title:
        return ma_codes["base_url"]

    base = ma_codes["base_url"].rstrip("/") + "/"

    if section:
        new_item_url = f"{base}{part_title}/Chapter{chapter}/Section{section}"
    else:
        # Chapter-only landing page
        new_item_url = f"{base}{part_title}/Chapter{chapter}"

    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None



def get_cal_code_url(california_codes: dict, section_abbr: str, section_num: str):
    """Generate URL for California statute."""
    new_item_url = california_codes["item_url"].replace(california_codes["to_replace_sec"], section_abbr).replace(california_codes["to_replace_num"], section_num)
    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None


def get_ariz_code_url(arizona_codes: dict, section_code: str, section_num: str):
    """Generate URL for Arizona statute."""
    # number patterns: 00012   or   00012-11  in url format
    if '.' in section_num:
        num1, num2 = section_num.split('.')
        # pad the first part of number to 5 digits
        pad_num1 = num1.zfill(5)
        padded_section_num = '-'.join([pad_num1, num2])
    else:
        padded_section_num = section_num.zfill(5)

    new_item_url = arizona_codes["item_url"].replace(arizona_codes["to_replace_sec"], section_code).replace(arizona_codes["to_replace_num"], padded_section_num)
    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None


def get_wis_code_url(wisconsin_codes: dict, chapter_section: str):
    """
    Generate URL for Wisconsin statute.
    chapter_section can be:
    - "111.31" -> chapter 111, section 31 -> /statutes/statutes/111/31
    - "196" -> chapter 196 only -> /statutes/statutes/196
    """
    if '.' in chapter_section:
        # Has both chapter and section
        chapter, section = chapter_section.split('.', 1)
        new_item_url = wisconsin_codes["item_url"].replace(wisconsin_codes["to_replace_chapter"], chapter).replace(wisconsin_codes["to_replace_section"], section)
    else:
        # Chapter only
        chapter = chapter_section
        new_item_url = wisconsin_codes["base_url"] + "/" + chapter

    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None


def get_il_code_url(illinois_codes: dict):
    """Generate URL for Illinois statute (returns base URL only)."""
    return illinois_codes["base_url"]


def get_ny_code_url(new_york_codes: dict, section_abbr: str, section_num: str):
    """Generate URL for New York statute."""
    new_item_url = new_york_codes["item_url"].replace(new_york_codes["to_replace_sec"], section_abbr).replace(new_york_codes["to_replace_num"], section_num)
    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None


def get_wa_code_url(washington_codes: dict, cite: str) -> Optional[str]:
    """
    Generate URL for Washington state statute (RCW).
    cite: like '79A.25.030' or '19.86.020'
    """
    new_item_url = washington_codes["item_url"].replace(washington_codes["to_replace_cite"], cite)
    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None


def get_or_code_url(oregon_codes: dict, chapter: str) -> Optional[str]:
    """
    Generate URL for Oregon state statute (ORS).
    chapter: like '30' or '646A' (just the chapter part, not section)
    URL pattern: https://www.oregonlegislature.gov/bills_laws/ors/ors030.html
    Chapter is zero-padded to 3 digits (letters stay as-is after digits)
    """
    # Extract numeric part and optional letter suffix
    match = re.match(r"(\d+)([A-Za-z]?)", chapter)
    if not match:
        return None

    num_part = match.group(1)
    letter_part = match.group(2) or ""

    # Zero-pad numeric part to 3 digits, append letter if present
    padded_chapter = num_part.zfill(3) + letter_part.upper()

    new_item_url = oregon_codes["item_url"].replace(oregon_codes["to_replace_chapter"], padded_chapter.lower())
    print("new_item_url: ", new_item_url)
    url_status = check_url_status(new_item_url)
    if url_status == 200:
        return new_item_url
    else:
        return None


def find_state_codes(intext: str):
    """
    Find list of unique state statutes inside intext.

    Returns: list of dicts [
        {'prefix': '', 'id': None, 'displayName': '', 'citationName': ''},   # prefix is actually the full url
    ]
    """
    print("inside find_state_codes")
    unqiue_statutes = set()
    list_state_citations = []

    if any(keyword in intext for keyword in ("California", "Cal.", "CALIFORNIA")):
        print("inside California statutes")
        for i, section_abbr in enumerate(CA_ABBRS):
            each_code_pattern = CA_STATUES_PATTERNS[i]
            matches = re.finditer(each_code_pattern, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        section_num = each_match.group(1)
                        code_url = get_cal_code_url(CA_CODES, section_abbr, section_num)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                                {"prefix": code_url,
                                "id": None,
                                'form': whole_citation,
                                "citationName": whole_citation,
                                "displayName": whole_citation,
                                'start_index': each_match.start(),
                                'end_index': each_match.end(),
                                'type': 'state'})

    if any(keyword in intext for keyword in ("N.Y.","CPLR", "C.P.L.R.")):
        print("looking for New York statutes")
        for i, section_abbr in enumerate(NY_ABBRS):
            each_code_pattern = NY_STATUES_PATTERNS[i]
            matches = re.finditer(each_code_pattern, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        section_num = each_match.group(1)
                        code_url = get_ny_code_url(NY_CODES, section_abbr, section_num)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                            {"prefix": code_url,
                             "id": None,
                             'form': whole_citation,
                             "citationName": whole_citation,
                             "displayName": whole_citation,
                             'start_index': each_match.start(),
                             'end_index': each_match.end(),
                             'type': 'state'})

    if any(keyword in intext for keyword in ("ARIZONA", "Arizona", "Ariz.")):
        print("inside Arizona statutes")
        for each_statue_p in AZ_STATUES_PATTERNS:
            matches = re.finditer(each_statue_p, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        sec_code, num = each_match.group(1).split('-')
                        code_url = get_ariz_code_url(AZ_CODES, sec_code, num)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                            {"prefix": code_url,
                             "id": None,
                             "citationName": whole_citation,
                             "displayName": whole_citation,
                             'start_index': each_match.start(),
                             'end_index': each_match.end(),
                             'type': 'state'})

    if any(keyword in intext for keyword in ("Wisconsin", "Wis.", "WISCONSIN")):
        print("inside Wisconsin statutes")
        for each_statue_p in WI_STATUES_PATTERNS:
            matches = re.finditer(each_statue_p, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        chapter_section = each_match.group(1)
                        code_url = get_wis_code_url(WI_CODES, chapter_section)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                            {"prefix": code_url,
                             "id": None,
                             'form': whole_citation,
                             "citationName": whole_citation,
                             "displayName": whole_citation,
                             'start_index': each_match.start(),
                             'end_index': each_match.end(),
                             'type': 'state'})

    if any(keyword in intext for keyword in ("Illinois", "Ill.", "ILLINOIS")):
        print("inside Illinois statutes")
        for each_statue_p in IL_STATUES_PATTERNS:
            matches = re.finditer(each_statue_p, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        code_url = get_il_code_url(IL_CODES)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                            {"prefix": code_url,
                                "id": None,
                                'form': whole_citation,
                                "citationName": whole_citation,
                                "displayName": whole_citation,
                                'start_index': each_match.start(),
                                'end_index': each_match.end(),
                                'type': 'state'})

    if any(keyword in intext for keyword in ("Massachusetts", "Mass.", "M.G.L.", "G.L.", "MGL")):
        print("looking for Massachusetts statutes")
        for each_statue_p in MA_STATUES_PATTERNS:
            matches = re.finditer(each_statue_p, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)

                        chapter = each_match.group(1)
                        section = None
                        # if your MA number_pattern includes optional section as group(2)
                        if each_match.lastindex and each_match.lastindex >= 2:
                            section = each_match.group(2)

                        code_url = get_ma_code_url(MA_CODES, chapter, section)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                                {"prefix": code_url,
                                "id": None,
                                "form": whole_citation,
                                "citationName": whole_citation,
                                "displayName": whole_citation,
                                "start_index": each_match.start(),
                                "end_index": each_match.end(),
                                "type": "state"})

    if any(keyword in intext for keyword in ("Washington", "Wash.", "RCW", "R.C.W.")):
        print("looking for Washington statutes")
        for each_statue_p in WA_STATUES_PATTERNS:
            matches = re.finditer(each_statue_p, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        cite = each_match.group(1)  # The citation number like 79A.25.030
                        code_url = get_wa_code_url(WA_CODES, cite)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                                {"prefix": code_url,
                                 "id": None,
                                 "form": whole_citation,
                                 "citationName": whole_citation,
                                 "displayName": whole_citation,
                                 "start_index": each_match.start(),
                                 "end_index": each_match.end(),
                                 "type": "state"})

    if any(keyword in intext for keyword in ("Oregon", "Or.", "ORS", "O.R.S.")):
        print("looking for Oregon statutes")
        for each_statue_p in OR_STATUES_PATTERNS:
            matches = re.finditer(each_statue_p, intext)
            if matches:
                for each_match in matches:
                    whole_citation = each_match.group(0)
                    if whole_citation not in unqiue_statutes:
                        unqiue_statutes.add(whole_citation)
                        chapter = each_match.group(1)  # The chapter like 30 or 646A
                        # section = each_match.group(2) if each_match.lastindex >= 2 else None
                        code_url = get_or_code_url(OR_CODES, chapter)
                        logger.info(f"URL for {whole_citation}: {code_url}")
                        if code_url:
                            list_state_citations.append(
                                {"prefix": code_url,
                                 "id": None,
                                 "form": whole_citation,
                                 "citationName": whole_citation,
                                 "displayName": whole_citation,
                                 "start_index": each_match.start(),
                                 "end_index": each_match.end(),
                                 "type": "state"})

    return list_state_citations
