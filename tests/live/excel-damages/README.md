# Excel damages live suite

Synthetic multi-turn test for workbook review, formula inspection, CSV input,
recalculation, output delivery and variance explanation.

## Fixtures

```text
fixtures/
  Damages Model.xlsx   # Assumptions, Lost Profits, Costs, Payments, Summary
  payments.csv         # six monthly payments
  client-email.txt     # revised assumptions: 8%, three years, exclude disputed revenue
  expert-notes.txt     # methodology and review flags
```

All fixtures are synthetic and safe to commit. No client data is used.

## Conversation

One conversation, three turns:

1. Review the workbook and supporting files; explain the model and flag assumptions.
2. Recalculate with 8%, three years and no disputed revenue; save a revised XLSX and CSV.
3. Explain the variance and remaining assumptions; update the demand summary.

## Expected values

| Metric | Original | Revised |
| --- | ---: | ---: |
| Lost profits present value | 533,083.46 | 375,657.29 |
| Total damages | 543,083.46 | 385,657.29 |
| Variance | — | -157,426.17 |

The model contains a Year 4 revenue override and a disputed Year 3 revenue line.
The agent should flag both rather than silently accepting them.

## Notes

The current parser supports `.xlsx` and `.csv`, not `.eml`/`.msg` attachment
extraction. The email is a separate text file plus separate workbook/CSV
attachments.
