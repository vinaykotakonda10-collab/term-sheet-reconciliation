# Technical Report: LLM-Powered Term Sheet Reconciliation

## 1. Workflow and architecture

A Python CLI (`main.py`) runs one job per term sheet in `data/manifest.json`: **parse → select excerpts → LLM extraction → validation and evidence checks → booking ingestion → deterministic reconciliation → LLM mismatch review → reports**. Each step is a separate module (`src/`). The pipeline depends only on an `LLMClient` protocol, comparison rules sit in a registry, and fields are declared once in `field_catalog.py`, from which the schema, prompt, loader and engine derive. A failed job is recorded and the others continue. Only fatal LLM configuration errors (invalid key, unavailable model) stop the run.

## 2. LLM / API integration

* **Provider:** Groq Cloud through the official `groq` SDK; default model `openai/gpt-oss-20b` (configurable; `test_groq_connection.py` checks the key, lists models and makes one live call). JSON mode, temperature 0, `reasoning_effort=low`.
* **Two passes:** (1) structured extraction of 19 fields with verbatim evidence; (2) one call per *distinct* suspected mismatch, returning `TRUE_MISMATCH | FALSE_ALERT | INSUFFICIENT_EVIDENCE` with a category, explanation, supporting evidence and confidence. Both responses are validated with Pydantic.
* **Prompt design:** versioned templates with business definitions per field (issue size is not a trade notional; a call date is not a maturity), explicit formats, and a `stated` / `derived` basis that must be explained when derived.
* **Free-tier economics:** the free tier allows 8K tokens per minute against documents of 30–42K characters, so keyword-scored excerpts (12K characters, traceable by page) are sent instead of whole documents. The client reads `x-ratelimit-*` headers and waits for the token window rather than triggering 429s. Responses are cached by SHA-256 of model, prompt version and full prompt, and identical breaks share one verdict. A full run takes 9 requests and about 19.7K tokens.

## 3. Field extraction and evidence strategy

PyMuPDF yields two text views per page: a *layout* view (table label and value on one line, which the LLM reads) and a *reading-order* view (multi-line cells intact). python-docx handles Word files (paragraphs and tables in body order; explicit page breaks define pages). Every LLM claim is checked by code:

1. **Schema:** strict types per field: a number must be a JSON number (`"10.75%"` fails), ISO dates, ISIN format plus check digit, ISO 4217 currencies, enumerations, `"PERPETUAL"` maturity. Fields are validated independently, so one bad field becomes a *Parsing Error* without affecting the others.
2. **Evidence:** each quote must exist in either view after a fixed normalisation (Unicode/quote/dash folding and collapsed whitespace, with a whitespace-insensitive fallback for hyphenated line breaks). The reported page is where the quote was found. Paraphrases and "..." are rejected.
3. **Grounding:** a `stated` value must appear in its own quote (crore/lakh/million scaling and Indian digit grouping are understood).
4. **Semantics:** a notional that reuses the issue size, denomination or minimum-subscription clause is rejected. In a live test the model used the USD 2,000 denomination as the notional; this check now catches that.

Malformed JSON gets one corrective retry, and failed fields get one targeted repair call whose output must pass all checks. Absent terms are `null` and reported as **Unverifiable**: the IDBI term sheet has no ISIN, issue price, parent or business-day centres, and neither term sheet gives a settlement date or a trade notional.

## 4. Reconciliation methodology

Comparison is per trade and per field (10 trades × 19 fields = 190 rows). Deterministic rules come first: date parsing with file-level M/D vs D/M inference (the Genel CSV uses `10/14/2025`), exact `Decimal` arithmetic, perpetual-aware maturity, synonym vocabularies (`Act/Act` = `Actual/Actual`), set comparison for business centres, and legal-suffix-insensitive names. Representation differences such as `0.1075` vs `10.75%`, or a crore-scaled amount, are flagged as *candidate equivalences* and go to the LLM review, which classifies them as False Alerts. The **trade notional is never compared with the issue size**: it is only checked to be a whole number of bonds (IDBI trade 3: 1,100,000 / 10,00,000 = 1.1 bonds, a break). **Guardrails** on the second pass: a False Alert for values that still differ in the same unit, a low-confidence False Alert, a missing-evidence verdict, `INSUFFICIENT_EVIDENCE` and a failed call all become **Unresolved**, never an invented decision.

**Result on the supplied data:** 136 Match, 15 True Mismatch, 39 Unverifiable, 0 Parsing Error, 0 Unresolved, 0 failed calls. The breaks are: IDBI perpetual vs a booked 2025-10-17 maturity and the generic business-day convention (all trades; the latter an LLM judgement), coupon 11.00 vs 10.75 (trade 2), 1.1 bonds (trade 3), issue date 10 vs 17 Oct 2014 (trade 4); Genel maturity +1 day (trade 2) and coupon 9.50 vs 9.25 (trade 4). Genel trade 3's 3,100,000 notional is a valid 1,550 bonds, so it is reported as Unverifiable, not as a break.

## 5. Challenges and assumptions

* **Noisy layout:** IDBI's three-column table breaks words across lines; the two text views and whitespace-tolerant (otherwise exact) quote matching handle it.
* **Semantics:** "Deemed Date of Allotment" is treated as the issue date. The booking `InterestPaymentDate` is taken to be the *first* coupon date (derived for IDBI from the "anniversary of the Deemed Date of Allotment"). The base issue size excludes the green-shoe.
* **Model variance:** output is not fully deterministic at temperature 0 (in testing, 20b quoted more faithfully and 120b chose clauses better). Validation, repair and the guardrails stop a deviation from becoming a wrong Match or Mismatch.

## 6. Error handling and security

Typed errors for 401, 403, 404 (model), 413, 429 (rate limit vs daily quota), 5xx, timeouts, network failures and Groq `json_validate_failed`, with exponential backoff, `retry-after` honoured and a `GROQ_MAX_RETRIES` cap. Input problems (ambiguous dates, duplicate TradeIDs, missing columns, scanned or corrupt PDFs) produce actionable messages, and every run writes a JSON-lines log. **Security:** the key lives only in the git-ignored `.env` or the environment; logs redact keys; document text is fenced as untrusted data, so injected instructions cannot change what validation accepts. No secret is ever sent in a prompt.

## 7. Productionisation

* **CI/CD:** the provided GitHub Actions workflow runs the 151 offline tests on every push. Next: lint and type-check gates, and a nightly live run on a golden set with accuracy thresholds that every prompt or model change must pass before release.
* **Deployment:** the provided Dockerfile (non-root user) runs as a scheduled container job (Airflow or Kubernetes CronJob) triggered by new term sheets in object storage or by booking exports, with secrets from a vault.
* **Monitoring:** ship the JSON logs to ELK or Datadog; track the Parsing-Error, evidence-failure and Unresolved rates, latency, and tokens and cost per document; alert on spikes and quota errors.
* **Scale and resilience:** a queue with worker concurrency sized to provider rate limits; a shared Redis cache; a paid tier or fallback model behind `LLMClient`; OCR for scanned term sheets.
* **Controls and data security:** a maker-checker UI where reviewers confirm or override Unresolved and False-Alert rows, with feedback kept for prompt tuning; immutable audit trails; a confidentiality review (or a self-hosted model) before documents go to an external LLM; encryption in transit and at rest.
