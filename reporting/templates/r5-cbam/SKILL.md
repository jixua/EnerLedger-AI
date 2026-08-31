---
name: r5-cbam
description: Generate an R5 CBAM data-report draft when the user explicitly selected R5 and the source identifies a reporting period, covered goods, importer, exporter, and production installation.
---

# CBAM report

Read `definition.json` and use only the frozen regulation reference set for the run.

## Domain decisions

- Require reporting period, declarant, importer, exporter, CN code, covered category, quantity, installation, and calculation method.
- Preserve the distinction between actual and default values and record the applicable method and reference version.
- Keep direct, indirect, and precursor emissions separate before calculating totals and intensity.
- Preserve quantity and emissions units; never assume tonnes when the source uses another basis.
- Record origin carbon price with currency, period, paid status, and evidence. Do not estimate legal certificate obligations without an approved formula and frozen rule version.
- Treat official regulations, implementation guidance, FAQs, and company examples as different source classes.

Pause when identity, code, installation, period, quantity, method, or rule version is missing. The output is a preparation draft, not a filing or legal compliance opinion.
