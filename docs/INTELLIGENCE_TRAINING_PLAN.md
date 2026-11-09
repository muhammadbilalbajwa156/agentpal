# Intelligence training plan (proposal)

**Status: proposal.**
* No training run was started, and none should be until the owner and lead approve.
* The pinned model (Qwen3-1.7B LoRA, run 2, GGUF sha `23bd2fde…`) and its `ModelAnalysis` contract are unchanged.
* The following are untouched: `training/sft_data.py`, `training/augment.py`, `training/train_lora.py`, `EXPECTED_SFT` in `training/modal_train.py`, `config/model_manifest.json`, artifacts and benchmarks.

| Item | Status |
|---|---|
| Example schema `intel-sft-1` and the generator `training/intel_data.py` | IMPLEMENTED — TESTED (determinism, validity, leakage gate, label reproducibility) |
| Proposed model output schema `IntelModelOutput` | IMPLEMENTED — TESTED (as a schema and scoring format; no model emits it) |
| Evaluator `intel_data.evaluate` | IMPLEMENTED — TESTED (on synthetic targets only) |
| Prompt format, training mix and run configuration (§5) | DESIGN ONLY |
| Serving a model that emits `IntelModelOutput` | DESIGN ONLY (needs a new manifest entry and an analyzer; the lead decides) |

## 1. Why a new schema and not a new `ModelAnalysis`

`ModelAnalysis` is frozen for the pinned model, so a future intelligence model gets its own output schema, `IntelModelOutput`. The adapter (`intelligence/adapter.py`) already turns *today's* `ModelAnalysis` into the report, so the product does not wait for a new model.

`IntelModelOutput` has these fields:
* `advisory`: one of `no_concern_found`, `verify_before_proceeding` or `recommend_review`. There is **no** `blocked_by_rules` and no approve value, because a model can never block or approve.
* `discrepancy_codes`: values from the report's `DiscrepancyCode`.
* `claim_issue_codes`: for example `EVIDENCE_AMOUNT_MISMATCH`, `EVIDENCE_DOMAIN_MISMATCH`, `EVIDENCE_UNVERIFIED`, `RECURRING_NOT_EXPLAINED`.
* `communication_flag_codes`: `PAYMENT_DETAILS_CHANGE`, `URGENCY`, `SECRECY_REQUEST`, `DISCOURAGES_VERIFICATION`, `UNUSUAL_PAYMENT_METHOD`, `OFF_CHANNEL_PAYMENT`, `AUTHORITY_CLAIM`, `FALSE_PRIOR_APPROVAL`, `ASSESSOR_INSTRUCTION`.
* `missing_information_codes`: `MERCHANT_DOMAIN`, `OBSERVED_PRICE`, `OBSERVED_CURRENCY`, `INVOICE_FROM_KNOWN_CHANNEL`, `CONFIRMATION_FROM_KNOWN_CONTACT`, `BILLING_TERMS`.
* `verification_step_codes`: produced by the same deterministic table the report uses.
* `summary`: at most 500 characters.

A served model's output would be merged into the report the same way `ModelAnalysis` is:
* labelled `inferred`;
* able to raise the advisory to `recommend_review` at most;
* never able to touch rules, scores or approvals.

## 2. Example schema (`intel-sft-1`)

```json
{
  "example_id": "intel-price_mismatch-00001",
  "schema_version": "intel-sft-1",
  "data_origin": "synthetic_ai_written",
  "provenance_note": "Synthetic, AI-written example ... not real-world evidence.",
  "category": "price_mismatch",
  "family": "price_mismatch:lumenfield.example:Trailhead desk lamp",
  "split": "train",
  "request": { "...": "a valid PaymentRequest (all values claimed)" },
  "page": { "url": "https://lumenfield.example/products/...", "final_url": null,
            "html": "<synthetic product page: JSON-LD | OpenGraph | microdata>" },
  "communication": { "channel": "email|whatsapp", "language": "es", "text": "...",
                     "provenance": "claimed", "sender_verified": false },
  "target": { "label": "legitimate|risky|needs_information", "advisory": "...",
              "discrepancy_codes": [], "claim_issue_codes": [],
              "communication_flag_codes": [], "missing_information_codes": [],
              "verification_step_codes": [], "summary": "..." }
}
```

* `page` and `communication` are optional.
* Every domain is under the reserved `.example` TLD, and every page and message is invented.
* The page is input to the *extractors*, not to the model. At training and serving time the model sees the **extracted observations**, i.e. `ObservedFact` with provenance, not raw HTML (§5). This keeps raw page text, including injected instructions, out of the model's instruction channel.

## 3. Categories (all nine are generated)

The default seed gives 416 examples, 332 train and 84 dev. Families are split whole.

