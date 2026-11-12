"""Explicit citation-formatting requests may change form, never invent metadata."""
import re


def requested(request):
    text = request.get('followup_instructions') or request.get('instructions', '')
    return bool(re.search(r'\bbluebook(?:ed|ing)?\b|\b(?:format|formatting|standardize|standardise)\b.{0,45}\bcitations?\b|\bcitations?\b.{0,30}\bformat(?:ting)?\b', text, re.I))


def guidance(request):
    if not requested(request):
        return ''
    return '''\nThe lawyer explicitly requested citation formatting. For this task only, citation-preservation instructions mean preserve the identity of every authority and all volumes, reporters, initial pages, pinpoints, sections, dates and quotations. You may correct case-name abbreviations, punctuation, spacing and citation form consistently with Bluebook conventions. Do not invent missing courts, years, history, pages or other metadata. Flag uncertainties in findings. Preserve paragraph boundaries. Do not put Markdown italic markers into Word text. Existing Word formatting is preserved by the editor; flag required typography changes for manual review. This is a formatting review, not certification of Bluebook compliance.\n'''


def numerical_references(text):
    values = []
    for match in re.finditer(r"(\d+)(?:\s*[-–—]\s*(\d+))?", text):
        start, end = match[1], match[2]
        if end is None:
            values.append(('number', int(start)))
        else:
            # Bluebook-style range compression changes digits but not pages:
            # 676–679 and 676–79 both refer to the same range.
            last = int(end)
            if len(end) < len(start):
                last = int(start[:-len(end)] + end)
                if last < int(start):
                    last += 10 ** len(end)
            values.append(('range', int(start), last))
    return values


def validate(before, after, request):
    if requested(request) and numerical_references(before) != numerical_references(after):
        raise ValueError('The citation-formatting proposal changed numerical references. Nothing was changed in Word. Ask LexText to format citations while preserving all volumes, pages, years and sections.')


def notes(request):
    return ['Citation form was reviewed. Check italics and other typography in Word; missing metadata and court-specific rules still require review.'] if requested(request) else []


def layout_reply(request):
    """Do not represent text replacements as changes to Word page layout."""
    instruction = request.get('followup_instructions') or request.get('instructions', '')
    if request.get('action') not in {'revise', 'revise_document', 'strengthen'}:
        return None
    layout = re.search(r'\b(?:format|formatting|reformat|comply|conform)\b.{0,65}\b(?:local|court|filing)\s+(?:rules?|requirements?)\b', instruction, re.I)
    if not layout:
        return None
    return {
        'kind': 'report',
        'text': "Automatic court formatting is not available in the Word add-in. Nothing was changed in Word. Use Word’s Layout and Paragraph controls to apply confirmed margins, spacing, and other requirements. You can use Ask LexText to make a checklist from rule text or a judge’s order you supply.",
        'findings': [], 'sources': [],
        'notes': [
            "Confirm the current court rules and any judge-specific orders. A checklist does not apply formatting or certify filing compliance.",
        ],
    }
