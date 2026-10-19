# Term Sheet Reconciliation Report

Generated 2026-10-19T15:05:20+00:00 · model `openai/gpt-oss-20b` · booking source: both · 10 trade(s) across 2 term sheet(s)

## Outcome

| Match | False Alert | True Mismatch | Parsing Error | Unverifiable | Unresolved | Failed LLM calls |
|---:|---:|---:|---:|---:|---:|---:|
| 136 | 0 | 15 | 0 | 39 | 0 | 0 |

*Match / False Alert / True Mismatch* are reconciliation outcomes. *Parsing Error* (a value failed schema or evidence validation), *Unverifiable* (the term sheet does not state the field) and *Unresolved* (the second LLM pass could not reach a defensible verdict) are exceptions shown separately.

## Trades

| Term sheet | Trade | ISIN | Overall | Match | False Alert | True Mismatch | Parsing Error | Unverifiable | Unresolved |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| IDBI_Omni_Term_Sheet.pdf | 1 | INE008A08U84 | **BREAK** | 11 | 0 | 2 | 0 | 6 | 0 |
| IDBI_Omni_Term_Sheet.pdf | 2 | INE008A08U84 | **BREAK** | 10 | 0 | 3 | 0 | 6 | 0 |
| IDBI_Omni_Term_Sheet.pdf | 3 | INE008A08U84 | **BREAK** | 11 | 0 | 3 | 0 | 5 | 0 |
| IDBI_Omni_Term_Sheet.pdf | 4 | INE008A08U84 | **BREAK** | 10 | 0 | 3 | 0 | 6 | 0 |
| IDBI_Omni_Term_Sheet.pdf | 5 | INE008A08U84 | **BREAK** | 11 | 0 | 2 | 0 | 6 | 0 |
| Genel_Energy_Term_Sheet.pdf | 1 | NO0010894330 | **CLEAN** | 17 | 0 | 0 | 0 | 2 | 0 |
| Genel_Energy_Term_Sheet.pdf | 2 | NO0010894330 | **BREAK** | 16 | 0 | 1 | 0 | 2 | 0 |
| Genel_Energy_Term_Sheet.pdf | 3 | NO0010894330 | **CLEAN** | 17 | 0 | 0 | 0 | 2 | 0 |
| Genel_Energy_Term_Sheet.pdf | 4 | NO0010894330 | **BREAK** | 16 | 0 | 1 | 0 | 2 | 0 |
| Genel_Energy_Term_Sheet.pdf | 5 | NO0010894330 | **CLEAN** | 17 | 0 | 0 | 0 | 2 | 0 |

## Exceptions requiring action

