---
name: r2-organizational-ghg
description: Generate an R2 organizational greenhouse-gas inventory draft when the user explicitly selected R2 and the source contains organization-level reporting-period or Scope data.
---

# Organizational GHG inventory report

Read `definition.json` before analysis and use it as the authoritative contract.

## Domain decisions

- Freeze organization identity, reporting year, consolidation boundary, and base year before totals.
- Keep Scope 1 source categories visible instead of collapsing unsupported components.
- Keep Scope 2 location-based and market-based results separate; never substitute one for the other.
- Represent Scope 3 as objects with `category` and numeric `emissions` fields, and distinguish omitted, screened-out, unavailable, and zero values.
- Trace emission factors, activity data, units, GWP basis, and recalculation decisions.
- Recompute registered totals with tools and surface disagreements with source totals as `CONFLICT`.

Ask the user when boundary method, reporting period, emission-factor basis, or GWP version is missing. Never call an inventory verified without explicit third-party evidence.
