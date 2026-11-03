# Decision note

**Run:** `4a4b71d9…` captured 2026-07-31 (India, English). 150/150 responses analysed, 0 failed, 0 skipped. Model `qwen2.5:3b` (local, Ollama), prompt `prompt-v1`, score `score-v1`, categories `cat-v1`, quote match `words-v2`. The submitted results are a **replay** of one recorded real run (30.8 min of model time, 152 calls, US$0). They are not repeated and not re-verified live. **Active time:** 6 h.

## What the data says

| | AI Mode | ChatGPT | Gemini |
|---|---|---|---|
| Unbranded questions: Kasparro mentioned | 0/25 | 0/25 | 0/25 |
| Branded questions: mentioned | 25/25 | 25/25 | 22/25 |
| Branded: recommended (model) | 4/25 | 1/25 | 4/25 |
| Branded: negative (model, likely overstated) | 12/25 | 14/25 | 10/25 |
| Branded: kasparro.com cited | 8/25 | 6/25 | 0/25 |
| Branded: mean score (0–100) | 35.2 | 25.0 | 30.0 |

The same 50 prompts were asked on all three engines:
- **25 prompts:** no engine names Kasparro. These are exactly the 25 unbranded prompts.
- **22 prompts:** all three engines name it.
- **1 prompt:** recommended by all three engines.

The wins are limited to answers that talk about Kasparro when asked by name. Some describe its structural, no-prompt-hacks approach accurately (e.g. chatgpt/009). The losses:

- **D1:** absent from all 75 unbranded discovery answers. Semrush, Peec AI and Profound are named in 17–19 each.
- **D2:** confusion with Kaspr. 6/75 by model label; 3/75 branded answers are entirely about Kaspr.
- **D3:** hedged or doubtful branded answers. Of 85 Kasparro-related cited URLs, 47 are job listings and 19 are kasparro.com.

Each diagnostic separates pattern, possible explanation and test, and its evidence is response IDs plus verbatim excerpts.

## Scoring and aggregation
- **Scale.** The score is 0–100 per response, from separate components:
  - inclusion 25
  - correct identity 15
  - recommendation 40
  - list position 10/position
  - kasparro.com cited 10
  - negative −30 (clamped at 0)
- **Why these weights.** A recommendation is worth more than a mention. Being misidentified removes the identity points.
- **What is kept.** Component values are stored, so any weighting can be recomputed.
- **What is never mixed.** Branded and unbranded prompts are never averaged together, because naming the brand in the question guarantees a mention. Failed or skipped responses get no score and are reported separately from the denominators.
- **Uncertainty.** Rates carry 95% Wilson intervals over prompts, e.g. unbranded mention 0/75 with CI [0, 0.049]. These do not cover run-to-run model variation, which one capture cannot measure.
- **Alternatives considered.** A single blended "visibility index" hides the branded/unbranded split. Share of voice needs a fixed competitor set that the brief does not define.

## Backend, review and reliability choices
- **Separate layers.** ingest → categories → model gateway → deterministic extraction/guards → scoring → diagnostics → reports → API.
- **Code vs model.** Code does counts, mentions, citations, permissions and arithmetic. The LLM labels recommendation, negativity, confusion, position and competitors.
- **Model labels need proof.** A label survives only with a word-for-word quote from the answer.
- **Duplicates.** Records are keyed by supplied ID plus a content hash. The same content is reused; changed content is a recorded conflict. Each response is committed separately, so one bad record cannot erase others.
- **Review.**
  - Role comes from the server-side token only.
  - Submit freezes a SHA-256 of the content.
  - A decision applies to one exact version.
  - Any edit after submit or approval creates a new draft.
  - Export of the latest version needs that version approved. Older approvals export only when the version is named, marked `is_latest: false`.
- **Analysis runs synchronously in a background task.** This is fine for 150 records. It is not resumable after a crash, and the caller polls for status.

## Failure, diagnosis, fix, regression
1. **The quote check rejected real quotes.**
   - *Found in:* the real run (aimode/009, 010, 011 and tuning data).
   - *Cause:* characters were compared, so a `\-` escape, a leading `>`, `'Kasparro'` vs `"Kasparro"`, or trailing punctuation broke the match.
   - *Fix:* compare word sequences (`words-v2`). Paraphrases and inserted words are still rejected.
   - *Regression tests:* `tests/test_evidence_match.py`.
   - *Effect, all 15 eval cases (`eval/results/before_fix/` → `eval/results/`):* negative recall 0.20 → 1.00, precision 0.33 → 0.57, accuracy 0.60 → 0.79.
2. **Prompt injection (synthetic E01).**
   - *Found:* an instruction inside the answer made the model output `recommended: true`, backed by a genuine but irrelevant quote.
   - *Fix:* answers with instruction-like text fail as `suspected_prompt_injection` and are never scored.
   - *Regression test:* `test_prompt_injection_in_answer_is_not_scored`.

## Evaluation and honest limits
The eval set is 13 hand-labelled real responses (6 tuning, 7 final) across 3 engines and 5 intents, plus 2 synthetic edge cases. Results:
- **Mentions:** 1.00 for the pipeline vs 0.93 accuracy for the keyword baseline (the baseline counts URLs).
- **Negative:** pipeline recall 1.00 vs baseline 0.25, but pipeline precision only 0.57.
- **Recommended:** the 3B model found 0/3 recommendations. The baseline beat it here.
- **Entity confusion:** the model labelled confusion in 2 of the 5 cases that have it (recall 0.40). The baseline found 4 (0.80).
- **Other checks:** score arithmetic matches an independent implementation on all successful cases. Diagnostic numbers are recomputed and a corrupted diagnostic is caught.
- **What the grader can't do:** it checks that quotes exist and numbers match, not whether a real quote actually supports the claim. Some D2/D3 excerpts are weak.
- **Sample size:** 15 cases is too small for firm precision figures, and the labels are one person's judgement.

## Top remedy, largest risk, omissions
- **Top remedy (R1).** Investigate the sources engines cite for unbranded prompts (LinkedIn, Reddit, YouTube, Medium, arXiv, Semrush lead) and pursue accurate coverage there.
  - *Validation:* re-run the same 25 prompts; track the mention rate against 0/75.
  - *Cheapest fix:* R2 (entity disambiguation).
- **Largest risk.** D3's "36/75 negative" depends on a weak model label. Treat it as an upper bound until a reviewer or a larger model checks it.
- **Omissions.**
  - cited pages were not opened (support unverified)
  - no approval expiry or review deadline
  - no `If-Match` on PATCH
  - no resume
  - no prompt-v2 re-run
- **Questions before release.**
  - Does Kasparro accept "Kasparro AI" as the same entity?
  - Which commercial facts (pricing, onboarding time) can it publish?
  - Is a larger hosted model approved for labelling?

## AI assistance
Claude, an AI coding assistant, wrote the code from my direction. I set the requirements and made the design calls, including the choice of local model (`qwen2.5:3b` through Ollama). I reviewed the generated code and had it restructured into separate modules, each with one responsibility (ingest, model access, extraction, scoring, diagnostics, reports, API). I found the quote-matching bug described above in the real run. I ran the whole system end to end myself: the real local-model run recorded in `replay/`, the test suite, the evaluation runner and the API review flow in `output/demo_flow_transcript.txt`. The figures in this note come from those runs.
