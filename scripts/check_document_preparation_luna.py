"""Opt-in live Luna preparation check; synthetic sources, no database writes.

Run inside Conda lexintel: python -m scripts.check_document_preparation_luna --long
This uses the application's configured API credentials and incurs API usage.
"""
from __future__ import annotations

import argparse
import json
import time

from backend.util import preparation_summary as summary
from backend.util.attachment_parser import fingerprint, manifest_from_blocks


def fixtures(include_long=False):
    yield 'supply-agreement.txt', [
        {'text': 'SUPPLY AGREEMENT', 'heading': 'SUPPLY AGREEMENT'},
        {'text': 'Alpha Customer and Beta Supplier enter this agreement. Business Day means Monday through Friday, excluding New York public holidays.'},
        {'text': 'Alpha Customer shall pay Beta Supplier $125,000 on December 31, 2030. If written notice is received within ten Business Days, the payment deadline extends by thirty days.'},
        {'text': 'The aggregate liability cap is $250,000. The cap does not apply to fraud or gross negligence.'},
        {'text': 'The customer alleges that an April 2030 shipment was late. This is an allegation in correspondence, not a judicial finding.'},
    ], ['$125,000', '$250,000', 'gross negligence']
    if include_long:
        filler = [{'text': 'This administrative background paragraph repeats correspondence context and adds no operative deadlines, amounts, duties, or exceptions.',
                   'paragraph': i + 1} for i in range(1600)]
        blocks = [
            {'text': 'ESCROW RECORD', 'heading': 'ESCROW RECORD'},
            {'text': 'North Company and South Company are parties to this escrow record. Delivery is due at the end of the Relevant Period.'},
        ] + filler[:400] + [
            {'text': 'The escrow release amount is $77,777. A release objection must be delivered within forty-five days, except a fraud objection remains permitted afterward.'},
        ] + filler[400:1200] + [
            {'text': 'Relevant Period means the period ending March 15, 2031. This definition applies to the delivery obligation stated at the beginning.'},
        ] + filler[1200:] + [
            {'text': 'South Company alleges breach by North Company. The correspondence does not report a judicial finding of breach.'},
        ]
        yield 'escrow-record.txt', blocks, ['$77,777', '2031', 'fraud', 'forty-five', 'Relevant Period']


def check(filename, blocks, expected):
    manifest = manifest_from_blocks(blocks)
    saved = {}

    def checkpoint(key, value=None):
        if value is not None and '_work' not in value:
            saved[key] = value
        return saved.get(key)

    started = time.monotonic()
    metadata, coverage = summary.summarize_document(
        manifest['text'], filename, checkpoint,
        source_hash=fingerprint(manifest['text']), manifest=manifest,
    )
    sections = [record for record in saved.values() if record.get('_kind') == 'section']
    material = json.dumps([metadata, sections], ensure_ascii=False).casefold()
    checks = {term: term.casefold() in material for term in expected}
    checks['allegation_attribution'] = any(
        fact['attribution'] == 'allegation' for record in sections for fact in record['facts']
    )
    checks['complete_readable_coverage'] = coverage['covered_characters'] == len(manifest['text'])
    checks['dedicated_route'] = coverage['model'] == 'gpt-6-luna' and coverage['reasoning_effort'] == 'medium'
    return {'file': filename, 'ok': all(checks.values()), 'checks': checks,
            'source_tokens': coverage['source_tokens'], 'sections': coverage['total_segments'],
            'usage': coverage['usage'], 'elapsed_seconds': round(time.monotonic() - started, 1)}


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument('--long', action='store_true', help='Also process a synthetic 32K-token record.')
    options = arguments.parse_args()
    passed = True
    for filename, blocks, expected in fixtures(options.long):
        result = check(filename, blocks, expected)
        passed = passed and result['ok']
        print(json.dumps(result), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
