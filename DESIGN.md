# Design: Taraba Central Billing and Collections

The operator-side sibling of GovPayHub (govpayhub.com, the Taraba State Central Billing System). It uses the same visual grammar so the public portal and the internal console read as one family. Tokens live in `:root` in `src/template.html`.

## World

- **Forest band.** Every view opens on a deep green band (`#0B3D2E`) lit by three soft green radial glows, copied exactly from GovPayHub's header. White type on it; secondary text is white at 85% and 66%, never grey.
- **Sheet rising out of the band.** The main white card pulls up 72px into the band (56px on phones, where it goes full-bleed with square corners), as GovPayHub's content card does.
- **Sidebar with uppercase section labels.** Overview, Billing, Reporting, Resources. The current page is a white pill with the outline shadow. On phones it becomes a drop-down panel behind a "Menu" button.
- **Section heads.** A 2px primary rule sits on a 1px hairline above a muted bold label (GovPayHub's "Features" heading).
- **Hairline figure grids.** Figures sit in cells separated by 1px gaps showing the border colour (GovPayHub's feature grid), not in separate cards.

## Tokens

| Role | Value |
|---|---|
| Band | `#0B3D2E` + radial glows |
| Mint (band accent, selected month) | `#72E3AD` |
| Canvas | `#F7F6F4` |
| Card | `#FFFFFF`; raised rows `#FBFAF9` |
| Border / strong | `#E3E2E0` / `#D2D0CB` |
| Ink / secondary / muted | `#171717` / `#3F3F3D` / `#6B6A67` |
| Primary (buttons, rules, focus) | `#275941` (GovPayHub primary `oklch(42% .068 160)`), hover `#1D4632`, soft `#E9F3ED` |
| Chart series (single) | `#277A55`, hover `#1C5E41` |
| Business size (ordinal ramp) | Nano `#82D2A8`, Small `#389469`, Medium `#1A5439` |
| Taxpayer class | MSMEs `#007D50`, Institutions `#D27908` (validated: CVD ΔE 8.9, contrast ≥ 3:1) |
| Negative / positive text | `#B42318` / `#17744A` |
| Radius | 8px controls and grids, 12px sheet, pills only on small chips |
| Outline shadow | `0 0 0 1px rgba(0,0,0,.08), 0 1px 2px rgba(30,32,36,.06), 0 4px 10px -2px rgba(0,0,0,.035)` |

## Type

Nunito (GovPayHub's face), embedded as woff2 (latin and latin-ext; the ext subset carries ₦). Base weight 500, tracking -0.015em; headings -0.025em. Band headline 44px/650; figure values 23px/700 (lead figure 32px); body 15px. `tabular-nums` only in table columns and axis ticks.

## Charts

- **The calendar is the chart.** The band's monthly column chart is also the period picker: each bar is a button. Selecting a bar scopes the whole dashboard to that month, and selecting it again returns to the year. Bars in scope are mint and the rest are white at 26%. Only the peak or selected month carries a value label.
- Horizontal bars: label and value on one line, a 10px bar beneath with a square baseline and a 4px rounded end, on a 2px track.
- Stacked split bars: 22px tall with 2px surface gaps, always with a legend that gives amount, share and paying count.
- Every mark has a hover and keyboard-focus tooltip with the exact Naira value.
- Revenue-head figures are apportioned from monthly receipts, and the view always says so.

## Motion

Only to show state changes: bar heights and widths ease out over 320ms (`cubic-bezier(.16,1,.3,1)`) when the period changes; controls transition in 120–150ms. All motion turns off under `prefers-reduced-motion`.

## Documents

The demand notice is a letterhead: the coat of arms, "Taraba State Government", a 2px forest rule, uppercase tracked field labels, a table with a forest grand-total row, the amount in words, a 12-month payment grid, and a status strip. Printing hides the band, sidebar and controls, and uses A4 with 14mm margins.
