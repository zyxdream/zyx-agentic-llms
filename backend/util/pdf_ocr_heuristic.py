import fitz


def page_needs_ocr(page, nonempty_lines: int, min_nonempty_lines: int = 3,
                   image_coverage_threshold: float = 0.70,
                   text_coverage_threshold: float = 0.15) -> bool:
    """
    Decide whether a PDF page should be OCR'd.

    Returns True if:
    1. Sparse text: fewer than min_nonempty_lines non-empty lines, OR
    2. High image coverage with low text density: images cover >= 70% of page
       and text blocks cover < 15% of page area.
    """
    # Signal 1: sparse text (existing logic)
    if nonempty_lines < min_nonempty_lines:
        return True

    # Signal 2: image coverage vs text density
    page_area = abs(page.rect)
    if page_area == 0:
        return False

    # Calculate image coverage
    images = page.get_images(full=True)
    if not images:
        return False  # No images → text-only page, no OCR needed

    total_image_area = 0
    for img in images:
        try:
            bbox = page.get_image_bbox(img)
            if bbox and not bbox.is_empty:
                total_image_area += abs(bbox & page.rect)  # intersection with page
        except Exception:
            continue

    image_ratio = total_image_area / page_area

    if image_ratio < image_coverage_threshold:
        return False  # Images are small (logos, watermarks) — not a scan

    # Images are large — check if text density is low
    text_dict = page.get_text("dict")
    total_text_area = 0
    for block in text_dict.get("blocks", []):
        if "lines" in block:  # text block (not image block)
            block_rect = fitz.Rect(block["bbox"])
            total_text_area += abs(block_rect)

    text_ratio = total_text_area / page_area

    return text_ratio < text_coverage_threshold
