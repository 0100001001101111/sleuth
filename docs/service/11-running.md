# 11 — Running the service locally (IMPLEMENTED, pilot only)

Nothing here deploys to production, spends money, or sends anything externally.

```bash
cd ~/sleuth
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn jinja2 python-multipart curl_cffi pytest ruff httpx2
.venv/bin/python -m pytest -q tests            # 80 offline tests (53 CLI + 27 service/collectors)
.venv/bin/ruff check service sleuth/collectors tests/test_service.py tests/test_collectors.py   # only the new files
export SLEUTH_ADMIN_PASSWORD='choose-one'        # enables /admin/payments (basic auth, user "admin"); unset = 401
export SLEUTH_PAY_TO_BASE_USDC=0x...             # receive-only USDC address on Base; unset = checkout disabled (keep unset until B-2)
# export SLEUTH_X402=1                           # enables POST /v1/screen/token with the MOCK facilitator only (keep off: 09 entry 2)
# export SLEUTH_SERVICE_DB=/path/service.db      # default: $SLEUTH_HOME/data/service.db
# export SLEUTH_HOST=127.0.0.1 SLEUTH_PORT=8400  # bind address and port
# export SLEUTH_CHAIN_ID=4663                    # default: config default_chain, else 4663
# export SLEUTH_TRUST_PROXY=1                    # only behind a reverse proxy: honour X-Forwarded-For for the per-IP brakes
.venv/bin/python -m service                      # web + worker, http://127.0.0.1:8400
```

`httpx2` is the package starlette's `TestClient` imports in this venv (fastapi 0.162 / starlette); it is a test-only
dependency.

- `python -m service web` / `python -m service worker` run the halves separately (same SQLite file, same host).
- The worker shares `SLEUTH_HOME` with the CLI: triage and dev-history still update `data/watchlist.json` (known
  limitation, 09 entry 2; harmless for the pilot but it is why the x402 flag stays off).
- Schema: `service/db.py` runs `CREATE TABLE IF NOT EXISTS` only; there is no migration tool. `db.SCHEMA_VERSION` is
  stamped into `PRAGMA user_version`, and `db.connect()` refuses a non-empty file with a different version instead of
  silently reusing stale tables. After any schema edit: bump `SCHEMA_VERSION`, stop the service and delete the scratch
  `service.db` (plus `-wal`/`-shm`) before restarting.
- Limits (per process): 10 free screens per IP per hour, 3 open screens per subject per hour, 60 cases per hour and
  50 queued runs service-wide, 5 failed admin logins per IP per 10 minutes. The error text on the form says which one hit.
- Monitoring alerts are appended to `$SLEUTH_HOME/data/notifications.jsonl`. No email or SMS provider is wired.
- Manual settlement: customer sends USDC on Base to the receive address shown on the case page; the operator checks
  the Base explorer **by hand** (the code does not verify the transfer) and enters the tx hash in `/admin/payments`.
  The confirm form carries a per-process CSRF token: a stale page after a restart gets 403, reload and resubmit. That
  entitles the case (deep pass + 30-day monitoring). The same tx hash cannot be credited twice; drain cases cannot be
  checked out (400).
- Backups: `service.db.backup(conn, dest)` uses SQLite's online backup API; never copy the file while running.
- Expose only behind TLS and a reverse proxy; the case capability token travels in the URL.
