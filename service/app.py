"""FastAPI web app: public landing + free screens, private case workspace (capability token in the URL), exports,
methodology/corrections pages, operator admin, and the flag-gated x402 machine endpoint. The app never calls RPC in a
request handler: it writes a case + run and the worker does the work. No public case pages, no accusation rendering,
no external submissions (design round 3, R-6)."""
import collections
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from sleuth import config as C
from sleuth import report as R
from sleuth.collectors import COLLECTOR_VERSION

from . import SERVICE_VERSION, cases, db, reports
from .jobs import JOB_VERSION
from .payments import PaymentError, manual, x402

log = logging.getLogger("sleuth.service.app")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
def _local(ts, tz="America/Los_Angeles"):
    """Explorer timestamps are trusted data, not trusted shapes: a malformed one renders as-is instead of a 500."""
    try:
        return R.local(ts, tz) if ts else "—"
    except (ValueError, TypeError, OverflowError):
        return str(ts)


templates.env.filters["local"] = _local
templates.env.filters["short"] = R.short
app = FastAPI(title="Sleuth incident service", version=SERVICE_VERSION, docs_url=None, redoc_url=None)
basic = HTTPBasic()
BASIC = Depends(basic)
_conn = None                      # test override only; production uses one connection per request thread
_local = threading.local()
_facilitator = x402.MockFacilitator()
_CSRF = secrets.token_urlsafe(16)  # per-process; the admin form must echo it (Basic auth alone is replayable cross-site)


def conn():
    if _conn is not None:
        return _conn
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = db.connect()
    return c


class Brake:
    """Fixed-window per-key counter (in memory, per process): good enough to stop one IP flooding free screens or
    guessing the admin password. Not shared across processes; the service-wide brake lives in cases.create."""
    def __init__(self, limit, window_s):
        self.limit, self.window, self.hits, self.lock = limit, window_s, collections.defaultdict(collections.deque), threading.Lock()

    def hit(self, key):
        """Record one hit; returns False when the key is over its limit."""
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self.hits) > 10_000:  # bounded memory under address spraying
                self.hits.clear()
            return True


SCREEN_BRAKE = Brake(limit=10, window_s=3600)
ADMIN_BRAKE = Brake(limit=5, window_s=600)


def client_ip(request):
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() if os.environ.get("SLEUTH_TRUST_PROXY") == "1" else "") \
        or (request.client.host if request.client else "?")


def chain_id():
    try:
        return int(os.environ.get("SLEUTH_CHAIN_ID") or C.load().get("default_chain") or 4663)
    except (SystemExit, ValueError, AttributeError):
        return 4663


def render(name, request, **ctx):
    ctx.update(request=request, service_version=SERVICE_VERSION, chain_id=chain_id(), x402_enabled=x402.enabled())
    return templates.TemplateResponse(request, name, ctx)


def admin(request: Request, creds: HTTPBasicCredentials = BASIC):
    pw = os.environ.get("SLEUTH_ADMIN_PASSWORD", "")
    ip = client_ip(request)
    if not pw or not (secrets.compare_digest(creds.username, "admin") and secrets.compare_digest(creds.password, pw)):
        if not ADMIN_BRAKE.hit(ip):
            raise HTTPException(429, "too many failed admin logins; wait 10 minutes")
        raise HTTPException(401, "admin disabled or bad credentials", headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.middleware("http")
async def headers(request, call_next):
    resp = await call_next(request)
    resp.headers["Referrer-Policy"] = "no-referrer"  # the case token lives in the URL; never leak it to third parties
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz")
def healthz():
    try:
        conn().execute("SELECT 1").fetchone()
        return {"ok": True, "service": SERVICE_VERSION, "collectors": COLLECTOR_VERSION, "jobs": JOB_VERSION}
    except db.DbError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render("index.html", request)


@app.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request):
    return render("methodology.html", request)


@app.get("/corrections", response_class=HTMLResponse)
def corrections(request: Request):
    return render("corrections.html", request)


@app.get("/screen", response_class=HTMLResponse)
def screen_form(request: Request):
    return render("screen.html", request, kind="rug", error=None)


@app.get("/drain", response_class=HTMLResponse)
def drain_form(request: Request):
    return render("screen.html", request, kind="drain", error=None)


@app.post("/screen")
def screen_create(request: Request, kind: str = Form(...), subject: str = Form(...), email: str = Form(""), incident_ts: str = Form(""), claimed_loss: str = Form("")):
    if not SCREEN_BRAKE.hit(client_ip(request)):
        return render("screen.html", request, kind=kind if kind in ("rug", "drain") else "rug", error="too many screens from this address in the last hour")
    try:
        ts = float(incident_ts) if incident_ts.strip() else None
        cid, tok = cases.create(conn(), kind, chain_id(), subject, email=email.strip()[:200] or None, incident_ts=ts, claimed_loss=claimed_loss.strip()[:100] or None)
    except (ValueError, db.DbError) as e:
        return render("screen.html", request, kind=kind if kind in ("rug", "drain") else "rug", error=str(e))
    return RedirectResponse(f"/c/{cid}?k={tok}", status_code=303)


