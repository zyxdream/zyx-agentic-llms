# Waymo guided-drafter benchmark

Generated: 24/24 outputs; four-way comparison judgments: 12/12.
Known cost: $38.76

Each judge sees four anonymous candidates and a separately labeled filed Answer or RFP as context, not a fifth ranked candidate. Answer comparisons use only clear filed denials, not admissions or other lawyer-selected responses. Element Checker uses the FAC because no filed analysis exists.

Scores are 0–100. The filed document is context only; only the four generated outputs receive scores and ranks.

## answer

### Per-run round scores

| Model | Effort | Repeat | Comparison score | Mean rank | Critical errors | Cost | Output |
|---|---|---:|---:|---:|---:|---:|---|
| gpt-5.5 | medium | 1 | 69.0 | 4 | 8 | $2.8282 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-5.5-medium/repeat-1.md` |
| gpt-6-sol | medium | 1 | 84.5 | 2 | 2 | $1.2940 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6-sol-medium/repeat-1.md` |
| gpt-6.1-sol | high | 1 | 84.0 | 1.5 | 4 | $1.8609 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-high/repeat-1.md` |
| gpt-6.1-sol | medium | 1 | 80.5 | 2.5 | 5 | $1.3748 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-medium/repeat-1.md` |
| gpt-5.5 | medium | 2 | 65.5 | 4 | 8 | $2.8303 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-5.5-medium/repeat-2.md` |
| gpt-6-sol | medium | 2 | 86.0 | 1 | 0 | $1.2933 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6-sol-medium/repeat-2.md` |
| gpt-6.1-sol | high | 2 | 80.5 | 2.5 | 4 | $1.8830 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-high/repeat-2.md` |
| gpt-6.1-sol | medium | 2 | 78.0 | 2.5 | 4 | $1.3797 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-medium/repeat-2.md` |

### Two-run model summary

| Model | Effort | Comparison mean [range] | Rank mean [range] | Failures |
|---|---|---|---|---:|
| gpt-5.5 | medium | 67.25 [65.5, 69.0] | 4 [4, 4] | 0 |
| gpt-6-sol | medium | 85.25 [84.5, 86.0] | 1.5 [1, 2] | 0 |
| gpt-6.1-sol | medium | 79.25 [78.0, 80.5] | 2.5 [2.5, 2.5] | 0 |
| gpt-6.1-sol | high | 82.25 [80.5, 84.0] | 2.0 [1.5, 2.5] | 0 |

## element_checker

### Per-run round scores

| Model | Effort | Repeat | Comparison score | Mean rank | Critical errors | Cost | Output |
|---|---|---:|---:|---:|---:|---:|---|
| gpt-5.5 | medium | 1 | 47.0 | 4 | 7 | $3.7313 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-5.5-medium/repeat-1.md` |
| gpt-6-sol | medium | 1 | 71.5 | 3 | 2 | $1.5369 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6-sol-medium/repeat-1.md` |
| gpt-6.1-sol | high | 1 | 86.5 | 1.5 | 1 | $2.0943 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-high/repeat-1.md` |
| gpt-6.1-sol | medium | 1 | 91.5 | 1.5 | 1 | $1.5069 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-medium/repeat-1.md` |
| gpt-5.5 | medium | 2 | 54.5 | 4 | 7 | $4.0131 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-5.5-medium/repeat-2.md` |
| gpt-6-sol | medium | 2 | 62.0 | 3 | 3 | $1.4245 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6-sol-medium/repeat-2.md` |
| gpt-6.1-sol | high | 2 | 87.0 | 2 | 3 | $1.9709 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-high/repeat-2.md` |
| gpt-6.1-sol | medium | 2 | 91.5 | 1 | 0 | $1.3827 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-medium/repeat-2.md` |

### Two-run model summary

| Model | Effort | Comparison mean [range] | Rank mean [range] | Failures |
|---|---|---|---|---:|
| gpt-5.5 | medium | 50.75 [47.0, 54.5] | 4 [4, 4] | 0 |
| gpt-6-sol | medium | 66.75 [62.0, 71.5] | 3 [3, 3] | 0 |
| gpt-6.1-sol | medium | 91.5 [91.5, 91.5] | 1.25 [1, 1.5] | 0 |
| gpt-6.1-sol | high | 86.75 [86.5, 87.0] | 1.75 [1.5, 2] | 0 |

## rfp

### Per-run round scores

| Model | Effort | Repeat | Comparison score | Mean rank | Critical errors | Cost | Output |
|---|---|---:|---:|---:|---:|---:|---|
| gpt-5.5 | medium | 1 | 68.0 | 4 | 2 | $1.3550 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-5.5-medium/repeat-1.md` |
| gpt-6-sol | medium | 1 | 87.0 | 2.5 | 0 | $0.4771 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6-sol-medium/repeat-1.md` |
| gpt-6.1-sol | high | 1 | 87.0 | 2.5 | 1 | $0.7891 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-high/repeat-1.md` |
| gpt-6.1-sol | medium | 1 | 95.0 | 1 | 0 | $0.5956 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-medium/repeat-1.md` |
| gpt-5.5 | medium | 2 | 63.0 | 4 | 3 | $1.2955 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-5.5-medium/repeat-2.md` |
| gpt-6-sol | medium | 2 | 82.0 | 3 | 2 | $0.4693 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6-sol-medium/repeat-2.md` |
| gpt-6.1-sol | high | 2 | 93.0 | 1.5 | 1 | $0.7878 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-high/repeat-2.md` |
| gpt-6.1-sol | medium | 2 | 93.0 | 1.5 | 0 | $0.5807 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-medium/repeat-2.md` |

### Two-run model summary

| Model | Effort | Comparison mean [range] | Rank mean [range] | Failures |
|---|---|---|---|---:|
| gpt-5.5 | medium | 65.5 [63.0, 68.0] | 4 [4, 4] | 0 |
| gpt-6-sol | medium | 84.5 [82.0, 87.0] | 2.75 [2.5, 3] | 0 |
| gpt-6.1-sol | medium | 94.0 [93.0, 95.0] | 1.25 [1, 1.5] | 0 |
| gpt-6.1-sol | high | 90.0 [87.0, 93.0] | 2.0 [1.5, 2.5] | 0 |


The report JSON retains both judges' individual scores, ranks, evidence, and critical errors for each of the two rounds. Review all candidate output files with a lawyer before any production model decision.
