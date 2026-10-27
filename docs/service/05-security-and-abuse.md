# 05 — Security and abuse analysis

## Threat model

| Threat | Harm | Control (technical where possible) |
|---|---|---|
| False attribution rendered as fact, then forwarded or published | Harassment of an unrelated wallet owner; legal and reputational exposure for Bo (Codex R2: the single largest harm) | Private by default; no public case pages; no external submission code path; ownership = unknown unless a rule establishes it; shared funding or shared service never mapped to common ownership; user claims stored as assertions; causal drain predicates unknown without actor-tying evidence |
| Victim data exposure | Email, claimed loss, victim-address linkage leak | IMPLEMENTED: capability URL (192-bit token, sha256 at rest, constant-time compare), `Referrer-Policy: no-referrer` and `no-store`, no public case pages, no narrative collected. **Not yet**: the email is stored as given (it is the alert destination) and the claimed loss is raw text; the token travels in `?k=` (no cookie exchange) so it lands in browser history and any proxy log; there is no `chain_facts` table and no cross-case inference test. Treat the SQLite file as containing PII |
| Screener abuse: harassment, SEO of accusations, scraping | Someone uses free screens to build "X is a scammer" pages | Free screen output is three-valued and names no owner; public pages carry methodology and corrections only; IMPLEMENTED brakes: 10 screens per IP per hour (per process, `X-Forwarded-For` honoured only with `SLEUTH_TRUST_PROXY=1`), 3 open screens per subject per hour, 60 cases per hour and 50 queued runs service-wide. NOT BUILT: API keys, robots directives |
| Payment fraud / double delivery | Free cases, chargeback-like disputes, duplicate settlements | Entitlement only after verified settlement; idempotency keys; unique settled tx; `settlement_pending` reconciliation; worker refuses unpaid leases |
| Merchant signing material exposure | Theft of receipts | The service holds **no** signing keys: x402 settlement is facilitator-side, checkout is provider-hosted, the receiving wallet is a bare address; scraped content, user narratives and explorer responses never reach any payment component (separate package, no imports) |
| Prompt / content injection | Malicious token names, explorer labels or narratives steer output | No LLM in the MVP; templates escape all chain strings; labels from public sources carry provenance |
| Resource exhaustion | Free screens exhaust RPC budget; monitoring floods | Per-run budgets (wall time, RPC subcalls, receipts), window caps (≤1M blocks free), queue leases, outbox dedup, one health notice per outage |
| Insider / operator error | Wrong case shown, evidence edited | IMPLEMENTED: `audit_events` rows for case, payment, assertion, reorg and settlement actions (free-form detail JSON, not before/after hashes); manifest sha256 on every export; admin form needs Basic auth plus a per-process CSRF token, 5 failed logins per 10 minutes lock the IP. NOT: evidence is not append-only — provisional rows are deleted on reorg and promoted by update |
| Monitoring misrepresentation | "Monitored" while the worker was down | Workspace always shows last provisional/finalized scan, lag and provider health; outage credits |

## Hard boundaries (not disclaimers)

- No signing, fund transfer, custody or wallet-management code anywhere in the repo.
- Never ask for seed phrases, private keys, wallet connections, or a transaction from the compromised wallet.
- Payment code (`service/payments/`) cannot import collectors or case evidence; collectors cannot import payments.
- No LE-only portals; no impersonation of law enforcement; no freeze/seizure/recovery promises.
- No mass reporting; every outbound report (later) needs an authorization record.

## Data retention

PROPOSED (NOT BUILT, decision B-8): cases default to a retention window (12 months after close) after which private
tables are purged. Today nothing is purged. Exports are the customer's copy. Audit events outlive the case.

## Access

Workspace access is a capability URL; no passwords. The emailed re-issue flow is NOT BUILT (a lost link means a new
free screen). Operator access is `/admin/payments` on the same process, which binds to 127.0.0.1 by default
(`SLEUTH_HOST`) and must only be exposed behind TLS and a reverse proxy. The only secrets are `SLEUTH_ADMIN_PASSWORD`
and the receive address (not a secret); none are committed.
