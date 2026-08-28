---
name: r4-energy-audit
description: Generate an R4 energy-audit draft when the user explicitly selected R4 and the source contains facility, energy-bill, metering, system, or conservation-measure data.
---

# Energy audit report

Read `definition.json` before analysis and preserve the selected audit subject and period.

## Domain decisions

- Establish facility type, area, audit period, baseline period, audit level, energy carriers, and unit systems.
- Do not combine electricity, fuel, steam, heat, and other carriers until registered conversion factors and versions are available.
- Keep measured, billed, estimated, and calculated values distinguishable.
- Build system breakdowns from evidence and surface reconciliation gaps with facility totals.
- For each conservation measure, preserve assumptions, savings basis, investment, cost savings, dependencies, and implementation risk.
- Use registered formulas for comprehensive energy, intensity, savings rate, and payback. Reject division-by-zero or incompatible-unit inputs.

Ask for blocking facility, area, period, or conversion information. The output remains a draft until site conditions, meters, assumptions, and professional sign-off are independently checked.