| Term sheet | Trade | Field | Term sheet value (page) | Booking value | Status | Explanation |
|---|---|---|---|---|---|---|
| IDBI_Omni_Term_Sheet.pdf | 1 | Maturity | PERPETUAL (p. 1) | 2025-10-17 | **True Mismatch** | The term sheet specifies the bond is perpetual, meaning it has no fixed maturity date, while the booking record contains a specific maturity date of 2025‑10‑17, which contradicts the perpetual nature described. |
| IDBI_Omni_Term_Sheet.pdf | 1 | BusinessDayConvention | Preceding for non‑coupon dates, next for coupon dates (p. 15) | Business day adjustment | **True Mismatch** | The term sheet specifies distinct conventions for non‑coupon and coupon dates, whereas the booking record only lists a generic "Business day adjustment" without distinguishing the two cases, indicating a mismatch in the economic terms. |
| IDBI_Omni_Term_Sheet.pdf | 2 | Maturity | PERPETUAL (p. 1) | 2025-10-17 | **True Mismatch** | The term sheet specifies the bond is perpetual, meaning it has no fixed maturity date, while the booking record contains a specific maturity date of 2025‑10‑17, which contradicts the perpetual nature described. |
| IDBI_Omni_Term_Sheet.pdf | 2 | Coupon | 10.75 (p. 3) | 11.0 | **True Mismatch** | The term sheet specifies a coupon of 10.75%, while the booking records a coupon of 11.0%, a difference of 0.25 percentage points that is not explained by any representational rule or adjustment. |
| IDBI_Omni_Term_Sheet.pdf | 2 | BusinessDayConvention | Preceding for non‑coupon dates, next for coupon dates (p. 15) | Business day adjustment | **True Mismatch** | The term sheet specifies distinct conventions for non‑coupon and coupon dates, whereas the booking record only lists a generic "Business day adjustment" without distinguishing the two cases, indicating a mismatch in the economic terms. |
| IDBI_Omni_Term_Sheet.pdf | 3 | Maturity | PERPETUAL (p. 1) | 2025-10-17 | **True Mismatch** | The term sheet specifies the bond is perpetual, meaning it has no fixed maturity date, while the booking record contains a specific maturity date of 2025‑10‑17, which contradicts the perpetual nature described. |
| IDBI_Omni_Term_Sheet.pdf | 3 | Notional | denomination 1000000 (p. 3) | 1100000 | **True Mismatch** | The booking records a notional of 1,100,000, which is not a whole multiple of the bond denomination of 1,000,000 stated in the term sheet, violating the rule that a trade notional must be an integer number of bonds. |
| IDBI_Omni_Term_Sheet.pdf | 3 | BusinessDayConvention | Preceding for non‑coupon dates, next for coupon dates (p. 15) | Business day adjustment | **True Mismatch** | The term sheet specifies distinct conventions for non‑coupon and coupon dates, whereas the booking record only lists a generic "Business day adjustment" without distinguishing the two cases, indicating a mismatch in the economic terms. |
| IDBI_Omni_Term_Sheet.pdf | 4 | Maturity | PERPETUAL (p. 1) | 2025-10-17 | **True Mismatch** | The term sheet specifies the bond is perpetual, meaning it has no fixed maturity date, while the booking record contains a specific maturity date of 2025‑10‑17, which contradicts the perpetual nature described. |
| IDBI_Omni_Term_Sheet.pdf | 4 | IssueDate | 2014-10-17 (p. 8) | 2014-10-10 | **True Mismatch** | The term sheet specifies an Issue Date of 2014‑10‑17, while the booking record shows 2014‑10‑10, a 7‑day difference that cannot be explained by formatting, units, rounding or a business‑day convention. |
| IDBI_Omni_Term_Sheet.pdf | 4 | BusinessDayConvention | Preceding for non‑coupon dates, next for coupon dates (p. 15) | Business day adjustment | **True Mismatch** | The term sheet specifies distinct conventions for non‑coupon and coupon dates, whereas the booking record only lists a generic "Business day adjustment" without distinguishing the two cases, indicating a mismatch in the economic terms. |
| IDBI_Omni_Term_Sheet.pdf | 5 | Maturity | PERPETUAL (p. 1) | 2025-10-17 | **True Mismatch** | The term sheet specifies the bond is perpetual, meaning it has no fixed maturity date, while the booking record contains a specific maturity date of 2025‑10‑17, which contradicts the perpetual nature described. |
| IDBI_Omni_Term_Sheet.pdf | 5 | BusinessDayConvention | Preceding for non‑coupon dates, next for coupon dates (p. 15) | Business day adjustment | **True Mismatch** | The term sheet specifies distinct conventions for non‑coupon and coupon dates, whereas the booking record only lists a generic "Business day adjustment" without distinguishing the two cases, indicating a mismatch in the economic terms. |
| Genel_Energy_Term_Sheet.pdf | 2 | Maturity | 2025-10-14 (p. 1) | 2025-10-15 | **True Mismatch** | The term sheet specifies a maturity date of 14 October 2025, while the booking record shows 15 October 2025, a one‑day discrepancy that is not justified by any rule or convention in the provided data. |
| Genel_Energy_Term_Sheet.pdf | 4 | Coupon | 9.25 (p. 1) | 9.5 | **True Mismatch** | The term sheet specifies a coupon rate of 9.25%, while the booking records a coupon of 9.5%. This is a substantive difference in the economic term, not a formatting or rounding issue. |

