"""Word-only citation context adapter; never changes shared verifier behavior.

Resolve named reporter short forms against unique full references anywhere in
captured body/notes before batching. Unresolved forms remain review items, not
false mismatches against a reporter's pinpoint page. This is not a citator.
"""
import re

REPORTER = r"(?:F\.\s*(?:Supp\.\s*)?(?:2d|3d|4th)?|U\.\s*S\.|S\.\s*Ct\.|L\.\s*Ed\.\s*(?:2d)?|Cal\.\s*(?:App\.\s*)?(?:2d|3d|4th|5th)|A\.\s*(?:2d|3d)|N\.\s*E\.\s*(?:2d|3d)|S\.\s*W\.\s*(?:2d|3d))"
FULL = re.compile(rf"(?P<volume>\d+)\s+(?P<reporter>{REPORTER})\s+(?P<page>\d+)\b")
SHORT = re.compile(rf"(?P<name>[A-Z][\w’'&.-]*(?:\s+[A-Z][\w’'&.-]*){{0,3}}),\s*(?P<volume>\d+)\s+(?P<reporter>{REPORTER})\s+at\s+(?P<pin>\d+(?:[–-]\d+)?)")
SUPRA = re.compile(r"(?P<name>[A-Z][\w’'&.-]*(?:\s+[A-Z][\w’'&.-]*){0,3}),\s*supra(?:,\s*at\s+(?P<pin>\d+(?:[–-]\d+)?))?")
SECTION = re.compile(r"(?<!§)§\s*(?P<section>\d+[a-z]*(?:-\d+[a-z]*)?)(?P<subs>(?:\([A-Za-z0-9]+\))*)")
USC = re.compile(r"\b(?P<title>\d+)\s+U\.?\s*S\.?\s*C\.?(?:A\.|S\.)?\s*§{1,2}\s*(?P<section>\d+[a-z]*(?:-\d+[a-z]*)?)(?P<subs>(?:\([A-Za-z0-9]+\))*)", re.I)


def compact(value):
    return re.sub(r"\s+", "", value).lower()


def cornell_link(citation):
    match = USC.search(citation)
    if not match:
        return ""
    fragment = "_".join(re.findall(r"\(([^)]+)\)", match["subs"]))
    return f"https://www.law.cornell.edu/uscode/text/{match['title']}/{match['section']}" + (f"#{fragment}" if fragment else "")


def short_forms(text, context):
    full = []
    for match in FULL.finditer(context):
        # Only named cases are antecedents. Bare reporter strings are ambiguous.
        prefix = context[max(0, match.start() - 200):match.start()].rstrip(" ,")
        prefix = re.split(r"[\n;!?]", prefix)[-1]
        name = re.search(r"([A-Z][A-Za-z0-9’'&., -]{0,100}\s+v\.\s+[A-Z][A-Za-z0-9’'&., -]{0,100})$", prefix)
        if name:
            full.append((match, name[1]))
    resolutions = []
    for short in sorted([*SHORT.finditer(text), *SUPRA.finditer(text)], key=lambda match: match.start()):
        words = set(re.findall(r"[a-z0-9]+", short['name'].lower())) - {"see", "also", "the", "in", "re", "inc", "co", "corp"}
        candidates = {}
        for match, name in full:
            if short.re is SHORT and (match['volume'] != short['volume'] or compact(match['reporter']) != compact(short['reporter'])):
                continue
            if words and words.issubset(set(re.findall(r"[a-z0-9]+", name.lower()))):
                # Two cases starting on different pages remain ambiguous.
                candidates[(compact(match[0]), compact(name))] = f"{name}, {match[0]}"
        citation = next(iter(candidates.values())) if len(candidates) == 1 else None
        # Supply both parties: the shared verifier's name window can include
        # preceding prose, which otherwise makes a correct short name look wrong.
        expanded = (citation + (f", {short['pin']}" if short['pin'] else "")) if citation else None
        resolutions.append((short, expanded))
    return resolutions


def case_keys(text):
    """Reporter identity excludes pinpoints, year and harmless spacing."""
    return {(m['volume'], compact(m['reporter']).replace('.', ''), m['page'])
            for m in FULL.finditer(text)}


def lookup_short_case(short):
    """Resolve a named reporter short form, never treating its pinpoint as page one.

    Only the public citation is searched. A unique matching cluster is required;
    the caller still sends the full reference through Hallucination Guard.
    """
    if short.re is not SHORT:
        return None
    from backend.word_addin.research import search_cases, _get
    words = set(re.findall(r'[a-z0-9]+', short['name'].lower())) - {'see', 'also', 'the', 'inc', 'co', 'corp'}
    if not words:
        return None
    try:
        public_name = ' AND '.join(chr(34) + word + chr(34) for word in sorted(words))
        hits = search_cases(f'caseName:({public_name}) AND citation:("{short["volume"]} {short["reporter"]}")')
        # A full result page may hide another matching case. Do not guess.
        if len(hits) >= 5:
            return None
        candidates = {}
        for hit in hits:
            identifier = hit.get('cluster_id')
            if not str(identifier).isdigit():
                continue
            cluster = _get('clusters', identifier)
            name = cluster.get('case_name', '')
            if not words.issubset(set(re.findall(r'[a-z0-9]+', name.lower()))):
                continue
            for citation in cluster.get('citations', []):
                reporter = str(citation.get('reporter') or '')
                page = str(citation.get('page') or '')
                if (str(citation.get('volume')) == short['volume']
                        and compact(reporter).replace('.', '') == compact(short['reporter']).replace('.', '')
                        and page.isdigit() and int(page) <= int(re.split(r'[–-]', short['pin'])[0])):
                    candidates[str(identifier)] = f"{name}, {short['volume']} {reporter} {page}, {short['pin']}"
        return next(iter(candidates.values())) if len(candidates) == 1 else None
    except Exception:
        # A lookup outage is an unresolved citation, never proof it is wrong.
        return None


