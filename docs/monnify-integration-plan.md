# Monnify integration plan — Taraba Central Billing and Collections

Rebuilding the payment side of the platform on **Monnify** (developers.monnify.com),
for the Taraba State Internal Revenue Service (TIRS).

- Written: 20 October 2026
- Status: **plan only.** No project source file has been changed by this document.
- **Update, 20 October 2026: built, with these decisions from the user that replace parts of this plan.**
  Everything runs on **one VPS** (Docker Compose: FastAPI + PostgreSQL + worker + Caddy), not Netlify Functions; read "Netlify Function" below as "endpoint in `server/app/`". The Netlify-specific caveats (frozen processes, Blobs, `netlify.toml`) no longer apply: the worker is a long-running container woken by PostgreSQL `NOTIFY`. The **staff console is online behind per-officer sign-in** (§6.3's "own sign-in" option, implemented with Argon2 and an audit log), not offline-only. Phase 0 (simulated checkout removed) is done and deployed. Phases 1–3 are implemented and tested locally against a stand-in for Monnify; see `deploy/README.md`. The 2025 "receipt" for workbook-only collections is now a **Statement of 2025 Collections** on the public page (§7.4); the staff console still offers its old receipt.
- Docs read: see [Appendix A — sources](#appendix-a--sources). Every technical claim below cites the page it came from. Anything not confirmed from the docs is marked **(to confirm)**.

---

## 0. Executive summary

| | |
|---|---|
| **Remove first, independent of Monnify** | The simulated card/OTP/USSD checkout in `src/template.html` (lines ~366–428 CSS, ~679–787 storage, ~1164–1614 modal). It collects card numbers and fake OTPs on a public government domain and reaches no bank. Phase 0. |
| **Recommended collection pattern** | **API-first one-off checkout per demand notice**: a Netlify Function authenticates to Monnify, calls `init-transaction` with the amount read server-side from the assessment, and redirects the taxpayer to the returned `checkoutUrl`. Later, add **dynamic invoices** so a printed notice carries a real bank account number. |
| **Rejected** | Browser-side Web SDK (amount is client-controlled), reserved/virtual accounts per taxpayer (needs each customer's BVN or NIN — TIRS has neither), transaction splitting (max 5 accounts, needs an indemnity). |
| **Datastore** | A hosted Postgres (Neon or Supabase). A `UNIQUE` constraint on Monnify's transaction reference is what makes duplicate webhooks safe; SQL is what makes the dashboard possible. Netlify Blobs is not suitable for a money ledger. |
| **Biggest risk, bigger than Monnify** | 39,981 taxpayers' names, addresses and phone numbers are embedded in a 6.2 MB HTML file on a public domain with no password. Phase 1 splits the build so the public page carries **no registry at all**. |
| **Hard gate on going live** | Every one of the 39,981 accounts is fully settled in the 2025 data. There is nothing to collect. The payment path cannot go live until a dataset with real outstanding balances exists. |

---

## 1. What the docs actually say

Confirmed facts, with the page each came from. These are the only facts the plan is built on.

### 1.1 Credentials and environments
- You need a Monnify merchant account (`app.monnify.com`) and an **API Key**, **Secret Key** and **Contract Code** from *Developers > API Keys & Contracts* on the dashboard. [quickstart]
- Sandbox base URL `https://sandbox.monnify.com`; live base URL `https://api.monnify.com`. Sandbox and live keys and contract codes are entirely separate. [quickstart], [live §1]
- The dashboard also exposes keys under *Settings > API Keys & Webhooks*, and the contract code under *Settings > Contracts*. [quickstart Path C]

### 1.2 Authentication
- `POST {base}/api/v1/auth/login` with `Authorization: Basic base64(apiKey:secretKey)`.
- Returns `responseBody.accessToken` and `expiresIn: 3600`. **Tokens expire after 1 hour.** Send as `Authorization: Bearer <token>` on every later call. [quickstart Path B Step 1], [checkout-api Step 1]

### 1.3 Initialize a transaction (the recommended pattern)
- `POST {base}/api/v1/merchant/transactions/init-transaction`, Bearer token.
- Request fields: `amount`, `customerEmail`, `paymentReference`, `paymentDescription`, `currencyCode`, `contractCode`, `redirectUrl`, `paymentMethods`, `metadata`. [checkout-api Step 2]
- Required: `amount`, `paymentReference`, `contractCode`; `redirectUrl` required for hosted checkout. `currencyCode` defaults to NGN. `paymentMethods` optional — omit to show all, **an empty array is an error**. [checkout-api field reference], [payment-methods]
- Response: `transactionReference`, `paymentReference`, `enabledPaymentMethod`, `checkoutUrl`. [checkout-api Step 2]
- **"Always confirm that the values returned by this endpoint match what you sent in the request payload. Bad actors can intercept requests and alter values such as the transaction amount."** [checkout-api Step 2]
- **`checkoutUrl` is valid for 40 minutes.** [checkout-api Step 3]
- After payment Monnify redirects to `redirectUrl?paymentReference=…&transactionReference=…&paymentStatus=PAID`. [checkout-api Step 4]
- `paymentReference` must be unique per transaction; reuse returns **"Duplicate payment reference"**. [quickstart], [checkout-api errors]
- Payment method keys: `ACCOUNT_TRANSFER`, `CARD`, `USSD`, `PHONE_NUMBER`, `PAY_WITH_BANK`. [payment-methods], [pay-with-bank]

### 1.4 Verification (never trust the browser)
- `GET {base}/api/v2/merchant/transactions/query?paymentReference=…` or `?transactionReference=…`, Bearer token. [verify-transactions]
- "Never trust a client-side callback or redirect alone. Always verify a transaction on your server… before delivering goods, granting access, or crediting a wallet." [verify-transactions]
- Response carries `transactionReference`, `paymentReference`, `amountPaid`, `totalPayable`, `settlementAmount`, `fee`, `paidOn`, `createdOn`, `completedOn`, `paymentStatus`, `paymentMethod`, `currencyCode`, `customerDTO`, `metaData`, `paymentSourceInformation[]` (bank code, account name, account number, session id). [verify-transactions]
- Statuses: `PAID`, `PARTIALLY_PAID`, `PENDING`, `OVERPAID`, `FAILED`, `REVERSED`, `EXPIRED`. [verify-transactions]
- **"By default, Monnify rejects over and under payments. The funds are returned to the sender and you receive a `REJECTED_PAYMENT` webhook instead."** `OVERPAID` / `PARTIALLY_PAID` only appear if the contract is configured to accept them (*Settings > Contracts Setup > Edit Contract*). [verify-transactions]
- "Only mark an order as fulfilled when `paymentStatus === "PAID"` **and** `amountPaid` matches your expected amount." [quickstart]
- `amountPaid` is gross; `settlementAmount` is after Monnify's fee. [verify-transactions], [reconciliation]

### 1.5 Webhooks
- Register URLs at *Developers > Webhook URLs*: Transaction Completion, Refund Completion, Disbursement, Settlement. [webhooks], [live §2]
- Payload shape is always `{ "eventType": …, "eventData": { … } }`. [webhooks]
- Collection event is `eventType: "SUCCESSFUL_TRANSACTION"`, with `eventData` carrying `transactionReference`, `paymentReference`, `paidOn`, `amountPaid`, `totalPayable`, `settlementAmount`, `paymentMethod`, `paymentStatus`, `currency`, `customer`, `paymentSourceInformation[]`, `destinationAccountInformation`, `product.{reference,type}`, `metaData`, `cardDetails`. [event-types]
- Other events: `SUCCESSFUL_DISBURSEMENT`, `FAILED_DISBURSEMENT`, `REVERSED_DISBURSEMENT`, `SUCCESSFUL_REFUND`, `FAILED_REFUND`, `SETTLEMENT`, `REJECTED_PAYMENT`, mandate status change, wallet activity, low balance. [webhooks], [event-types]
- **Signature:** validate the `monnify-signature` header by "computing an **HMAC-SHA512** hash of the request body using your client secret". All three code samples (Node, PHP, Java) compute `hmac_sha512(key = clientSecret, data = stringifiedRequestBody)` and compare hex. [webhooks], [event-types]
  - *Note the doc's own inconsistency:* the prose one line below also says "SHA-512 encoding of your client secret key and the object of the request body… `SHA-512(client secret key + object of request body)`". The Security Reminder and all three code samples say HMAC-SHA512 keyed with the secret. **Implement HMAC-SHA512 over the raw body.** Flagged as **(to confirm)** on first live transaction.
  - **The `monnify-signature` header is only sent in production. It is not present on sandbox notifications.** [webhooks], [event-types] — this is why API verification, not the signature, must be the thing that authorises a receipt.
  - Which secret signs it is **(to confirm)**: the docs call it "client secret" and the worked example uses a 32-character key (`91MUDL9N6U3BQRXBQ2PJ9M0PW4J22M1Y`) that does not look like the `SK_TEST_…` Secret Key used for Basic auth. Ask Monnify support to confirm before go-live.
- **Monnify's webhook source IP is `35.242.133.146`**; whitelist it so only that origin is accepted. [webhooks], [event-types]
- Best practices, verbatim intent: validate the hash; whitelist Monnify's IP; **check for duplicate notifications** ("A resend of already processed notification can happen if we do not get a 200 HTTP Status code, or in the case of a request time out"); **return 200 first, then do the slow work**. [webhooks]

### 1.6 Reconciliation
- `GET {base}/api/v1/merchant/transactions/search?page=&size=&paymentStatus=&from=&to=` — `from`/`to` are **Unix timestamps in milliseconds**; paginate until `page` reaches `responseBody.totalPages`. [reconciliation]
- Reconcile at two layers: transaction level (match on your `paymentReference`) and settlement level (sum `settlementAmount`). Fetch `PARTIALLY_PAID`, `OVERPAID` and `REVERSED` separately, not only `PAID`. [reconciliation]
- Recommended cadence: a scheduled job at 11 PM daily, after the 10 PM settlement. Keep a recon audit log. [reconciliation]

### 1.7 Settlement
- Two cycles: **Same Day** (bulk at 10 PM on the day of payment) and **Express** (on demand, dashboard only, max 3/day, min ₦5,000, free, no API). [settlements]
- Default cycle for Account Transfer / Card / USSD / Phone Number is **10 PM same day**. [settlements]
- Settlement destination is **Wallet or Bank Account**; it **currently defaults to Wallet** (*Settings > Contract Setup*). With Bank Account, funds go to the designated account in the next cycle. [live §6], [settlements]
- Retrieval APIs exist by settlement reference or transaction reference; a `SETTLEMENT` webhook fires on each settlement. [settlements], [event-types]

### 1.8 Reserved (dedicated virtual) accounts — why they are rejected for now
- `POST {base}/api/v2/bank-transfer/reserved-accounts` with `accountReference`, `accountName`, `currencyCode`, `contractCode`, `customerEmail`, `customerName`, `bvn` and/or `nin`, `getAllAvailableBanks`. [reserved-accounts]
- **"Please note that the BVN or NIN of your customer must be supplied when sending this request."** Error `BVN or NIN is required.` if omitted. [reserved-accounts]
- One account per customer: error `R42` / "You cannot reserve more than 1 account(s) for a customer", triggered by a repeated `customerEmail`. [reserved-accounts]
- Default virtual accounts are Moniepoint; `getAllAvailableBanks: true` spreads across partner banks. [reserved-accounts]
- Extras available: transaction limit profiles, `restrictPaymentSource` with allowed BVNs / account numbers / account names, `incomeSplitConfig`. [reserved-accounts]
- In sandbox, reserved-account payments must be completed with the **Monnify Payment simulator**; real transfers are not supported in sandbox. [reserved-accounts]

### 1.9 Invoices — the good fit for a printed demand notice
- `POST {base}/api/v1/invoice/create` with `invoiceReference`, `amount`, `invoiceDescription`, `contractCode`, `customerEmail`, `customerName`, `expiryDate`, `currencyCode`; `accountReference` additionally for static invoices. [invoice]
- **Dynamic invoice:** a new virtual account number is generated per invoice, plus a checkout URL. **No BVN/NIN required.** Expires immediately once paid; the account cannot be reused. [invoice]
- **Static invoice:** the same reserved account number every time for one customer — but it **requires the customer's BVN or NIN** and is dormant between invoices. [invoice]
- Duplicate `invoiceReference` returns "Invoice with this reference already exists." [invoice]
- **(to confirm)** How long a dynamic invoice's virtual account stays payable. The request takes an `expiryDate`, but the docs do not state a maximum, and the 40-minute figure elsewhere applies to the *checkout* session and to the dynamic transfer account inside checkout. [payment-methods], [checkout-api]

### 1.10 Transaction splitting / sub-accounts
- `POST {base}/api/v1/sub-accounts` (array body) with `currencyCode`, `bankCode`, `accountNumber`, `email`, `defaultSplitPercentage`. [transaction-splitting]
- Attach per payment via `incomeSplitConfig: [{ subAccountCode, feePercentage, splitPercentage | splitAmount, feeBearer }]`. One of `splitPercentage` *or* `splitAmount` per entry, never both. [transaction-splitting]
- **Hard ceiling: "Monnify can help you share the amount paid between up to five different accounts."** [transaction-splitting]
- **Not enabled by default.** Requires an email to `integration-support@monnify.com` stating the use case, and "you will be expected to indemnify Monnify for potential use and misuse of the feature". API and UI are activated separately. [transaction-splitting]

### 1.11 Offline cash pay-ins (Moniepoint agents) — strong later option
- Cash collection at any Moniepoint Business Owner location, with agents "across every local government in Nigeria". [offline-payins]
- **Not enabled by default**: email `integration-support@monnify.com` with the use case and an indemnity. [offline-payins]
- Flow: create an offline product; the taxpayer visits an agent; the agent enters the taxpayer's ID; **Monnify calls TIRS's own Payer Verification endpoint** (`POST`, body `{productCode, paymentRecipientId}`) and TIRS replies `{responseCode: "00", responseMessage, paymentRecipientId, paymentRecipientDescription}` — and for the `MERCHANT_INVOICE` product type, **also an `amount`**, so TIRS sets the amount per taxpayer at verification time. Unknown payer → `responseCode: "02"`. [offline-payins]
- Product types: Fixed, Variable, Invoice, Merchant Invoice. [offline-payins]

### 1.12 Other confirmed details
- Web SDK script: `https://sdk.monnify.com/plugin/monnify.js`; `MonnifySDK.initialize({ amount, currency, reference, customerFullName, customerEmail, apiKey, contractCode, paymentDescription, metadata, incomeSplitConfig, onLoadStart, onLoadComplete, onComplete, onClose })`. npm packages `monnify-js` / `monnify-ts`. [checkout-page], [quickstart Path A], [integration tools]
- Sandbox checkout host observed in the docs: `https://sandbox.sdk.monnify.com/checkout/<transactionReference>`. The live host is **(to confirm)** — presumably `https://sdk.monnify.com/checkout/…`, not stated. [checkout-api]
- Direct card charge (your own card form) **requires PCI-DSS certification**; apply to `integration-support@monnify.com`. [checkout-api], [payment-methods]
- Pay with Bank Transfer inside checkout: dynamic virtual account, **valid 40 minutes (2400 s)**; pass a `bankCode` to also get a USSD string. [checkout-api], [payment-methods]
- Refunds: `POST {base}/api/v1/refunds/initiate-refund`, status at `GET {base}/api/v1/refunds/<refundReference>`; `SUCCESSFUL_REFUND` / `FAILED_REFUND` webhooks. [refunds], [event-types]
- Sandbox test cards: `4111 1111 1111 1111` (no OTP), `5060 9959 9424 7093` (OTP `123456`), `4000 0000 0000 0002` (3DS), `4111 1111 1111 1110` (fails); CVV `123`, PIN `1234`. [test-cards]
- IP whitelisting *of your own server* is required **only for merchants using disbursement/transfers**, requested by email to `integration-support@monnify.com`, and needs a static IP. TIRS only collects, so TIRS does not need this. [live §3]
- Monnify generates synthetic customer emails itself in some flows (`111222333@ZZAMZ4WT4Y3E.monnify` appears in the official webhook sample), which supports using a synthetic per-notice email where a taxpayer has none. [webhooks]

---

## 2. Phase 0 — remove the simulated checkout (do this first, no credentials needed)

**Why first:** on `https://taraba.govpayhub.com` — a public, unauthenticated, government-branded domain — the current page presents a card form, a fake 3-D Secure OTP screen with an "⚡ Autofill OTP (123456)" button, a "Taraba State Treasury virtual account" to transfer real money to, USSD codes to dial, fake `TIRS-TXN-…` references and a downloadable PDF "receipt". Nothing reaches a bank. Whatever the intent, that is the exact shape of a payment-credential phishing page, carrying state insignia. It should come down before anything else is designed, and its removal does not depend on Monnify in any way.

### 2.0.1 Exact edits in `src/template.html`

| Lines (current file) | What is there | Action |
|---|---|---|
| ~366–428 | `/* payment modal */` CSS block: `.pm-backdrop` … `.pm-otp-inputs input:focus` and its `@media` rule, ending just before `/* tooltip */` | **Delete.** Keep `.pay-*` and `.doc-*` rules (used by the real pay page and the notice). |
| 682–689 | `STORAGE_KEY = "TIRS_PAYMENTS_DATA_v1"`, `getLocalStore()`, `saveLocalStore()` | **Delete.** |
| 711–727 | the block inside `prepare()` that reads the local store and mutates `pay`, `paidAll`, `monthTotal`, `localTxns` | **Delete.** `prepare()` then returns figures that come only from the workbook. Drop `localTxns` from the returned object. |
| 733–761 | `recordPayment()` — invents `TIRS-TXN-…` references | **Delete.** |
| 763–787 | `setUnpaidOverride()` — the "unpaid test mode" | **Delete.** |
| 1167–1614 | `/* Payment checkout modal (Card, Bank Transfer, USSD) */`: `pmState`, `detectScheme`, `formatCardNum`, `formatExp`, `closePaymentModal`, `openPaymentModal`, `renderPaymentModal`, `startProcessing`, `bindPaymentModalEvents` | **Delete the whole block** (ends immediately before `function renderNotice(id)` at 1616). |
| 1639, 1671 | in `renderNotice()`: the `#ntestunpaid` button and its handler | **Delete.** |
| 1640, 1670, 1672 | in `renderNotice()`: `#npay` → `openPaymentModal(r)`, `#nrestore` → `setUnpaidOverride` | **Delete** `#nrestore`. Replace `#npay` with a link to `#/pay/<DN>` (Phase 3 wires that page to Monnify). |
| 1706–1711 | in `renderPay()`: "Quick test notices (click to view & test payment)" chips | **Delete.** |
| 1735–1738, 1747–1750 | in `renderPay()`: `#ppayextra`, `#ptestunpaid`, `#presetsettled` and their handlers | **Delete.** Keep `#prcpt`. Keep `#ppay` but have it do nothing yet (hidden while the notice is settled — which, in the 2025 data, is always). |
| 1721, 1741, 1781–1786, 1816 | `localTxn` use in `renderPay()` and `downloadReceipt()` — prints the invented reference and channel on the PDF | **Delete all four.** The 2025 receipt then shows only what the workbook supports. |
| 1743 | "Pay online instantly using Debit Card, Direct Bank Transfer, or USSD." | **Replace** with wording that is true today, e.g. "Online payment is not yet available for this notice. Pay at any TIRS office or bank branch quoting your Assessment Number." |
| 2342 | `document.addEventListener("keydown", …)` references `pmState` / `closePaymentModal()` | **Simplify** to `if (e.key === "Escape") closeMenu();` |

Then `python build.py` and deploy. `grep -c "pmState\|localTxn\|recordPayment\|setUnpaidOverride\|STORAGE_KEY" src/template.html` must return 0.

### 2.0.2 `PRODUCT.md`
- **Line 35** describes the simulated checkout as a product capability ("an interactive GovPayHub-themed checkout modal supports Debit Card (with 3D-Secure OTP verification)… Successful settlements persist in browser storage… generate official transaction references (`TIRS-TXN-...`)… Settled notices can also receive supplementary payments or be toggled into unpaid test mode."). **Replace** with: the Pay page finds a notice by Assessment Number or TTIN and shows its status from the 2025 collections return; online payment is planned on Monnify and is not live.
- **Line 59** ("No outstanding balances, arrears, payment dates, receipt numbers, or payment channels exist in the data; none may be invented.") already forbids exactly what line 35 described. Keep line 59 — it is the principle the rest of this plan follows.
- Add a line under *Operating Context*: payments will be collected through Monnify, verified server-side, and recorded in a ledger separate from the spreadsheet.

### 2.0.3 Redeploy
`deploy.cmd -Prod -Yes` (or `.\deploy.ps1 -Prod`). No Monnify credentials, no backend, no CSP change needed — Phase 0 only removes code.

**Effort: half a day.** **Do not wait for any other decision on this plan.**

---

## 3. Choosing the Monnify collection pattern

### 3.1 The four candidates

| Pattern | Docs | Fit for a Consolidated Demand Notice | Verdict |
|---|---|---|---|
| **A. Web SDK in the browser** — `MonnifySDK.initialize({amount, reference, apiKey, contractCode, …})` | [quickstart Path A], [checkout-page] | Fastest to build; no server auth; works inside the existing single-file page. **But `amount` and `reference` are supplied by the browser.** A taxpayer can open dev tools, set `amount: 1`, pay ₦1 and receive a genuine `PAID` transaction. Server verification then catches the mismatch — after TIRS has taken ₦1 it must refund. Also needs `script-src https://sdk.monnify.com` plus the SDK's iframe and XHR hosts in the CSP. | **Rejected as primary.** Unacceptable for statutory revenue. |
| **B. API-first → hosted `checkoutUrl`** — server `init-transaction`, then redirect | [quickstart Path B], [checkout-api Option A] | Amount and reference are fixed server-side from the authoritative assessment and cannot be edited. Monnify's own page handles card, transfer, USSD, phone number and Pay with Bank. PCI scope stays with Monnify. Needs no CSP loosening at all, because a `location.assign()` navigation is not governed by `connect-src`, `script-src`, `frame-src` or `form-action`. Keys stay in a Netlify Function. | **Recommended primary.** |
| **C. Dynamic invoice** — `POST /api/v1/invoice/create` | [invoice] | `invoiceReference` = the DN number, `amount` = the balance, `expiryDate` = the due date. Returns **both** a virtual account number and a checkout URL, and needs **no BVN/NIN**. The account number can be printed on the paper notice, so a taxpayer with no smartphone can walk into any bank and transfer. This is the single biggest usability win available. | **Recommended as a second phase**, once (to confirm) the account's payable window is long enough to cover a 30-day notice. |
| **D. Reserved account per taxpayer** | [reserved-accounts] | Would give each TTIN a permanent account number — attractive in principle. **Blocked**: every reservation needs that customer's BVN or NIN, and the workbook has neither (phone numbers exist for institutions only, no emails at all). One account per `customerEmail`. Reserving 39,981 accounts is also a KYC/AML programme, not an integration task. | **Rejected now.** Revisit only for a small set of large institutions where TIRS can lawfully obtain a director's BVN/NIN. |
| **E. Transaction splitting / sub-accounts** | [transaction-splitting] | Could route each payment to several agency bank accounts. **But the ceiling is five accounts per transaction** and the platform has 13 revenue heads; and the feature needs activation plus an indemnity letter to Monnify. | **Rejected.** Settle to one consolidated TIRS revenue account and keep apportionment in the reporting layer, where the platform already does it (and already states that it apportions — `DESIGN.md`, "Revenue-head figures are apportioned from monthly receipts, and the view always says so"). If TIRS insists on separate agency accounts, the five-account ceiling is a blocking constraint to raise with Monnify first. |
| **F. Offline cash pay-ins at Moniepoint agents** | [offline-payins] | Many Taraba MSMEs pay cash. An agent in every LGA, with Monnify calling a TIRS Payer Verification endpoint and TIRS returning the taxpayer's name **and the amount due** (`MERCHANT_INVOICE` product type) is almost exactly a demand-notice lookup. Needs activation plus an indemnity. | **Recommended as an optional later phase.** High value; put it to TIRS as a question. |

### 3.2 Recommendation

**Phase 3: pattern B** (server-initialised transaction, redirect to `checkoutUrl`).
**Phase 5: add pattern C** (dynamic invoice per notice) so the printed notice carries a real account number, and keep B as the "pay online now" route.
**Do not use A, D or E.** Ask TIRS about F.

What would change this: if Monnify confirms a dynamic invoice's virtual account stays payable for the full 30-day notice period, C becomes the headline channel and B becomes the convenience route — which is the better end state. If Monnify says a dynamic invoice account is short-lived, B stays primary and the notice keeps directing taxpayers online.

---

## 4. Architecture

```
Taxpayer (public internet)
   │  scans QR on notice →  https://taraba.govpayhub.com/#/pay/DN175804411896133
   ▼
dist/index.html  ── PUBLIC build: no registry data, ~120 KB
   │  fetch /.netlify/functions/notice-lookup?dn=…        (one notice only)
   │  POST  /.netlify/functions/payment-start             (returns checkoutUrl)
   ▼
Netlify Functions  ── hold MONNIFY_* secrets, never shipped to the browser
   │                            ▲
   │  Monnify REST              │  monnify-webhook  ◄── POST from 35.242.133.146
   ▼                            │
Monnify (sandbox.monnify.com | api.monnify.com)
   │
   ▼
Postgres (Neon / Supabase) ── assessments, payment_intents, payments,
                              webhook_events, legacy_collections, lookup_audit

Staff (offline)
   taraba-billing.html  ── the existing self-contained file: dashboard,
                           registry, reports. Not published publicly.
```

### 4.1 Why Netlify Functions
The site is already on Netlify with a linked site (`.netlify/state.json`) and a working deploy script. Functions are the smallest possible addition: no new host, no new domain, same-origin so `connect-src 'self'` is enough, and secrets live in Netlify environment variables where the user has already said they belong.

**Concrete gap to fix:** there is **no `netlify.toml`** in the repo, and `deploy.ps1` line 142 deploys with `netlify deploy --dir <dist>` only. That publishes the static folder and **no functions**. Add:

```toml
# netlify.toml  (new file, repo root)
[build]
  publish = "dist"

[functions]
  directory = "netlify/functions"
  node_bundler = "esbuild"

[functions."monnify-webhook"]
  # webhook must answer fast; see §4.5
  timeout = 10

[[redirects]]
  from = "/api/*"
  to = "/.netlify/functions/:splat"
  status = 200
```

and change `deploy.ps1`'s `$deployArgs` to include `'--functions', (Join-Path $root 'netlify\functions')`. Belt and braces: the toml alone usually suffices, but an explicit flag makes a silent "pay button 404s in production" failure impossible.

### 4.2 Datastore — recommendation and reasons

**Recommended: a hosted Postgres — Neon (or Supabase, if its auth is also wanted later).**

Reasons, in order of weight:
1. **Idempotency is a uniqueness problem.** `UNIQUE (monnify_transaction_reference)` plus `INSERT … ON CONFLICT DO NOTHING` makes a duplicate webhook a no-op at the storage layer, atomically, under concurrency. The docs explicitly warn that resends happen on non-200 or timeout [webhooks]. Nothing else on the shortlist gives this for free.
2. **It is money.** A revenue ledger wants ACID writes, a `NOT NULL` schema, foreign keys to the assessment, and an append-only audit trail with the raw payload kept in `JSONB` for disputes.
3. **The dashboard needs aggregation.** Collections by month, by LGA, by category, top contributors — that is `GROUP BY`. Doing it by listing 40,000 key-value blobs on every page load is not viable.
4. **Reconciliation needs set operations.** "Which Monnify transactions have no matching intent" and vice versa are two anti-joins [reconciliation].
5. **Cost and ops.** Neon's free tier scales to zero and is far larger than needed: ~40,000 payments a year is a few megabytes.

**Rejected: Netlify Blobs.** It is the obvious "already on Netlify" answer and it is genuinely good for *derived* artefacts — see below — but it is a key-value store without documented conditional writes, so the "has this webhook already been processed?" check becomes a read-then-write race, and aggregate reporting over 40,000 keys means paginated listing. Do not put the ledger there.

**Do use Netlify Blobs for one thing:** a nightly *published snapshot* — a small gzipped JSON of `{dn → {assessed, paid, balance, status}}` — so the staff dashboard can load a period view in one fetch instead of hammering Postgres. Derived, regenerable, non-authoritative.

**Also rejected:** SQLite in the repo (Functions have an ephemeral filesystem; no shared writes), Google Sheets as a backend (no constraints, rate-limited, and it reintroduces exactly the "spreadsheet as database" failure mode), and Netlify's own form handling (not a ledger).

### 4.3 Functions to write (`netlify/functions/`)

| File | Method | Public? | Job |
|---|---|---|---|
| `notice-lookup.js` | GET | yes, rate-limited | `?dn=` or `?ttin=` → **exactly one** notice: name, address, assessment lines, total, paid, balance, due date, status. Never phone. Never a list. |
| `payment-start.js` | POST | yes, rate-limited | Body `{dn, email?}`. Reads the balance **from the DB**, refuses ≤ 0, creates/increments a `payment_intents` row, gets a Monnify token, calls `init-transaction`, **asserts the response's `amount`/`paymentReference` match what was sent** [checkout-api], stores `transactionReference`, returns `{checkoutUrl, expiresAt}`. |
| `payment-return.js` | GET | yes | Monnify's `redirectUrl` target. Takes the query params as a *hint only*, calls `verify-transaction.js` logic, then 302s to `/#/pay/<dn>?result=paid|pending|failed`. |
| `monnify-webhook.js` | POST | Monnify only | Signature + IP check, **200 immediately**, then enqueue/do the ledger write. §4.5. |
| `verify-transaction.js` | internal | — | Shared module: token cache, `GET /api/v2/merchant/transactions/query`, status/amount assertion, ledger upsert. The **only** code path that may mark anything paid. |
| `payment-status.js` | GET | yes, rate-limited | `?dn=` → current status + the receipt token if settled. Lets the pay page poll while a webhook is in flight. |
| `receipt-data.js` | GET | yes, token-scoped | `?ref=<transactionReference>&t=<hmac>` → verified receipt fields. The page draws the PDF with the already-inlined jsPDF. |
| `recon-daily.js` | scheduled | — | 23:10 WAT. `transactions/search` for the day across `PAID`, `PARTIALLY_PAID`, `OVERPAID`, `REVERSED`; write a `recon_runs` row with discrepancies [reconciliation]. |
| `monnify-token.js` | internal | — | Caches the Bearer token for < 55 min (`expiresIn: 3600` [quickstart]). Cache in module scope *and* a `monnify_token` DB row, because Function instances are short-lived. |

Staff-side (only if the staff console goes online — Phase 6): `staff-summary.js`, `staff-registry.js`, `staff-report.js`, all behind authentication.

### 4.4 Server-side verification rule (the core invariant)

> **Nothing is marked paid except by `verify-transaction.js`, and only when `paymentStatus === "PAID"` and `amountPaid >= expected_amount_kobo/100` for that `paymentReference`.**

The browser's redirect params and the webhook body are **triggers**, never evidence. This follows the docs directly [verify-transactions], [quickstart], and it is also what makes the unconfirmed signature format safe: if HMAC validation fails for a formatting reason, the webhook is logged with `signature_ok = false` and **still** triggers an API verification, so a signature surprise degrades into a log entry rather than a wrong receipt or a missed payment.

### 4.5 Webhook handling, step by step

```
1. Read the RAW body. In a Netlify Function this is `event.body` (and possibly
   base64 — honour `event.isBase64Encoded`). Compute HMAC-SHA512 over those
   exact bytes with MONNIFY_CLIENT_SECRET, hex, and timing-safe compare to the
   `monnify-signature` header.
   *** NEVER hash JSON.stringify(JSON.parse(body)). Key order and whitespace
   must survive untouched, or every signature fails. ***
2. Check `x-nf-client-connection-ip` against 35.242.133.146. Treat a mismatch as
   a WARNING, not a rejection: Netlify is not an IP firewall and the signature
   plus step 6 are the real controls.
3. If MONNIFY_ENV = sandbox, expect NO signature header and skip step 1 — the
   docs state it is production-only [webhooks].
4. INSERT INTO webhook_events (event_type, transaction_reference, payload,
   signature_ok) ... ON CONFLICT (transaction_reference, event_type)
   DO NOTHING RETURNING id.
   No row returned  →  already seen  →  return 200 and stop. (Idempotency.)
5. return 200 NOW. (Docs: acknowledge first, then do slow work [webhooks].)
6. Asynchronously: call GET /api/v2/merchant/transactions/query, then apply the
   ledger write in verify-transaction.js.
```

**Netlify caveat:** a Function's process can be frozen the moment the response is returned, so "return 200 then keep working" is not reliable. Two safe shapes:
- **(preferred)** step 5 returns 200 after only the `webhook_events` insert; a **Netlify scheduled function running every minute** picks up rows where `processed_at IS NULL` and does step 6. The insert is tiny, so the 200 is fast, and nothing is lost if the instance dies.
- **(acceptable)** do step 6 inline before returning 200, and keep the function well inside its timeout. Simpler, and one `query` call is fast — but a Monnify slowdown then causes a resend, which step 4 already makes harmless.

Pick the first. It is the only version that is correct if the process is killed.

**Reversals:** `REVERSED` is a real status [verify-transactions]. The ledger must accept a reversal row that takes a notice back to outstanding, and the receipt endpoint must refuse to issue for a reversed transaction. Design for it in Phase 3; do not retrofit.

### 4.6 Reconciliation
`recon-daily.js`, scheduled 23:10 WAT (after the 10 PM settlement cycle [settlements], as the docs recommend [reconciliation]):
1. `transactions/search` the day's window (ms timestamps), paginating to `totalPages`, once per status in {`PAID`,`PARTIALLY_PAID`,`OVERPAID`,`REVERSED`}.
2. Anti-join both ways against `payments`:
   - in Monnify, not in our ledger → `MISSING_IN_DB` (a lost webhook) → verify and insert.
   - in our ledger, not in Monnify → `MISSING_AT_MONNIFY` → alert, never auto-resolve.
   - amount differs → `AMOUNT_MISMATCH` → alert.
3. Sum `settlementAmount` for the day and store it as the expected settlement, to be checked against the `SETTLEMENT` webhook and the bank statement [reconciliation].
4. Write a `recon_runs` row (counts + the discrepancy list) — the audit trail the docs ask for, and the thing a TIRS finance review will want.

---

## 5. Data model

### 5.1 Tables

```sql
-- From the workbook. Rebuilt by the build pipeline; never written by a payment.
assessments (
  dn                   text PRIMARY KEY,          -- build.py demand_notice_number()
  tax_year             int  NOT NULL,
  reg_id               text NOT NULL,             -- TR/MSME/2025/00001
  ttin                 text,
  name                 text NOT NULL,
  entity_type          text NOT NULL,
  category             text NOT NULL,
  lga                  text NOT NULL,
  business_size        text NOT NULL,
  address              text NOT NULL,
  phone                text,                      -- NEVER returned to the public
  heads                jsonb NOT NULL,            -- [{head, amount_kobo}, ...]
  subtotal_fees_kobo   bigint NOT NULL,
  total_assessed_kobo  bigint NOT NULL,
  assessment_date      date NOT NULL,
  due_date             date NOT NULL,
  source_workbook      text NOT NULL,
  synced_at            timestamptz NOT NULL,
  UNIQUE (reg_id, tax_year)
)

-- The 2025 monthly columns. Immutable, no payment instrument behind them.
legacy_collections (
  id serial PRIMARY KEY,
  dn text REFERENCES assessments(dn),
  period date NOT NULL,                           -- first of the month
  amount_kobo bigint NOT NULL,
  source text NOT NULL DEFAULT 'workbook-2025',
  UNIQUE (dn, period)
)

-- One row per payment attempt. Gives each attempt its own unique reference.
payment_intents (
  payment_reference      text PRIMARY KEY,        -- TIRS-<DN>-<taxyear>-<attempt>
  dn                     text NOT NULL REFERENCES assessments(dn),
  attempt                int  NOT NULL,
  expected_amount_kobo   bigint NOT NULL,
  customer_email         text NOT NULL,
  transaction_reference  text,                    -- filled from init-transaction
  checkout_url           text,
  status                 text NOT NULL,           -- CREATED|REDIRECTED|PAID|FAILED|EXPIRED
  created_at             timestamptz NOT NULL DEFAULT now(),
  expires_at             timestamptz NOT NULL,    -- created_at + 40 min
  UNIQUE (dn, attempt)
)

-- THE LEDGER. Append-only. One row per Monnify transaction.
payments (
  transaction_reference  text PRIMARY KEY,        -- Monnify's MNFY|...  <-- idempotency
  payment_reference      text NOT NULL,
  dn                     text NOT NULL REFERENCES assessments(dn),
  payment_status         text NOT NULL,           -- PAID|PARTIALLY_PAID|OVERPAID|REVERSED|...
  amount_paid_kobo       bigint NOT NULL,         -- gross; what settles the notice
  total_payable_kobo     bigint,
  settlement_amount_kobo bigint,                  -- net to the bank
  fee_kobo               bigint,
  payment_method         text NOT NULL,           -- ACCOUNT_TRANSFER|CARD|USSD|PHONE_NUMBER|CASH
  paid_on                timestamptz NOT NULL,
  payer_account_name     text,                    -- paymentSourceInformation[0]
  payer_account_masked   text,                    -- last 4 only
  verified_at            timestamptz NOT NULL,    -- when OUR server verified it
  raw_verify             jsonb NOT NULL,          -- the query response, for disputes
  reversal_of            text REFERENCES payments(transaction_reference)
)

-- Idempotency + audit for every inbound notification.
webhook_events (
  id serial PRIMARY KEY,
  event_type            text NOT NULL,
  transaction_reference text,
  payload               jsonb NOT NULL,
  signature_ok          boolean NOT NULL,
  source_ip             text,
  received_at           timestamptz NOT NULL DEFAULT now(),
  processed_at          timestamptz,
  process_error         text,
  UNIQUE (transaction_reference, event_type)
)

lookup_audit (id serial PRIMARY KEY, ip_hash text, query_kind text,
              hit boolean, at timestamptz DEFAULT now())
recon_runs   (id serial PRIMARY KEY, ran_for date, counts jsonb,
              discrepancies jsonb, at timestamptz DEFAULT now())
```

### 5.2 Source of truth going forward

| Fact | Authority | Never |
|---|---|---|
| Who is assessed, and for how much | the workbook → `assessments` | a payment may not alter an assessment |
| What was collected in 2025 | the workbook's monthly columns → `legacy_collections` | may not be given a transaction reference, channel or day |
| What has been collected from go-live onward | `payments`, written only by verified Monnify transactions | may not be invented, back-dated or entered by hand |

`balance(dn) = total_assessed_kobo − Σ legacy_collections − Σ payments(amount_paid_kobo, status PAID/OVERPAID, minus reversals)`.

The existing `ROUNDING = 5` kobo tolerance in `src/template.html` line 667 must stay: the workbook rounds some line items, so a fully-paid 2025 account can differ from its assessment by a few kobo. Apply it to the legacy component only — a live Monnify balance is exact and needs no tolerance.

### 5.3 How each view reads live status

| View | Today | After |
|---|---|---|
| `#/pay/<DN>` (public) | reads the embedded dataset + `localStorage` | `notice-lookup` for the assessment, `payment-status` for live paid/balance |
| `#/notice/<DN>` (public) | reads the embedded dataset | same two functions; notice wording and layout unchanged |
| `#/dashboard` (staff) | `D.monthTotal` from the workbook | workbook months **plus** a live-payments series, drawn as a visually distinct segment and labelled by source. Never silently merged — a manager must be able to see which naira came from the 2025 return and which from the portal. |
| `#/registry` (staff) | `statusPill()` at line 1044 from `D.paidAll` | `D.paidAll` + the nightly Blobs snapshot, with a "live as of HH:MM" stamp |
| `#/reports` (staff) | months from the workbook | add a **Monnify collections** report (date range, transaction reference, method, gross, fee, net) and a **Reconciliation** report. The statutory breakdown stays exactly as it is — it must keep reproducing the workbook byte-for-byte. |

### 5.4 The "nothing to pay" problem — and the hard gate

Every one of the 39,981 accounts is fully settled in the 2025 data (₦8,528,514,150.77 assessed and collected). So:

- **There is nothing for Monnify to collect today.** Phases 0–4 can all be built and sandbox-tested, but the live payment path must not be switched on against a dataset where every balance is zero. A "Pay now" button that cannot be pressed by anyone is at best confusing and at worst invites someone to re-create a test mode.
- **Build and test against a separate assessment set**, never the production one. Add a `--tax-year` / `--workbook` path to `build.py` (it already accepts a workbook argument, line 320) and keep a `fixtures/TIRS_2026_sandbox.xlsx` with the same businesses and zero collections. Load it only into the sandbox database.
- **The honest UI state for a settled notice** is: status *Settled*, balance ₦0.00, no payment control, and a statement download. `renderPay()` already renders this shape; Phase 0 just removes the "Test as unpaid" escape hatch.
- **Gate:** go-live for collections requires (a) a dataset with real outstanding balances, (b) a live contract code and verified settlement account, (c) a successful live ₦100 end-to-end test that reconciles the next morning. Until then, deploy the Monnify code **disabled by a `PAYMENTS_ENABLED=false` environment variable** so the public page shows the "not yet available" state and `payment-start` returns 503.

### 5.5 Three data traps specific to this codebase

1. **DN numbers must become immutable once money is attached.** `build.py`'s `demand_notice_number()` (line 180) derives the DN from `sha256(reg_id|year)`. It is stable *given the same reg_id and year* — but a data clean-up that renumbers a business, or a change of tax year, silently produces a new DN and **orphans every payment made against the old one**. The assessment sync step must refuse to change or delete a DN that has rows in `payments`, and fail the build loudly instead.
2. **There are no email addresses in the registry.** `init-transaction` requires `customerEmail` [checkout-api]. Policy: offer an optional email field on the pay page (used only to send the receipt) and otherwise synthesise `dn-<DN>@notices.taraba.govpayhub.com`. Monnify's own webhook sample contains a synthetic address of this kind (`111222333@ZZAMZ4WT4Y3E.monnify` [webhooks]), which is good evidence a non-deliverable domain is accepted — still worth one sandbox test.
3. **`paymentReference` must be unique per *attempt*, not per notice.** Reuse returns "Duplicate payment reference" [checkout-api]. A taxpayer who abandons checkout and comes back 10 minutes later needs a fresh reference — hence `payment_intents.attempt` and the reference shape `TIRS-<DN>-<taxyear>-<attempt>`. Keep the DN inside the reference so a reference is self-describing in a Monnify dashboard export.

---

## 6. Privacy and security

### 6.1 The problem, stated plainly
`dist/index.html` is 6.2 MB and contains, gzipped but trivially extractable, the **name, business address and (for institutions) phone number of 39,981 Taraba businesses and institutions**, served from `https://taraba.govpayhub.com` with no password. `deploy.ps1` even warns about it on lines 14–16 and 133–134. Anyone with the URL has the whole registry. That is a larger exposure than any payment question in this document, and no CSP header mitigates it, because the data is *in* the page.

### 6.2 The split

**Two builds from one `src/template.html`, driven by a build flag.**

| | Public build → `dist/index.html` | Staff build → `taraba-billing.html` |
|---|---|---|
| Data embedded | **none** | all 39,981 records (as today) |
| Size | ~120 KB | ~6.2 MB |
| Routes | `#/pay`, `#/pay/<DN>`, `#/notice/<DN>`, `#/verify/<ref>` | `#/dashboard`, `#/registry`, `#/notice/<id>`, `#/reports` (+ pay, read-only) |
| Gets its data from | `notice-lookup` / `payment-status`, one notice at a time | the embedded dataset, as now |
| Name search / autocomplete | **removed** (`lookupMatches()` line 1130, `bindLookup()` line 1136) | kept |
| Where it lives | Netlify, public | **not published**: distributed to staff PCs / an internal file share |

Implementation in `build.py`: a `--public` flag that replaces `__DATA_GZ_B64__` with an empty dataset and sets a `MODE = "public"` constant the template branches on; the router (line 2345) whitelists routes per mode. `deploy.ps1` stages the `--public` output as `dist/index.html`. The 6.2 MB staff file keeps being built exactly as now.

**Why keeping the staff tool offline is the recommendation, not a compromise:** `PRODUCT.md` principle 4 already says "Works for the officer on a slow office PC with no connection", and the file already opens from disk. Making the staff console an offline artefact removes 39,981 people's addresses from the public internet **this week, at zero cost, with no new vendor and no authentication to build**. Everything else on the list is slower and weaker.

### 6.3 If TIRS wants the staff console online anyway (Phase 6)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Netlify password protection** on a `/staff/*` path | 5 minutes to set up; nothing to build | one shared password; no idea who looked at what; per-path protection is a paid plan feature | Acceptable **interim only**. A tax registry needs named accountability. |
| **Netlify Identity** | built in; JWT arrives in `context.clientContext.user`; invite-only | Netlify has been steering new sites away from Identity; **(to confirm)** whether it can still be enabled on this site at all | Only if it is actually still available. |
| **Supabase Auth** (if Postgres is Supabase) | one vendor for DB + auth; per-officer accounts; MFA; RLS; sessions auditable | couples auth to the DB choice | **Recommended** if Supabase is chosen in §4.2. |
| **Auth0 / Clerk** | strongest per-user auth, MFA, SSO to a state directory later | another vendor, another bill | **Recommended** if Neon is chosen. |
| **Own gateway function + signed cookie, officers in Postgres** | no vendor, full control of the audit log | we own every authentication bug; slowest and riskiest to get right | Not recommended. |

Whatever is chosen: every staff data function logs *who* ran *which* query *when*. A registry of 40,000 citizens' addresses should not be readable without a name attached to the read.

### 6.4 Enumeration protection on the public lookup

The public `notice-lookup` is the one door left open, so it gets narrow:

1. **Accept a full Assessment Number (DN) or a full TTIN. Nothing else.** No registration IDs (`TR/MSME/2025/00001` is sequential and would let anyone walk the registry), no partial names, no autocomplete, no result lists. The public build must not contain `lookupMatches()`.
2. **Return the minimum.** Name, address, assessment lines, totals, due date, status. **Never the phone number.** The name and address are needed because the notice letterhead shows them, and the taxpayer is holding the paper notice that carries the DN — proportionate, but it is a disclosure and should be a conscious decision, not a side effect.
3. **Rate limit on misses, not on hits.** A real taxpayer makes one or two lookups and succeeds. An enumerator generates a flood of misses. Per `sha256(ip + daily_salt)`: 10 misses/minute → 429; 50 misses/hour → 1-hour block; a global misses-per-minute ceiling as a circuit breaker. Keep counters in `lookup_audit` (or a Blobs counter — this is exactly the derived, loseable state Blobs suits).
4. **Uniform failure.** One message — "No demand notice matches that number" — for malformed, unknown and blocked alike. No "that TTIN exists but the DN is wrong".
5. Guessing a DN is already hard: `DN` + year + class digit + 5-digit serial + 5 check digits from `sha256(reg_id|year)` (`build.py` line 180), so ~10^5 guesses per known serial. **Do not weaken this** by ever exposing the generator or by switching to sequential numbering.
6. `Cache-Control: no-store` on every function response; `Referrer-Policy: no-referrer` already set.
7. **Do not plan any step that reads the live public site for verification.** Deploys are verified from the local `dist/` build and from Netlify's own deploy record, which `deploy.ps1` already parses (lines 152–160).

---

## 7. Receipts

### 7.1 A receipt may only exist for a verified Monnify transaction
`downloadReceipt()` (`src/template.html` line 1754) currently prints `localTxn.ref` — an invented `TIRS-TXN-…` string — onto a PDF headed "PAYMENT RECEIPT" with a green "SETTLED" badge. That must go in Phase 0 and be rebuilt in Phase 3 from `receipt-data.js`, which serves only rows in `payments`.

### 7.2 What the receipt shows
From the verified `query` response [verify-transactions]:

- TIRS letterhead, coat of arms, "PAYMENT RECEIPT" (unchanged design — `DESIGN.md` "Documents")
- **Assessment Number** (DN) and **TIRS TIN**
- Tax year, assessment date
- **Monnify transaction reference** (`transactionReference`, e.g. `MNFY|67|…`) — the number a taxpayer or auditor can quote to Monnify
- **TIRS payment reference** (`paymentReference`, `TIRS-<DN>-<year>-<n>`)
- **Amount paid** in figures and words (`amountPaid`, gross) — `noticeWords()` already does the words
- **Date and time paid** (`paidOn`, rendered in WAT, stated as WAT)
- **Channel**, mapped for a human: `ACCOUNT_TRANSFER` → "Bank transfer", `CARD` → "Debit card", `USSD` → "USSD", `PHONE_NUMBER` → "Phone number", `PAY_WITH_BANK` → "Pay with Bank", `CASH` → "Cash at Moniepoint agent"
- Revenue-head table from the assessment, and **balance remaining** after this payment
- A **verification QR** to `https://taraba.govpayhub.com/#/verify/<transactionReference>`, which re-queries Monnify live. A receipt anyone can check is worth more than a receipt that merely looks official.
- Footer: `generatedLine()` as today — "Generated <date> · Taraba State Internal Revenue Service"

### 7.3 What it must never show
Card digits or any card data; the payer's full bank account number (last 4 only); Monnify's `fee` or `settlementAmount` (the taxpayer paid the gross — the net is TIRS's internal business); anything for a `REVERSED` transaction; and any reference, channel or date that did not come from Monnify.

### 7.4 The 2025 data is not a receipt — rename it
For the 39,981 accounts settled in the 2025 return, TIRS holds **no payment instrument**: no reference, no date, no channel (`PRODUCT.md` line 59 says so explicitly). A PDF headed "PAYMENT RECEIPT" with a "SETTLED" badge therefore asserts more than the data supports.

**Recommendation:** rename that artefact **"Statement of 2025 Collections"** — same design, same figures, honest claim: "Our records show this assessment as fully settled from collections recorded in the 2025 consolidated revenue return. This is a statement of account, not a payment receipt." Keep the word *receipt* for documents backed by a Monnify transaction. This is a decision for the user (open question Q6), but it is the one that matches `PRODUCT.md`'s own first principle.

---

## 8. CSP, deploy and environment

### 8.1 CSP — the recommended pattern barely needs a change
Current (`deploy.ps1` lines 81–90, `dist/_headers`):

```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data:; font-src data:; connect-src 'none'; base-uri 'none';
form-action 'none'; frame-ancestors 'none'
```

Public build, after Phase 3 — **one token changes**:

```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data:; font-src data:; connect-src 'self'; base-uri 'none';
form-action 'none'; frame-ancestors 'none'
```

`connect-src 'self'` is all that is needed, because the page only ever `fetch`es its own `/.netlify/functions/*`. The hand-off to Monnify is a top-level `location.assign(checkoutUrl)`, and a plain navigation is not restricted by `connect-src`, `script-src`, `frame-src` or `form-action` (the proposed `navigate-to` directive was never shipped). **This is a concrete reason to prefer redirect-based checkout over the Web SDK**, which would instead require allowing `https://sdk.monnify.com` in `script-src` plus its iframe and XHR hosts in `frame-src` / `connect-src` — hosts the docs do not enumerate, so they would have to be discovered by observation.

Add to `_headers`:
```
/.netlify/functions/*
  Cache-Control: no-store
  X-Content-Type-Options: nosniff
```
Keep `X-Robots-Tag: noindex` and `robots.txt` — taxpayers arrive by QR code, not by search.

**Later hardening (optional):** drop `'unsafe-inline'` from `script-src` by having `build.py` compute the SHA-256 of the single inline `<script>` and emit `script-src 'sha256-…'` into `_headers`. Both files are generated by the same script, so they cannot drift.

**If the Web SDK is ever adopted after all**, the extra hosts must be established empirically in sandbox (load the SDK with `Content-Security-Policy-Report-Only` and collect violations) — do not guess them. **(to confirm)**

### 8.2 Environment variables (Netlify → Site configuration → Environment variables)

The user enters these in the Netlify UI. **They are never pasted into chat, never committed, and never placed in the HTML.**

| Variable | Scope | Notes |
|---|---|---|
| `MONNIFY_ENV` | Functions | `sandbox` \| `live` |
| `MONNIFY_BASE_URL` | Functions | `https://sandbox.monnify.com` \| `https://api.monnify.com` [live §1] |
| `MONNIFY_API_KEY` | Functions | `MK_TEST_…` / `MK_PROD_…` |
| `MONNIFY_SECRET_KEY` | Functions | **secret** — Basic auth half |
| `MONNIFY_CONTRACT_CODE` | Functions | **must match the environment** [live §4] |
| `MONNIFY_CLIENT_SECRET` | Functions | **secret** — webhook HMAC key. **(to confirm)** whether this equals `MONNIFY_SECRET_KEY` |
| `MONNIFY_WEBHOOK_IP` | Functions | `35.242.133.146` [webhooks] |
| `DATABASE_URL` | Functions | **secret** |
| `PUBLIC_SITE_URL` | Functions | `https://taraba.govpayhub.com` — builds `redirectUrl` |
| `PAYMENTS_ENABLED` | Functions | `false` until the go-live gate in §5.4 is cleared |
| `LOOKUP_IP_SALT` | Functions | **secret** — rotates daily-ish; hashes IPs in `lookup_audit` |

**Scope them to Functions / runtime only, not to the build.** If the Monnify secret is visible to the build step, a future `build.py` change could inline it into a 6 MB HTML file, and Netlify's build logs could echo it. Also add `.env`, `.env.*` and `netlify/functions/.env` to `.gitignore` (which currently only ignores `.netlify`).

### 8.3 Deploy changes (`deploy.ps1` / `deploy.cmd`)
1. Add the `netlify.toml` in §4.1.
2. `$deployArgs` (line 142): add `'--functions', (Join-Path $root 'netlify\functions')`.
3. Staging (lines 71–97): stage the **`--public`** build as `dist/index.html`; write the new `_headers`.
4. The production confirmation prompt (lines 132–137) currently warns that the deploy publishes all taxpayer records. After Phase 1 that is no longer true — update the wording, and keep a prompt that states what *is* being published.
5. Add a `-Sandbox` switch that refuses to deploy to production while `MONNIFY_ENV=sandbox`, so a sandbox-keyed build cannot reach the live domain.
6. Node dependencies: Functions will need a `package.json` at the repo root (`pg` or `@neondatabase/serverless`; Node 18+ has `fetch` and `crypto.createHmac` built in, so no HTTP or crypto dependency is needed). Keep the **staff** build dependency-free and offline, exactly as it is.

### 8.4 Sandbox-first testing plan
1. Sandbox keys from *Developers > API Keys & Contracts*; `MONNIFY_BASE_URL=https://sandbox.monnify.com` [quickstart].
2. `auth/login` → assert a token and cache it for < 55 min [quickstart].
3. `init-transaction` for a fixture notice → assert the echoed `amount` and `paymentReference` match the request [checkout-api].
4. Pay on the sandbox checkout with each test card: `4111 1111 1111 1111` (no OTP), `5060 9959 9424 7093` (OTP `123456`), `4000 0000 0000 0002` (3DS), `4111 1111 1111 1110` (must fail cleanly) [test-cards].
5. Bank transfer in checkout: confirm the 40-minute expiry is displayed, and that an expired session produces `EXPIRED`, not a stuck notice [checkout-api], [payment-methods].
6. Webhook: point the sandbox Transaction Completion URL at the deployed function. **Expect no `monnify-signature` header** [webhooks] — so test the HMAC path with locally generated requests built from the docs' worked example (client key `91MUDL9N6U3BQRXBQ2PJ9M0PW4J22M1Y`, the sample body, and the published expected hash), which is the only way to validate the implementation before live traffic.
7. Replay the same webhook 5× → exactly one `payments` row, four no-ops (idempotency).
8. Deliberately drop a webhook (return 500 once) → confirm Monnify resends and/or `recon-daily` repairs it.
9. Amount tampering: call `payment-start`, then try to complete a different amount → the notice must stay outstanding and raise a discrepancy.
10. Under/over-payment: confirm the sandbox contract's configuration and that `REJECTED_PAYMENT` is handled [verify-transactions].
11. `transactions/search` for the test day → reconciliation clean [reconciliation].
12. Receipt: generated only for a `PAID` row; refused for `PENDING`, `FAILED` and `REVERSED`.

### 8.5 Go-live checklist (from [live], [webhooks], [settlements])
- [ ] Compliance documents uploaded and the Monnify account activated [live]
- [ ] `MONNIFY_BASE_URL` → `https://api.monnify.com` [live §1]
- [ ] Live API key, secret key **and contract code** swapped; no sandbox contract code reused [live §1, §4]
- [ ] Transaction Completion webhook URL set and reachable; Refund and Settlement URLs set [live §2]
- [ ] Signature validation implemented and verified on the **first live transaction** (it cannot be verified in sandbox) [webhooks]
- [ ] Monnify's IP `35.242.133.146` checked in the webhook function [webhooks]
- [ ] Own-server IP whitelisting: **not required** — that is for disbursement merchants only, and TIRS does not disburse [live §3]
- [ ] Over/under-payment configuration consciously chosen in *Settings > Contracts Setup* (default is **reject**) [live §4], [verify-transactions]
- [ ] Settlement account verified in *Settings > Bank Accounts*: correct name, number and bank, validated to receive inflow [live §5]
- [ ] Settlement report email address configured [live §5]
- [ ] Settlement method chosen — Wallet (the default) vs Bank Account [live §6], [settlements]
- [ ] `PAYMENTS_ENABLED=true` only after a live ₦100 end-to-end test that appears in the next morning's reconciliation
- [ ] A named TIRS officer owns the daily reconciliation report

### 8.6 What TIRS must obtain from Monnify
1. A merchant account at `app.monnify.com` in the name of the entity that will lawfully hold the funds — a legal/treasury decision, not a technical one.
2. KYC/compliance documents accepted and the account activated [live].
3. Sandbox **and** live API Key, Secret Key and Contract Code [quickstart], [live].
4. Written confirmation of **which secret signs the `monnify-signature` header** (the "client secret") [webhooks] — **(to confirm)**.
5. A **verified settlement bank account** = the designated Taraba State revenue account [live §5].
6. Webhook URLs registered on the dashboard [live §2].
7. The contract's over/under-payment policy and settlement destination set deliberately [live §4, §6].
8. **The fee schedule in writing, and who bears the fee.** If TIRS bears it, a ₦100,000 assessment is settled in full but less than ₦100,000 reaches the treasury — the ledger already separates `amount_paid_kobo` from `settlement_amount_kobo` for exactly this. If the taxpayer bears it, `totalPayable > amount` and the receipt must show both [verify-transactions].
9. Only if TIRS wants them, each by email to `integration-support@monnify.com` **with an indemnity**: sub-accounts [transaction-splitting], offline cash pay-ins [offline-payins]; and PCI-DSS certification for a custom card form [checkout-api] — which this plan recommends against.
10. **(to confirm)** the live checkout host, and the payable lifetime of a dynamic invoice's virtual account [invoice].

---

## 9. Build order, effort and risk

### 9.1 Phases

| Phase | Work | Needs credentials? | Rough effort |
|---|---|---|---|
| **0. Remove the simulation** | §2: strip the modal, storage, test modes and invented references from `src/template.html`; correct `PRODUCT.md` lines 35 and 59; rebuild; `deploy.cmd -Prod -Yes` | **No** | **0.5 day** |
| **1. Privacy split** | `build.py --public`; mode-aware router; drop name search from the public build; stage the public build in `deploy.ps1`; new `_headers`; update the deploy warning. **39,981 people's addresses leave the public internet.** | No | 2–3 days |
| **2. Backend skeleton** | `netlify.toml`, root `package.json`, Postgres provisioned, schema from §5.1, `sync_assessments` loader with the DN-immutability guard, `notice-lookup` + `payment-status` with rate limiting, public page fetching one notice. Monnify calls behind a **mock driver** so the whole flow is testable with no keys. | No | 3–5 days |
| **3. Monnify sandbox** | `monnify-token`, `payment-start`, `payment-return`, `verify-transaction`, `monnify-webhook` + the one-minute processor, idempotency, reversals, `receipt-data`, rebuilt receipt PDF, the §8.4 test matrix | **Yes — sandbox keys** | 5–8 days |
| **4. Reconciliation + staff views** | `recon-daily` scheduled function, `recon_runs`, discrepancy alerting, nightly Blobs snapshot, dashboard live-collections series, Monnify collections + Reconciliation reports | Sandbox | 3–5 days |
| **5. Dynamic invoices on the notice** | `invoice/create` per notice, print the virtual account number and expiry on the paper notice, handle invoice-paid webhooks, `build.py` emits the account number into the notice | Sandbox, plus the §1.9 confirmation | 2–4 days |
| **6. Optional** | (a) Offline cash pay-ins: the Payer Verification endpoint + `MERCHANT_INVOICE` product — 3–5 days **plus Monnify activation and an indemnity**. (b) Online staff console with real per-officer auth — 5–8 days. (c) Refunds workflow — 2–3 days. | Varies | — |
| **Go-live** | §8.5 checklist, then `PAYMENTS_ENABLED=true` | **Live keys** | 1 day + a settlement cycle |

Estimates are build-and-test effort for one developer and exclude TIRS's account opening, KYC and any Monnify activation waits, which are usually the longer poles.

### 9.2 Doable before any credentials exist
All of Phase 0, all of Phase 1, all of Phase 2, and the entire structure of Phase 3 — with `MONNIFY_ENV=mock`, a local fake that returns the documented response shapes for `auth/login`, `init-transaction` and `transactions/query`, and that posts the documented `SUCCESSFUL_TRANSACTION` body at the webhook. The HMAC implementation can even be validated before credentials, against the worked example and published hash in the webhook docs. When sandbox keys arrive, only the base URL and the driver change.

### 9.3 Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Webhook signature format uncertain** (which secret; and the docs' prose contradicts their own code samples; header absent in sandbox) | Live webhooks rejected, or accepted unverified | Verify every transaction via the API regardless of signature result (§4.4); log `signature_ok` without blocking; validate the HMAC against the docs' worked example before go-live; confirm the secret with Monnify |
| **Netlify freezes the Function after the response**, so "200 then work" silently loses the write | Payments recorded late or not at all | Persist first, process in a one-minute scheduled function (§4.5); nightly reconciliation as the backstop |
| **Raw-body mangling** before the HMAC | 100% signature failure | Hash `event.body` bytes exactly; never re-serialise; honour `isBase64Encoded`; unit-test against the docs' sample |
| **DN renumbering orphans payments** | A taxpayer's money attached to a notice that no longer exists | Sync step refuses to change a DN with payment rows; fail the build |
| **Deploy without `--functions`** | Pay button 404s in production | `netlify.toml` **and** the explicit flag; a smoke check in `deploy.ps1` after deploy |
| **Nothing is outstanding in the 2025 data** | A payment path that nobody can use, tempting someone to re-add a test mode | `PAYMENTS_ENABLED=false`; separate sandbox fixture dataset; the §5.4 gate |
| **Under/over-payment rejected by default** | A taxpayer transfers the wrong amount, funds bounce back, taxpayer believes they paid | Decide the contract setting deliberately; handle `REJECTED_PAYMENT`; show the exact amount prominently and make it uneditable |
| **Fee bearer undecided** | Notices settled in full while less reaches the treasury, discovered at audit | Record gross and net separately from day one; get the fee schedule in writing (Q2) |
| **Public lookup enumerated** | Partial registry extraction despite Phase 1 | DN/TTIN only, no name search, miss-based rate limiting, uniform failure, audit log (§6.4) |
| **Secret leaked into the 6 MB HTML** | Full compromise of the merchant account | Scope env vars to Functions only; add a `grep -c 'MK_\|SK_' ` guard in `build.py` that fails the build |
| **`REVERSED` after a receipt was issued** | TIRS has certified a payment that was undone | Model reversals in Phase 3; verification QR on the receipt re-checks live status |

---

## 10. Open questions for TIRS

Ranked by how much they block the build.

1. **Who holds the money?** Which legal entity opens the Monnify merchant account, and which verified bank account receives settlement [live §5]? Blocks go-live entirely.
2. **Who bears the Monnify fee** — the state or the taxpayer? Changes what the receipt says and how a notice is considered settled.
3. **Are partial payments or instalments allowed** on a Consolidated Demand Notice? Monnify **rejects under-payment by default**; accepting it is a deliberate contract setting [verify-transactions].
4. **One consolidated revenue account, or separate agency accounts?** If separate, note Monnify's hard **five sub-accounts per transaction** ceiling against 13 revenue heads [transaction-splitting] — this needs resolving with Monnify before it is designed.
5. **Is there a 2026 assessment dataset with real outstanding balances, and when?** Nothing can be collected until there is (§5.4).
6. **Should the 2025 "Download receipt" become "Statement of 2025 Collections"?** TIRS holds no payment reference, date or channel for those months (§7.4).
7. **Who may see the full registry, and must the staff console be online at all,** or is the existing offline file enough (§6.2)?
8. **Does TIRS want cash collection at Moniepoint agents** in every LGA? It needs an activation request and an indemnity letter to Monnify [offline-payins].
9. **May TIRS lawfully use the registry's phone numbers** to send payment confirmations, and should the pay page ask for an email at all?
10. **Refunds:** who authorises one, over what threshold, and within what window [refunds]?
11. **Does the Objection paragraph on the notice need rewording** now that payment will be online? The wording is currently kept verbatim and reads awkwardly (`src/template.html` line 1663); changing it is a legal decision, not a design one.

---

## Appendix A — sources

All pages read on 20 October 2026 by rendering them in headless Chrome (the site returns HTTP 403 to plain fetches).

| Short name used above | URL |
|---|---|
| integration tools | https://developers.monnify.com/docs/integration |
| collections overview | https://developers.monnify.com/docs/collections |
| quickstart | https://developers.monnify.com/docs/collections/quickstart |
| payment-methods | https://developers.monnify.com/docs/collections/payment-methods |
| one-time-payments | https://developers.monnify.com/docs/collections/one-time-payments |
| checkout-page | https://developers.monnify.com/docs/collections/one-time-payments/checkout-page |
| checkout-api | https://developers.monnify.com/docs/collections/one-time-payments/checkout-api |
| pay-with-bank | https://developers.monnify.com/docs/collections/one-time-payments/pay-with-bank |
| payment-links | https://developers.monnify.com/docs/collections/one-time-payments/payment-links |
| invoice | https://developers.monnify.com/docs/collections/one-time-payments/invoice |
| offline-payins | https://developers.monnify.com/docs/collections/one-time-payments/offline-payins |
| reserved-accounts | https://developers.monnify.com/docs/collections/recurring-payments/reserved-accounts |
| verify-transactions | https://developers.monnify.com/docs/collections/manage-payments/verify-transactions |
| transaction-splitting | https://developers.monnify.com/docs/collections/manage-payments/transaction-splitting |
| refunds | https://developers.monnify.com/docs/collections/manage-payments/refunds |
| reconciliation | https://developers.monnify.com/docs/collections/manage-payments/reconciliation |
| webhooks | https://developers.monnify.com/docs/webhooks |
| event-types | https://developers.monnify.com/docs/webhooks/event-types |
| settlements | https://developers.monnify.com/docs/settlements |
| live | https://developers.monnify.com/docs/live |
| test-cards | https://developers.monnify.com/docs/test-cards |
| error-codes | https://developers.monnify.com/docs/error-codes |

Not read in depth (not relevant here): disbursements, bills payment, wallets, verification APIs (BVN/NIN), direct debit/mandates, card tokenisation, international payments, supported banks, changelogs, MCP server.

### Items explicitly marked "to confirm"
1. Which secret signs the `monnify-signature` header ("client secret" vs the `SK_…` Secret Key) [webhooks].
2. The exact canonical byte form the signature is computed over — unverifiable in sandbox, since the header is production-only [webhooks].
3. The live checkout host (only the sandbox host `sandbox.sdk.monnify.com` appears in the docs) [checkout-api].
4. How long a **dynamic invoice's** virtual account remains payable, and whether `expiryDate` has a maximum [invoice].
5. Whether a non-deliverable synthetic `customerEmail` is accepted on `init-transaction` (strongly suggested by Monnify's own sample payloads, but worth one sandbox call) [webhooks].
6. Whether partial payments produce `PARTIALLY_PAID` on this contract, or `REJECTED_PAYMENT` — depends on a dashboard setting [verify-transactions].
7. Whether **Netlify Identity** can still be enabled on this site (a Netlify question, not a Monnify one).
8. The host list the **Web SDK** needs in a CSP, if the Web SDK is ever adopted — must be found by observation, not guessed.

### Not planned, deliberately
- No step requires loading the live public site or scraping it for taxpayer data. Deploy verification uses the local `dist/` build and Netlify's deploy record.
- No secret appears in this document, in the repository, or in chat. Credentials are entered by the user in the Netlify dashboard.
