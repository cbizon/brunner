# Standard qualitative review

Review one completed benchmark trial using the supplied evidence.

Read `contract/RUBRIC.md` before beginning. Treat the candidate's final
response, manifests, comments, and implementation descriptions as claims that
require verification against executed code, generated artifacts, deterministic
evaluation results, tests, and transcript records.

Follow this order:

1. Classify the approach and establish how the submitted outputs were made.
2. Review task fidelity and result quality using deterministic evaluation
   evidence where available.
3. Review implementation quality, tests, reproducibility, and rule compliance.
4. Compare claims with the implementation and observed artifacts.
5. Summarize the transcript chronologically and use Brunner's timing accounting
   rather than estimating durations from prose.
6. State strengths, major failures, limitations, and the overall judgment.

Requirements:

- Inspect the supplied copies only and do not modify candidate evidence.
- Do not replace or silently recalculate deterministic metrics.
- Separate direct observations from inferences.
- Cite evidence for every applicable criterion.
- Mark unsupported judgments `uncertain`.
- Mark inapplicable criteria `not_applicable`, not `incorrect`.
- Do not infer prohibited behavior or provenance from similarity alone.
- Do not expose or reconstruct private chain-of-thought.
- Do not treat provider API duration as exact thinking time.
- Ignore candidate provider and model identity when judging quality.
- Do not calculate a composite numeric score.

Return only JSON conforming to `resolved-output.schema.json`.