def review_group(text, context, verify, lookup=None):
    resolutions = short_forms(text, context)
    external = set()
    if lookup:
        cache = {}
        updated = []
        for match, replacement in resolutions:
            # When this reporter/volume already occurs in the document, an
            # unresolved reference may be ambiguous. Public lookup must not
            # silently override competing document antecedents.
            has_antecedent = match.re is SHORT and any(
                m['volume'] == match['volume'] and compact(m['reporter']) == compact(match['reporter'])
                for m in FULL.finditer(context))
            if replacement is None and match.re is SHORT and not has_antecedent:
                key = match[0]
                if key not in cache and len(cache) < 3:
                    cache[key] = lookup(match)
                replacement = cache.get(key)
                if replacement:
                    external.add(match.start())
            updated.append((match, replacement))
        resolutions = updated
    expanded = text
    for match, replacement in reversed(resolutions):
        expanded = expanded[:match.start()] + (replacement or "[Unresolved short-form case citation]") + expanded[match.end():]
    result = verify(expanded)
    findings = []
    for finding in result['findings']:
        anchor = finding.get('anchor') or finding.get('title', '')
        matches = [(m, replacement) for m, replacement in resolutions
                   if replacement and anchor and (anchor in replacement or replacement in anchor)]
        if matches:
            for match, _ in matches:
                findings.append({**finding, 'title': match[0], 'anchor': match[0],
                                 'detail': finding.get('detail', '') +
                                 (' Short form matched to a published case by name and reporter.' if match.start() in external else
                                  ' Short form resolved using a unique full citation in the captured document.') +
                                 ' Review the pinpoint and proposition in context.'})
        else:
            findings.append({**finding, 'anchor': anchor if anchor in text else ''})
    for match, replacement in resolutions:
        if not replacement:
            findings.append({'title': match[0], 'anchor': match[0], 'status': 'review', 'href': '', 'evidence': [],
                             'detail': 'This short form could not be uniquely resolved from the available document context or case lookup. Include the full citation or review it manually; this does not mean the authority is incorrect.'})
        elif not any(f.get('anchor') == match[0] for f in findings):
            findings.append({'title': match[0], 'anchor': match[0], 'status': 'review', 'href': '', 'evidence': [],
                             'detail': f'A possible full reference was found: {replacement}. The verifier did not return a matching finding. Review the case, pinpoint and proposition manually.'})
    # Give statutes a useful text link even when the substantive audit omitted one.
    for match in USC.finditer(text):
        existing = next((f for f in findings if (f.get('anchor') and f['anchor'] in match[0]) or f.get('title') == match[0]), None)
        if existing:
            existing.update(title=match[0], anchor=match[0], href=cornell_link(match[0]))
        else:
            findings.append({'title': match[0], 'anchor': match[0], 'href': cornell_link(match[0]),
                             'status': 'review', 'evidence': [],
                             'detail': 'Open the statutory text at Cornell and review the subsection and applicable version. This link is not a substantive verification.'})
    # Resolve section-only federal references only where the captured document
    # identifies a unique U.S. Code title for that exact section. Never guess
    # across competing codes; states/rules remain the verifier's review items.
    full_statutes = list(USC.finditer(context))
    local_full = list(USC.finditer(text))
    for match in SECTION.finditer(text):
        if any(full.start() <= match.start() < full.end() for full in local_full):
            continue
        titles = {full['title'] for full in full_statutes if full['section'] == match['section']}
        # A different explicit code citation sharing this section makes inference
        # unsafe (e.g. 42 U.S.C. § 1983 versus a state Civil Code § 1983).
        competing = re.search(r"(?:C\.?F\.?R\.?|Code|Rule|Rules|Const\.|Civ\.|Pen\.|Gov\.|Stat\.|UCC)[^\n§]{0,30}§\s*" + re.escape(match['section']) + r"\b", context, re.I)
        if len(titles) == 1 and not competing:
            title = next(iter(titles))
            finding = next((f for f in findings if f.get('anchor') == match[0]), None)
            detail = f"Section-only reference matched to {title} U.S.C. using the captured document. Confirm the intended code and applicable version."
            if finding:
                finding['href'] = cornell_link(f"{title} U.S.C. {match[0]}")
                finding['detail'] = finding.get('detail', '') + ' ' + detail
            else:
                findings.append({'title': match[0], 'anchor': match[0], 'status': 'review', 'evidence': [],
                                 'href': cornell_link(f"{title} U.S.C. {match[0]}"), 'detail': detail})
    unique = {}
    for finding in findings:
        unique[(finding['title'], finding.get('detail', ''))] = finding
    result['findings'] = list(unique.values())
    count = len(result['findings'])
    result['text'] = f"Citation review returned {count} {'finding' if count == 1 else 'findings'}. Review each authority in context." if count else result.get('text', 'No reviewable authorities were returned.')
    return result