## False alerts (differences explained as non-economic)

None.

## Fields that could not be verified against the term sheet

| Term sheet | Field | Trades | Booking value(s) | Reason |
|---|---|---|---|---|
| IDBI_Omni_Term_Sheet.pdf | ISIN | 1, 2, 3, 4, 5 | INE008A08U84 | Term sheet does not state this field (ISIN not stated) |
| IDBI_Omni_Term_Sheet.pdf | Parent | 1, 2, 3, 4, 5 | IDBI Bank Limited | Term sheet does not state this field (No parent named) |
| IDBI_Omni_Term_Sheet.pdf | Notional | 1, 2, 4, 5 | 1000000 | Term sheet does not state a trade notional (the issue size is a different quantity). Consistency check passed: 1000000 = 1 bond(s) x 1000000. Note: below the primary-market minimum subscription of 5000000. |
| IDBI_Omni_Term_Sheet.pdf | SettlementDate | 1, 2, 3, 4, 5 | 2014-10-17 | Term sheet does not state this field (Only a date range given) |
| IDBI_Omni_Term_Sheet.pdf | IssuePrice | 1, 2, 3, 4, 5 | 100 | Term sheet does not state this field (Issue price not stated) |
| IDBI_Omni_Term_Sheet.pdf | BusinessDayLocation | 1, 2, 3, 4, 5 | Mumbai \| New Delhi | Term sheet does not state this field (No business‑day locations specified) |
| Genel_Energy_Term_Sheet.pdf | Notional | 1, 2, 4, 5 | 3000000 | Term sheet does not state a trade notional (the issue size is a different quantity). Consistency check passed: 3000000 = 1500 bond(s) x 2000. |
| Genel_Energy_Term_Sheet.pdf | SettlementDate | 1, 2, 3, 4, 5 | 2020-10-14 | Term sheet does not state this field (No settlement date specified) |
| Genel_Energy_Term_Sheet.pdf | Notional | 3 | 3100000 | Term sheet does not state a trade notional (the issue size is a different quantity). Consistency check passed: 3100000 = 1550 bond(s) x 2000. |

## Extracted terms – IDBI_Omni_Term_Sheet.pdf

Strategy: keyword_excerpts, 12130 characters from pages [1, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15].

| Field | Value | Basis | Page | Evidence | Status |
|---|---|---|---|---|---|
| ISIN | – | – | – |  | NOT_IN_DOCUMENT |
| Issuer | IDBI Bank Limited | stated | 1 | Issuer IDBI Bank Limited | EXTRACTED |
| Parent | – | – | – |  | NOT_IN_DOCUMENT |
| Maturity | PERPETUAL | stated | 1 | perpetual bonds | EXTRACTED |
| Notional | – | – | – |  | NOT_IN_DOCUMENT |
| Coupon | 10.75 | stated | 3 | Coupon Rate 10.75% p.a. | EXTRACTED |
| Currency | INR | stated | 3, 8 | Face Value Rs.10,00,000/- \|\| Minimum Application and 5 Bonds | EXTRACTED |
| SettlementDate | – | – | – |  | NOT_IN_DOCUMENT |
| IssueDate | 2014-10-17 | stated | 8 | Deemed Date of October 17, 2014 | EXTRACTED |
| IssueAmount | 15000000000 | stated | 1 | Issue Size and Option to Rs.1,500 Crores | EXTRACTED |
| IssuePrice | – | – | – |  | NOT_IN_DOCUMENT |
| NominalAmountPerBond | 1000000 | stated | 3 | Face Value Rs.10,00,000/- (Rupees Ten Lakh) per Bond. | EXTRACTED |
| InterestPaymentDate | 2015-10-17 | derived | 3 | Coupon Payment Dates On the anniversary of the Deemed Date of Allotment. | EXTRACTED |
| InterestPaymentFrequency | Annual | stated | 3 | coupon will be payable annually in arrear. | EXTRACTED |
| DayCountFraction | Actual/Actual | stated | 4 | Interest on application money will be computed as per Actual/Actual Day count convention. | EXTRACTED |
| BusinessDayConvention | Preceding for non‑coupon dates, next fo… | stated | 15 | Should any of the dates, other than the Coupon Payment Date including the Deemed Date of Allotment, Issuer Call Date, Tax Call Date or Regu… | EXTRACTED |
| BusinessDayLocation | – | – | – |  | NOT_IN_DOCUMENT |
| AmortizationType | Perpetual | stated | 1 | perpetual bonds | EXTRACTED |
| MinimumSubscription | 5000000 | derived | 8 | Minimum Application and 5 Bonds | EXTRACTED |

