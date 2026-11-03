# Evaluation report (replay)

- Model: `qwen2.5:3b` | prompt `prompt-v1` | score `score-v1` | labels `v1`
- Status: **replayed outputs of one recorded local-model run; not re-verified live**
- Label-file self-check: OK

## tuning cases: 6 eligible, 6 ok, 0 failed, 0 skipped

| label | pipeline P | pipeline R | pipeline acc | baseline P | baseline R | baseline acc |
|---|---|---|---|---|---|---|
| mentioned | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| recommended | 0.0 | 0.0 | 0.5 | 1.0 | 0.5 | 0.833 |
| negative | 1.0 | 1.0 | 1.0 | 1.0 | 0.5 | 0.833 |
| entity_confusion | 1.0 | 0.5 | 0.833 | 1.0 | 1.0 | 1.0 |

## final cases: 9 eligible, 8 ok, 1 failed, 0 skipped

| label | pipeline P | pipeline R | pipeline acc | baseline P | baseline R | baseline acc |
|---|---|---|---|---|---|---|
| mentioned | 1.0 | 1.0 | 1.0 | 0.833 | 1.0 | 0.875 |
| recommended | None | 0.0 | 0.875 | 0.5 | 1.0 | 0.875 |
| negative | 0.4 | 1.0 | 0.625 | 0.0 | 0.0 | 0.625 |
| entity_confusion | 1.0 | 0.333 | 0.75 | 0.667 | 0.667 | 0.75 |

## all cases: 15 eligible, 14 ok, 1 failed, 0 skipped

| label | pipeline P | pipeline R | pipeline acc | baseline P | baseline R | baseline acc |
|---|---|---|---|---|---|---|
| mentioned | 1.0 | 1.0 | 1.0 | 0.909 | 1.0 | 0.929 |
| recommended | 0.0 | 0.0 | 0.714 | 0.667 | 0.667 | 0.857 |
| negative | 0.571 | 1.0 | 0.786 | 0.5 | 0.25 | 0.714 |
| entity_confusion | 1.0 | 0.4 | 0.786 | 0.8 | 0.8 | 0.857 |

## Per case

| case | split | engine | status | expected (M/R/N/C) | pipeline | baseline | score ok |
|---|---|---|---|---|---|---|---|
| C01 | tuning | aimode | ok | 1/0/1/0 | 1/0/1/0 | 1/0/0/0 | True |
| C02 | tuning | chatgpt | ok | 1/0/0/1 | 1/0/0/1 | 1/0/0/1 | True |
| C03 | tuning | chatgpt | ok | 1/1/1/0 | 1/0/1/0 | 1/0/1/0 | True |
| C04 | tuning | gemini | ok | 1/0/0/1 | 1/1/0/0 | 1/0/0/1 | True |
| C05 | tuning | gemini | ok | 1/1/0/0 | 1/0/0/0 | 1/1/0/0 | True |
| C06 | tuning | aimode | ok | 0/0/0/0 | 0/0/0/0 | 0/0/0/0 | True |
| C07 | final | gemini | ok | 1/0/0/0 | 1/0/1/0 | 1/1/1/1 | True |
| C08 | final | aimode | ok | 1/0/1/0 | 1/0/1/0 | 1/0/0/0 | True |
| C09 | final | chatgpt | ok | 1/0/0/0 | 1/0/1/0 | 1/0/0/0 | True |
| C10 | final | aimode | ok | 1/1/0/0 | 1/0/1/0 | 1/1/0/0 | True |
| C11 | final | aimode | ok | 1/0/1/1 | 1/0/1/0 | 1/0/0/0 | True |
| C12 | final | gemini | ok | 0/0/0/1 | 0/0/0/1 | 0/0/0/1 | True |
| C13 | final | chatgpt | ok | 0/0/0/0 | 0/0/0/0 | 0/0/0/0 | True |
| E01 (synthetic) | final | synthetic | failed suspected_prompt_injection | 1/0/1/0 | - | 1/1/1/0 | - |
| E02 (synthetic) | final | synthetic | ok | 0/0/0/1 | 0/0/0/0 | 1/0/0/1 | True |

## Competitor extraction (recall on must-include names)

- C06: pipeline 1.0, baseline 1.0, forbidden names in pipeline output: none
- C13: pipeline 1.0, baseline 1.0, forbidden names in pipeline output: none
- E01: pipeline 0.0, baseline 1.0, forbidden names in pipeline output: none

## Diagnostics support check

Full-run counts: {'eligible': 150, 'failed': 0, 'failure_reasons': {}, 'model_calls': 150, 'ok': 150, 'skipped': 0}

- D1 Kasparro is absent from unbranded discovery answers: SUPPORTED (3 evidence items)
- D2 Branded answers confuse Kasparro with Kaspr or cannot identify it: SUPPORTED (6 evidence items)
- D3 When asked about Kasparro directly, engines rarely recommend it and often doubt it: SUPPORTED (5 evidence items)
- Remedies linked to diagnostics: {'R1': True, 'R2': True, 'R3': True}
- Negative control (corrupted claim + fabricated excerpt): CAUGHT ['excerpt_not_in_response', 'D1.mentioned: claims 9/75, results give 0/75']
