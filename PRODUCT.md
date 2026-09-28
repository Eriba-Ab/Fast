# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Single self-contained offline HTML file (user's choice). The 39,981 registry records are embedded at build time by a Python build script (`build.py`) that reads the single consolidated registry workbook; re-running the script regenerates the file when new data arrives. No server, no install, no network dependency at runtime.

## Users

Taraba State Internal Revenue Service (TIRS) staff: revenue managers and executives who track how much has been collected and when, and revenue officers who look up a business, print its demand notice, and pull period reports for meetings and returns.

## Product Purpose

Taraba Central Billing and Collections is the state's internal view of MSME billing: who is assessed, what they owe under the harmonized levy schedule, and what has been collected month by month. Success is a manager answering "how much did we collect in March, and from which LGAs?" in seconds, and an officer downloading a report for any date range without asking IT.

## Positioning

It works from the state's own Consolidated Demand Notice (CDN) billing registry, the same harmonized assessment that GovPayHub (the public Taraba State Central Billing System at govpayhub.com) issues to taxpayers. It is the operator-side counterpart to that public portal.

## Operating Context

- Source data: **one spreadsheet only** (user requirement): `Taraba State_Consolidated Revenue Collections Report_2025.xlsx` (the latest cleaned dataset), sheet "Master Registry", 39,981 rows (34,981 MSMEs, 5,000 institutions; IDs `TR/MSME/2025/00001`, `TR/INST/2025/00001`). Earlier source files are kept in `archive/` and are not used.
- Columns (36): MSME Registration ID, Entity Type (MSME / Institution), Business Name, Business Category (11), LGA (16), Business Address, Phone Number (institutions only, stored as numbers), Business Size (Nano / Small / Medium), seven fee lines, Sub-Total Fees, Licences, Fines, Sales, Earnings, Rent on Government Property, Rent on Land, Total Harmonized Assessment, Jan-2025 to Dec-2025, Total Billed 2025. Values only (no formulas). Revenue heads are read from the sheet, so heads can be added or dropped without code changes.
- Monthly columns are **payments collected** that month (re-confirmed by the user for this file, despite the "Total Billed 2025" header, which is the sum of the months). In this dataset every row reconciles exactly: sub-total, assessment and monthly collections agree to the kobo.
- Collections are recorded per month, not per day; date-range reports resolve to whole months.
- The statutory breakdown is one report, not split: the whole registry sheet regenerated in its own design (sheet name, headers, column order, fonts, fills, widths, frozen panes, number formats and any formulas), all read by `build.py` from the workbook itself.
- Exported documents never show the source file name; they carry "Generated <date> · Taraba State Internal Revenue Service".
- Payments are not split by revenue head in the source. Any revenue-head view of a period apportions receipts in proportion to each business's assessment and must say so.

## Capabilities and Constraints

- Dashboard with collections viewable by month, by year, and all periods together.
- Date-range parameters to download reports.
- Taxpayer registry with search and filters (ID, name, LGA, category, size).
- Printable Consolidated Demand Notice per business.
- Breakdowns by LGA, business category, and business size.
- Offline: must open from disk on an ordinary office PC.

## Brand Commitments

- Name: "Taraba Central Billing and Collections" (TIRS).
- Taraba State coat of arms: `Taraba_State_Coat_of_Arms.png`.
- Visual family: the user asked that it follow the design style of govpayhub.com, Taraba State's public Central Billing System.
- Currency is Naira (₦); figures are official revenue records and are shown precisely.

## Evidence on Hand

- The 2025 consolidated revenue collections workbook (39,981 businesses; ₦8,528,514,150.77 assessed and collected).
- Coat of arms image.
- No outstanding balances, arrears, payment dates, receipt numbers, or payment channels exist in the data; none may be invented.

## Product Principles

1. Figures are official: exact Naira amounts, sources and apportioning stated, never rounded away where a report needs them.
2. Every view answers "how much, when, from where" without extra clicks.
3. Anything on screen can be taken away as a report for the same period.
4. Works for the officer on a slow office PC with no connection.