## Extracted terms – Genel_Energy_Term_Sheet.pdf

Strategy: keyword_excerpts, 12122 characters from pages [1, 2, 3, 4, 5, 6, 9, 10, 11].

| Field | Value | Basis | Page | Evidence | Status |
|---|---|---|---|---|---|
| ISIN | NO0010894330 | stated | 1 | ISIN: NO 0010894330 | EXTRACTED |
| Issuer | Genel Energy Finance 4 Limited | stated | 1 | Issuer: Genel Energy Finance 4 Limited | EXTRACTED |
| Parent | Genel Energy plc | stated | 1 | Parent: Genel Energy plc | EXTRACTED |
| Maturity | 2025-10-14 | stated | 1 | Maturity Date: Expected to be 14 October 2025 | EXTRACTED |
| Notional | – | – | – |  | NOT_IN_DOCUMENT |
| Coupon | 9.25 | stated | 1 | Coupon Rate: 9.25% p.a., semi-annual interest payments. | EXTRACTED |
| Currency | USD | stated | 1 | Currency: USD | EXTRACTED |
| SettlementDate | – | – | – |  | NOT_IN_DOCUMENT |
| IssueDate | 2020-10-14 | stated | 1 | Issue Date: Expected to be 14 October 2020 | EXTRACTED |
| IssueAmount | 300000000 | stated | 1 | Issue Amount: USD 300,000,000 | EXTRACTED |
| IssuePrice | 97 | stated | 1 | Issue Price: 97% of the Nominal Amount. | EXTRACTED |
| NominalAmountPerBond | 2000 | stated | 1 | Nominal Amount: The Bonds will have a nominal amount of USD 2,000 each. | EXTRACTED |
| InterestPaymentDate | 2021-04-14 | stated | 1 | First Interest Payment Expected to be 14 April 2021 | EXTRACTED |
| InterestPaymentFrequency | Semi-annual | stated | 1 | Coupon Rate: 9.25% p.a., semi-annual interest payments. | EXTRACTED |
| DayCountFraction | 30/360 | stated | 1 | Day-count fraction for coupon is “30/360” | EXTRACTED |
| BusinessDayConvention | Unadjusted | stated | 1 | business day convention is “unadjusted” | EXTRACTED |
| BusinessDayLocation | Oslo \| New York | stated | 1 | business day is “Oslo” and “New York” | EXTRACTED |
| AmortizationType | Bullet | stated | 1 | Amortization: The Bonds shall be repaid in full at the Maturity Date at 100% of the Nominal Amount | EXTRACTED |
| MinimumSubscription | 200000 | stated | 2 | Minimum Subscription: Minimum subscription and allotment amount shall be USD 200,000 | EXTRACTED |

## Data quality

- CSV/JSON parity IDBI_Omni.csv vs IDBI_Omni.json: **consistent**
- CSV/JSON parity Genel_Energy.csv vs Genel_Energy.json: **consistent**

## LLM usage

- Extraction calls: 2; adjudication calls: 7 (reused verdicts: 8, from cache: 0)
- Failed or unusable LLM calls: 0; retries: 0; tokens in/out: 15581/4161
