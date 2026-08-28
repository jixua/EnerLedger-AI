---
name: r7-verification
description: Generate an R7 verification-opinion draft when the user explicitly selected R7 and the source contains an independent verification engagement, scope, procedures, findings, and draft opinion evidence.
---

# Verification report draft

Read `definition.json`. This skill produces a reviewable draft only; it cannot perform or sign an independent verification.

## Domain decisions

- Require named client, verification body, subject, period, scope, criteria, assurance level, and materiality threshold.
- Keep engagement evidence, procedures, samples, findings, corrective actions, and final opinion logically separate.
- Do not infer independence, competence, accreditation, site visits, sampling, or corrected findings without evidence.
- Preserve unresolved nonconformities and contradictory evidence; do not soften them to make an opinion pass.
- Use `待核查机构确认` unless the supplied source contains an authorized final opinion.
- Never generate a signature, seal, accreditation number, or signer identity that is not explicitly supported.

Pause when scope, criteria, assurance level, materiality, methods, or opinion basis is missing. Always include the draft and non-substitution disclaimer.
