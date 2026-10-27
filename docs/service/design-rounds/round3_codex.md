Basis: **VERIFIED** repository structure, evidence-completeness behavior, and bundled SQLite 3.45.3. **UNSURE** independently: live calldata, prevalence, and OR-filter measurements; network access was unavailable.

- **R-1 — SIGN** [INFERRED]
- **R-2 — SIGN** [INFERRED]
- **R-3 — OBJECT** [INFERRED]  
  Concrete failure: settlement may succeed immediately before a process crash, leaving no committed result/status; a retry could double-settle or withhold a paid result.  
  Smallest fix: require a durable idempotency state machine keyed by `(payment_id, request_hash, collector_version)`, cache the response before settlement, reconcile any uncertain settlement with the facilitator, never blindly resettle, and test every crash boundary.
- **R-4 — OBJECT** [INFERRED]  
  Concrete failure: a reorg can leave orphaned findings or notifications treated as canonical; deduplication may then suppress the replacement canonical alert.  
  Smallest fix: specify rollback to the common ancestor, transactional invalidation of derived evidence and unsent outbox rows, canonical block hash in dedup keys, and finalized-only notifications. If provisional alerts are sent, require an explicit reorg correction.
- **R-5 — OBJECT** [INFERRED]  
  Concrete failure: “manifest hash” without canonicalization and provenance cannot establish which chain, rules, inputs, or collector produced the report.  
  Smallest fix: define the hash as canonical JSON excluding its own hash field, and include chain ID, request parameters, collector/rule versions, source/RPC identity, covered block ranges, and boundary block hashes.
- **R-6 — SIGN** [INFERRED]
- **R-7 — SIGN** [INFERRED]

**Disagreement record — OBJECT** [VERIFIED against the supplied text]  
Entry 2 says replay/reconciliation requirements were resolved, but R-3 omits reconciliation and crash-state semantics. Smallest fix: amend R-3 as above; then the record can be signed. All other entries are **SIGN** [INFERRED].

No file was written because the workspace is explicitly read-only and no output-file path was supplied.