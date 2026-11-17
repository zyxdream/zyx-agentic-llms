# Opinion-location fixtures

These files contain direct public court-opinion text captured through CourtListener,
not benchmark answers or generated findings. `provenance.json` records source URLs,
opinion identity and exact text hashes. The original frozen replay is unchanged.

The Naranjo recorded passage contains `*1469`, the Rule 403 quotation, and `*1470`.
The shorter excerpt preserves the same actual quotation without either page marker.
Use a separate model conversation for each: a previous page-bearing passage must
not supply the missing evidence to the negative control.

The Reaves recorded passage contains its Rule 608(b)/insurance-fraud discussion
but no published reporter-page mapping. Its court-PDF page footers do not verify
the uploaded brief’s reporter pinpoint.

Offline tests establish unchanged delivery, source identity, location limitations
and prompt guidance. They do not establish live model compliance or legal quality.
