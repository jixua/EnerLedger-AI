---
name: r1-product-carbon-footprint
description: Generate an R1 product carbon footprint draft when the user explicitly selected R1 and the source contains product lifecycle or product-level emissions evidence.
---

# Product carbon footprint report

Read `definition.json` before analysis and use it as the field, section, formula, and blocking contract.

## Domain decisions

- Establish a product identity, quantitative functional unit, and explicit system boundary before interpreting totals.
- Keep organization-level emissions separate unless the document provides a defensible allocation to the selected product.
- Preserve lifecycle stages and source units. Use registered conversion and total formulas rather than mental arithmetic.
- Record the GWP assessment basis and version; do not insert a current version merely because it is common.
- Treat omitted lifecycle stages as `MISSING` or `NOT_APPLICABLE` only with evidence; do not silently assume zero.
- Explain allocation, cutoff, exclusions, data quality, and uncertainty without presenting general guidance as a mandatory standard clause.

Ask the user when product identity, functional unit, boundary, or GWP basis is blocking or contradictory. A verification body or verified status requires explicit evidence. Submit ReportIR only after lifecycle totals replay and every required field has a status.