def _case_or_404(case_id, k):
    row = cases.load(conn(), case_id, k)
    if not row:
        raise HTTPException(404, "case not found (check the full private link)")
    return row


def _workspace(case_id, evidence_limit=200):
    """Rows for the page (newest 200 evidence rows) or, with evidence_limit=None, everything for the export."""
    c = conn()
    runs = c.execute("SELECT * FROM investigation_runs WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
    asr = c.execute("SELECT * FROM assertions WHERE case_id=? AND superseded_at IS NULL ORDER BY created_at", (case_id,)).fetchall()
    lim = "" if evidence_limit is None else f" LIMIT {int(evidence_limit)}"
    ev = c.execute(f"SELECT * FROM evidence_items WHERE case_id=? ORDER BY block DESC{lim}", (case_id,)).fetchall()
    subs = c.execute("SELECT * FROM monitoring_subscriptions WHERE case_id=? ORDER BY starts_at DESC", (case_id,)).fetchall()
    pays = c.execute("SELECT * FROM payments WHERE case_id=? ORDER BY created_at DESC", (case_id,)).fetchall()
    return runs, asr, ev, subs, pays


@app.get("/c/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: str, k: str = ""):
    case = _case_or_404(case_id, k)
    runs, asr, ev, subs, pays = _workspace(case_id)
    results = [(r, db.loads(r["result_json"])) for r in runs]
    entitled = any(p["state"] in ("settled", "entitled") for p in pays)
    return render("case.html", request, case=case, k=k, runs=results, assertions=asr, evidence=ev, subs=subs, payments=pays, entitled=entitled,
                  checkout_enabled=bool(manual.receive_address()))


@app.post("/c/{case_id}/checkout")
def checkout(case_id: str, k: str = Form(...)):
    case = _case_or_404(case_id, k)
    if case["kind"] != "rug":
        raise HTTPException(400, "the founding case is for rug cases only; the drain screen is free during the beta")
    try:
        manual.start(conn(), case["id"])
    except PaymentError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/c/{case_id}?k={k}", status_code=303)


@app.get("/c/{case_id}/export.{fmt}")
def export(case_id: str, fmt: str, k: str = ""):
    case = _case_or_404(case_id, k)
    runs, asr, ev, subs, _ = _workspace(case_id, evidence_limit=None)
    try:
        rpc = C.chain(C.load(), case["chain_id"])[1].get("rpc", "unknown")
    except SystemExit:
        rpc = "unknown"
    pkg = reports.build(case, runs, ev, asr, subs, rpc_identity=rpc)
    if fmt == "json":
        return Response(reports.to_json(pkg), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="case-{case_id[:8]}.json"'})
    if fmt == "html":
        return HTMLResponse(reports.to_html(pkg))
    raise HTTPException(404, "export format must be json or html")


ADMIN = Depends(admin)


@app.get("/admin/payments", response_class=HTMLResponse)
def admin_payments(request: Request, _: str = ADMIN, error: str = ""):
    rows = conn().execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT 200").fetchall()
    return render("admin_payments.html", request, payments=rows, error=error, csrf=_CSRF)


@app.post("/admin/payments/{payment_id}/confirm")
def admin_confirm(payment_id: str, tx_hash: str = Form(...), csrf: str = Form(""), who: str = ADMIN):
    if not secrets.compare_digest(csrf, _CSRF):
        raise HTTPException(403, "stale or missing form token; reload /admin/payments")
    try:
        row = manual.confirm(conn(), payment_id, tx_hash.strip(), operator=f"admin:{who}")
        if row["case_id"]:
            cases.entitle(conn(), row["case_id"], actor=f"admin:{who}")
    except (PaymentError, ValueError, db.DbError) as e:
        return RedirectResponse(f"/admin/payments?error={str(e)[:120]}", status_code=303)
    return RedirectResponse("/admin/payments", status_code=303)


@app.post("/v1/screen/token")
def machine_screen(request: Request, body: dict):
    """x402-gated stateless screen for agents. Returns 402 + requirements until a valid PAYMENT-SIGNATURE is presented.
    Behind SLEUTH_X402=1 and SLEUTH_PAY_TO_BASE_USDC; the facilitator is the mock until decision B-4 provides a real one."""
    if not x402.enabled():
        raise HTTPException(404, "x402 endpoint disabled")
    pay_to = manual.receive_address()
    if not pay_to:
        raise HTTPException(503, "receive address not configured")
    token = str(body.get("token", "")).strip().lower()
    if not cases.valid_addr(token):
        raise HTTPException(400, "token must be a 0x address")
    params = {"token": token, "chain_id": chain_id()}
    req = x402.requirements("/v1/screen/token", pay_to)
    payment = x402.parse_header(request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT") or "")
    if not payment:
        return JSONResponse({"x402Version": x402.PROTOCOL_VERSION, "accepts": [req], "error": "payment required"}, status_code=402)
    runner = getattr(app.state, "screen_runner", None)
    if runner is None:
        from .jobs import run_job  # imported lazily so tests can inject a fake runner
        def runner(p):
            return run_job(C.load(), "rug_screen", p)[1]
    status, out = x402.gate(conn(), _facilitator, payment, req, params, JOB_VERSION, lambda: runner(params))
    return JSONResponse(out, status_code=status)
