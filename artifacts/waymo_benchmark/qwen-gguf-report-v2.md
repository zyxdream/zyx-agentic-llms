# Waymo guided-drafter benchmark — local Qwen judge

Generated: 24/24 original outputs; Qwen four-way comparisons: 5/6.
Qwen generated tokens: 34612; evaluated input tokens: 1992260.

The Qwen judge sees the same four anonymous candidates and separately labeled filed reference as Astra. Filed documents are contextual references, not ranked candidates.

Scores are 0–100. Astra is shown separately for comparison, never averaged into Qwen scores.

## answer

### Per-run round scores

| Model | Effort | Repeat | Qwen score | Qwen rank | Astra score | Astra rank | Critical errors | Original candidate cost | Output |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| gpt-5.5 | medium | 1 | 85.0 | 2 | 69.0 | 4 | 0 | $1.9864 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-5.5-medium/repeat-1.md` |
| gpt-6-sol | medium | 1 | 100.0 | 1 | 78.0 | 3 | 0 | $0.4522 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6-sol-medium/repeat-1.md` |
| gpt-6.1-sol | medium | 1 | 85.0 | 3 | 85.0 | 2 | 0 | $0.5330 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-medium/repeat-1.md` |
| gpt-6.1-sol | high | 1 | 76.0 | 4 | 85.0 | 1 | 0 | $1.0191 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-high/repeat-1.md` |
| gpt-5.5 | medium | 2 | 92.0 | 2 | 62.0 | 4 | 0 | $1.9891 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-5.5-medium/repeat-2.md` |
| gpt-6-sol | medium | 2 | 100.0 | 1 | 85.0 | 1 | 0 | $0.4521 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6-sol-medium/repeat-2.md` |
| gpt-6.1-sol | medium | 2 | 92.0 | 3 | 78.0 | 2 | 0 | $0.5385 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-medium/repeat-2.md` |
| gpt-6.1-sol | high | 2 | 87.0 | 4 | 76.0 | 3 | 1 | $1.0418 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/answer/gpt-6.1-sol-high/repeat-2.md` |

### Two-run model summary

| Model | Effort | Qwen mean [range] | Rank mean [range] | Scored rounds | Failures |
|---|---|---|---|---:|---:|
| gpt-5.5 | medium | 88.5 [85.0, 92.0] | 2 [2, 2] | 2 | 0 |
| gpt-6-sol | medium | 100.0 [100.0, 100.0] | 1 [1, 1] | 2 | 0 |
| gpt-6.1-sol | medium | 88.5 [85.0, 92.0] | 3 [3, 3] | 2 | 0 |
| gpt-6.1-sol | high | 81.5 [76.0, 87.0] | 4 [4, 4] | 2 | 0 |

## element_checker

### Per-run round scores

| Model | Effort | Repeat | Qwen score | Qwen rank | Astra score | Astra rank | Critical errors | Original candidate cost | Output |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| gpt-5.5 | medium | 1 | — | — | 43.0 | 4 | 0 | $2.7805 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-5.5-medium/repeat-1.md` |
| gpt-6-sol | medium | 1 | — | — | 60.0 | 3 | 0 | $0.5861 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6-sol-medium/repeat-1.md` |
| gpt-6.1-sol | medium | 1 | — | — | 83.0 | 2 | 0 | $0.5561 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-medium/repeat-1.md` |
| gpt-6.1-sol | high | 1 | — | — | 84.0 | 1 | 0 | $1.1435 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-high/repeat-1.md` |
| gpt-5.5 | medium | 2 | 100.0 | 4 | 43.0 | 4 | 0 | $3.1547 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-5.5-medium/repeat-2.md` |
| gpt-6-sol | medium | 2 | 100.0 | 3 | 55.0 | 3 | 0 | $0.5661 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6-sol-medium/repeat-2.md` |
| gpt-6.1-sol | medium | 2 | 100.0 | 1 | 83.0 | 1 | 0 | $0.5243 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-medium/repeat-2.md` |
| gpt-6.1-sol | high | 2 | 100.0 | 2 | 77.0 | 2 | 0 | $1.1125 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/element_checker/gpt-6.1-sol-high/repeat-2.md` |

### Two-run model summary

| Model | Effort | Qwen mean [range] | Rank mean [range] | Scored rounds | Failures |
|---|---|---|---|---:|---:|
| gpt-5.5 | medium | 100.0 [100.0, 100.0] | 4 [4, 4] | 1 | 1 |
| gpt-6-sol | medium | 100.0 [100.0, 100.0] | 3 [3, 3] | 1 | 1 |
| gpt-6.1-sol | medium | 100.0 [100.0, 100.0] | 1 [1, 1] | 1 | 1 |
| gpt-6.1-sol | high | 100.0 [100.0, 100.0] | 2 [2, 2] | 1 | 1 |

## rfp

### Per-run round scores

| Model | Effort | Repeat | Qwen score | Qwen rank | Astra score | Astra rank | Critical errors | Original candidate cost | Output |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| gpt-5.5 | medium | 1 | 60.0 | 4 | 70.0 | 4 | 2 | $1.1414 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-5.5-medium/repeat-1.md` |
| gpt-6-sol | medium | 1 | 80.0 | 2 | 84.0 | 3 | 0 | $0.2635 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6-sol-medium/repeat-1.md` |
| gpt-6.1-sol | medium | 1 | 74.0 | 3 | 96.0 | 1 | 2 | $0.3820 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-medium/repeat-1.md` |
| gpt-6.1-sol | high | 1 | 100.0 | 1 | 90.0 | 2 | 0 | $0.5755 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-high/repeat-1.md` |
| gpt-5.5 | medium | 2 | 80.0 | 3 | 60.0 | 4 | 0 | $1.0945 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-5.5-medium/repeat-2.md` |
| gpt-6-sol | medium | 2 | 80.0 | 4 | 84.0 | 3 | 0 | $0.2683 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6-sol-medium/repeat-2.md` |
| gpt-6.1-sol | medium | 2 | 100.0 | 1 | 90.0 | 2 | 0 | $0.3797 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-medium/repeat-2.md` |
| gpt-6.1-sol | high | 2 | 96.0 | 2 | 96.0 | 1 | 0 | $0.5868 | `/Users/yuxiao/Documents/lexintel/artifacts/waymo_benchmark/candidate_outputs/rfp/gpt-6.1-sol-high/repeat-2.md` |

### Two-run model summary

| Model | Effort | Qwen mean [range] | Rank mean [range] | Scored rounds | Failures |
|---|---|---|---|---:|---:|
| gpt-5.5 | medium | 70.0 [60.0, 80.0] | 3.5 [3, 4] | 2 | 0 |
| gpt-6-sol | medium | 80.0 [80.0, 80.0] | 3 [2, 4] | 2 | 0 |
| gpt-6.1-sol | medium | 87.0 [74.0, 100.0] | 2 [1, 3] | 2 | 0 |
| gpt-6.1-sol | high | 98.0 [96.0, 100.0] | 1.5 [1, 2] | 2 | 0 |


The JSON report retains Qwen's individual evidence and critical errors. No filed reference or raw judge prompt is written to this report.
