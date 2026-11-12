"""Prepare a private, reviewable Waymo benchmark fixture.

Run from the repository root in the lexintel Conda environment. The output is
under ignored ``artifacts/`` because the filed RFP is Attorneys' Eyes Only.
This module never calls a model or sends the reference documents to a provider.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from backend.util.data_tools import (
    get_complaint_text_with_line_metadata_from_file,
    prepare_complaint_text_for_memory,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "tests/e2e/fixtures"
FAC = SOURCE_DIR / "sample-waymo-fac.pdf"
ANSWER = SOURCE_DIR / "sample-waymo-answer.pdf"
RFP = SOURCE_DIR / "sample-rfp-uber.pdf"
PRIVATE_DIR = ROOT / "artifacts/waymo_benchmark"
CAUSE_RE = re.compile(r"<B>(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH) CAUSE OF ACTION</B>")
PARAGRAPH_RE = re.compile(r"^(\d{1,3})\.$")
RFP_RE = re.compile(r"^\s*(?:\d{1,2}\s+)?REQUEST FOR PRODUCTION NO\.\s*(\d{1,3})\s*:", re.I | re.M)


def _pdf_text(path: Path, *, page: int | None = None) -> str:
    command = ["pdftotext", "-layout"]
    if page is not None:
        command += ["-f", str(page), "-l", str(page)]
    command += [str(path), "-"]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _ocr_page(path: Path, page: int) -> str:
    with tempfile.TemporaryDirectory(prefix="waymo-rfp-ocr-") as temporary:
        prefix = Path(temporary) / "page"
        subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", "200", "-png", str(path), str(prefix)],
            check=True, capture_output=True,
        )
        return subprocess.run(
            ["tesseract", str(prefix.with_suffix(".png")), "stdout"],
            check=True, capture_output=True, text=True,
        ).stdout


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_fac() -> dict:
    text, case_number, _is_image, metadata = get_complaint_text_with_line_metadata_from_file(str(FAC))
    cover, lines, normalized_metadata = prepare_complaint_text_for_memory(
        FAC.name, text, metadata, lines_per_chunk_pdf=200, lines_per_chunk_docx=200,
    )
    starts = [(index, CAUSE_RE.search(line).group(1)) for index, line in enumerate(lines) if CAUSE_RE.search(line)]
    if len(starts) != 7 or [name for _, name in starts] != [
        "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "SIXTH", "SEVENTH",
    ]:
        raise ValueError(f"Expected seven ordered FAC causes, got {starts}")
    prayer = next((index for index, line in enumerate(lines) if "PRAYER FOR RELIEF" in line), None)
    if prayer is None or prayer <= starts[-1][0]:
        raise ValueError("FAC prayer boundary is missing")
    bounds = [index for index, _ in starts] + [prayer]
    causes = [
        {
            "number": index + 1,
            "title": " ".join(re.sub(r"</?B>", "", line).strip() for line in lines[start:start + 3]),
            "start_line": start,
            "end_line": bounds[index + 1],
        }
        for index, (start, _) in enumerate(starts)
    ]
    paragraphs = {}
    for index, line in enumerate(lines):
        match = PARAGRAPH_RE.fullmatch(line.strip())
        if match and int(match.group(1)) not in paragraphs:
            paragraphs[int(match.group(1))] = index
    if sorted(paragraphs) != list(range(1, 159)):
        raise ValueError(f"FAC numbered paragraphs are not 1–158: {sorted(paragraphs)}")
    return {
        "source_path": str(FAC), "source_sha256": _sha256(FAC), "case_number": case_number,
        "cover": cover, "lines": lines, "line_metadata": normalized_metadata,
        "causes": causes, "paragraph_locations": paragraphs,
        "represented_defendants": ["Uber Technologies, Inc.", "Ottomotto LLC"],
        "venue": "United States District Court, Northern District of California",
        "court_classification": "FEDERAL",
    }


def extract_rfp() -> dict:
    page_count = int(re.search(r"^Pages:\s+(\d+)", subprocess.run(
        ["pdfinfo", str(RFP)], check=True, capture_output=True, text=True,
    ).stdout, re.M).group(1))
    page_text = []
    ocr_pages = []
    for page in range(1, page_count + 1):
        text = _pdf_text(RFP, page=page)
        if len(text.strip()) < 100:
            text = _ocr_page(RFP, page)
            ocr_pages.append(page)
        page_text.append(text)
    all_text = "\n".join(page_text)
    matches = list(RFP_RE.finditer(all_text))
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, 147)):
        raise ValueError(f"RFP requests must be exactly 1–146, got {numbers}")
    requests = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(all_text)
        body = all_text[match.end():end].strip()
        if not body:
            raise ValueError(f"RFP request {numbers[index]} has no text")
        requests.append({"number": numbers[index], "text": body})
    return {
        "source_path": str(RFP), "source_sha256": _sha256(RFP),
        "ocr_pages": ocr_pages, "requests": requests,
    }


def prepare(*, force: bool = False) -> Path:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.chmod(0o700)
    # Keep the old prepared.json and review.md intact for provenance. V2 never
    # derives candidate choices from the filed Answer.
    destination = PRIVATE_DIR / "prepared-v2.json"
    if destination.exists() and not force:
        raise FileExistsError(f"Prepared fixture exists; refuse to overwrite lawyer review: {destination}")
    fac = extract_fac()
    output = {
        "version": 2,
        "fac": fac,
        "answer_input": {"default_choice": "deny", "source": "FAC-only benchmark control"},
        "answer_reference": {"source_path": str(ANSWER), "source_sha256": _sha256(ANSWER)},
        "rfp_reference": extract_rfp(),
        "fixed_factual_digest": None,
        "fixed_element_summaries": None,
        "reference_only_note": "Filed Answer/RFP contain lawyer knowledge and later facts; candidates see FAC only.",
    }
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return destination


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Replace the private fixture, losing any manual review")
    args = parser.parse_args()
    print(prepare(force=args.force))