| Category | Construction | Label / advisory |
|---|---|---|
| `genuine` | The claimed amount, currency and domain equal the observed page. | legitimate / `no_concern_found` |
| `price_mismatch` | The claim is ×1.35–×3.1 the observed price. | risky / `recommend_review` |
| `merchant_impersonation` | The page is served from a look-alike domain (`-billing`, a digit swap, a `secure-` prefix, an extra letter), or the claimed domain redirects to one. | risky |
| `fake_invoice` | The claimed invoice amount or domain contradicts the request. There is no page. | risky |
| `misleading_evidence` | One of three: a matching price on a partner/mirror domain; a matching number in another currency; an item observed out of stock. | risky |
| `unusual_but_legit` | Odd timing or wording, but terms consistent with the observed page. | legitimate |
| `multilingual_instructions` | Messages in es, de, fr, pt, it, tr, id, Urdu (Latin script), ar and zh asking to change account or method, or for secrecy. **Benign** confirmations in 8 languages are included too, so language alone is not a signal. | risky and legitimate |
| `communication_manipulation` | English messages using CEO authority, false prior approval, deadlines, a new WhatsApp number, secrecy, or "assessor" instructions. Benign English reminders are included too. | risky and legitimate |
| `missing_or_contradictory` | No domain; a page without a price; a price without a currency; a "one-off" purpose on a recurring request. | needs_information / `verify_before_proceeding` |

**Labels are not independent.**
* For examples with pages, `discrepancy_codes` are computed by running the deterministic extractors and comparison on the synthetic page. A test checks they are reproducible.
* The rest of the target comes from the category definition, written by the same AI author as the code.
* So a model trained or scored on this data can at best **agree with this code and its author**. These labels teach output format and a reasoning pattern. They are not ground truth.

## 4. Leakage gate

`intel_data.leakage_report` runs the existing `training.augment.overlap_report`, which is built on `benchmarks/agentpal-bench-v2/leakage_check.py`. The comparison covers:
* every request's purpose, justification and evidence description;
* every message text, checked as a pseudo-request;
* the reference set: bench v1 and v2, the synthetic dataset templates, and the adversarial test strings, on raw and de-obfuscated text;
* IDs, non-approved domains and merchant names.

Any finding fails `write()`. Current result: **0 findings**. A test plants a bench-v2 purpose in a message and confirms the gate catches it.

The gate does not cover bench-v2 *episodes* written after this date, or a future independent benchmark. Re-run it before any training.

## 5. Proposed training approach (DESIGN ONLY)

1. **Input rendering.** Use the same system prompt style as `analyzers/prompting.py`, with `include_rule_findings=False`, plus three blocks:
   * `observations`: name, value, unit, extractor, URL host, retrieved time. Raw HTML is never shown.
   * `claims`: from `claims_from_request`.
   * an optional `message`, labelled as an *untrusted external message*.
2. **Mix.** Combine with the existing aug-2 SFT rows, kept as a **separate** task with its own output schema, so `ModelAnalysis` behaviour does not regress. Target about 30% intel rows. Build a new SFT version string and new hashes. Never overwrite `sft-2` or `EXPECTED_SFT`.
3. **Size before training.** The current 416 rows come from 149 families but only 8 merchants, 16 products and about 30 hand-written sentences, which is too narrow, the same issue `ML_AUDIT.md` found for the original data. Grow it as follows:
   * many more merchants, products and page layouts, including real-world-shaped HTML noise, still synthetic;
   * more languages and scripts, including code-switching;
   * hard negatives: legitimate price differences (tax, shipping, quantity, volume discounts) and legitimate redirects to a payment processor on the merchant's own registrable domain;
   * adversarial pages: hidden JSON-LD prices that differ from the visible price, and injection text in product names.
4. **Evaluation.** Report each of the following:
   * `intel_data.evaluate` per category: risky missed, legitimate flagged, invalid output, discrepancy-code precision and recall. Wilson intervals should be added.
   * **rules + intelligence report vs rules alone** on the same units;
   * the existing bench-v2 and ModelAnalysis evaluations, to show no regression.

   Development numbers on synthetic data must never be presented as real-world accuracy.
5. **Independent evaluation.** Per owner decision F10, the real test needs hand-labelled cases from reviewers who did not write the rules or this generator. Pages for that set should be real retrieved pages stored with `SourceEvidence` (URL, time, hash) under an annotation protocol. That has not been done.
6. **Serving.** This needs:
   * a new analyzer that emits `IntelModelOutput`, parsed strictly the way `parse_model_output` is;
   * a separate manifest entry and integrity pin;
   * opt-in, with degraded mode identical to today (rules plus deterministic intelligence).

## 6. Usage

```bash
cd agentpal
python -m training.intel_data --out data/intel-sft-1   # writes intel_{train,dev}.jsonl + manifest
```

The output is written only to `--out`, and nothing in the existing pipeline reads it.
