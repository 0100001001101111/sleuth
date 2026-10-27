"""Evidence package export. The JSON is canonical (sorted keys) and carries a manifest whose sha256 is computed over
the package with the hash field blank, so anyone can recompute it. Includes chain id, request params, collector/rule
versions, RPC identity, covered ranges and boundary hashes (design round 3, R-5). No prose, no accusations: the HTML
renders the same facts with the same three-valued verdict words."""
import hashlib
import html

from sleuth import report as R

from . import db
from .jobs import JOB_VERSION

PACKAGE_VERSION = "evidence-package/0.1.0"
VERDICT_WORDS = {True: "YES", False: "no", None: "unknown (incomplete)"}


def verdict_word(v):
    return VERDICT_WORDS.get(v, "unknown (incomplete)")


def _manifest_hash(pkg):
    p = dict(pkg)
    p["manifest"] = dict(pkg["manifest"], sha256="")
    return hashlib.sha256(db.dumps(p).encode()).hexdigest()


def build(case, runs, evidence, assertions, subscriptions, rpc_identity, tz="America/Los_Angeles"):
    """Assemble the package from DB rows (sqlite3.Row or dicts). Deterministic for the same rows."""
    case = dict(case)
    runs = sorted((dict(r) for r in runs), key=lambda r: (r["created_at"], r["id"]))
    for r in runs:
        r["result"] = db.loads(r.pop("result_json", None))
    ev = sorted((dict(e) for e in evidence), key=lambda e: (e["block"] or 0, e["key"]))
    for e in ev:
        e["payload"] = db.loads(e.pop("payload_json", None))
    asr = sorted((dict(a) for a in assertions), key=lambda a: (a["created_at"], a["id"]))
    subs = [dict(s) for s in subscriptions]
    ranges = [{"collector": r["kind"], "checked_through": (r["result"] or {}).get("checked_through"),
               "head": (r["result"] or {}).get("head"), "complete": bool((r["result"] or {}).get("complete"))} for r in runs]
    pkg = {
        "package_version": PACKAGE_VERSION, "job_version": JOB_VERSION,
        "case": {k: case[k] for k in ("id", "kind", "chain_id", "subject", "created_at", "incident_ts", "status", "product") if k in case},
        "runs": runs, "evidence": ev, "assertions": asr,
        "monitoring": [{k: s.get(k) for k in ("id", "state", "starts_at", "ends_at", "provisional_cursor", "provisional_hash",
                                                "finalized_cursor", "finalized_hash", "last_success_at", "lag_blocks")} for s in subs],
        "manifest": {"chain_id": case["chain_id"], "rpc": rpc_identity, "collector_versions": sorted({r["collector_version"] for r in runs}),
                     "rule_versions": sorted({a["rule_version"] for a in asr}), "covered_ranges": ranges,
                     "monitoring_cursors": [{k: s.get(k) for k in ("id", "finalized_cursor", "finalized_hash", "provisional_cursor", "provisional_hash")} for s in subs],
                     "generated_at_utc": None, "sha256": ""},
        "boundaries": {"verdict_words": VERDICT_WORDS[None], "note": "Facts observed on chain. Ownership is unknown unless stated. "
                       "Nothing here is an accusation; external reports need human review and your authorization."},
    }
    pkg["manifest"]["generated_at_utc"] = R.local(db.now(), "UTC")
    pkg["manifest"]["sha256"] = _manifest_hash(pkg)
    pkg["display_tz"] = tz
    return pkg


def verify(pkg):
    """Recompute the manifest hash; True when the package is intact."""
    try:
        p = {k: v for k, v in pkg.items() if k != "display_tz"}
        return _manifest_hash(p) == pkg["manifest"]["sha256"]
    except (KeyError, TypeError):
        return False


def to_json(pkg):
    return db.dumps(pkg)


def _esc(s):
    return html.escape("" if s is None else str(s))


def to_html(pkg):
    """Plain, print-friendly HTML of the same package. Verdicts only in the three words; no narrative."""
    tz = pkg.get("display_tz", "UTC")
    rows = []
    for a in pkg["assertions"]:
        rows.append(f"<tr><td>{_esc(a['subject'])}</td><td>{_esc(a['predicate'])}</td><td>{_esc(a.get('object'))}</td>"
                    f"<td class='v'>{_esc(a['verdict'])}</td><td>{_esc(a.get('reason'))}</td><td>{_esc(a['rule_version'])}</td></tr>")
    evr = []
    for e in pkg["evidence"]:
        evr.append(f"<tr><td>{_esc(e['kind'])}</td><td class='m'>{_esc(e['key'])}</td><td>{_esc(e.get('block'))}</td>"
                   f"<td>{_esc(e['finality'])}</td><td>{_esc(R.local(e['observed_at'], tz))}</td><td class='m'>{_esc(e['content_hash'][:16])}</td></tr>")
    cov = "".join(f"<li>{_esc(r['collector'])}: checked through block {_esc(r['checked_through'])} — "
                  f"{'complete' if r['complete'] else 'INCOMPLETE (gaps listed in JSON)'}</li>" for r in pkg["manifest"]["covered_ranges"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Evidence package {_esc(pkg['case']['id'][:8])}</title>
<style>body{{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 16px;color:#111}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}td,th{{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}}
.m{{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}}.v{{font-weight:600}}.box{{background:#f6f6f6;padding:1rem;border-left:4px solid #999}}</style></head>
<body><h1>Evidence package</h1>
<p class="m">case {_esc(pkg['case']['id'])} · chain {_esc(pkg['case']['chain_id'])} · subject {_esc(pkg['case']['subject'])}<br>
manifest sha256 {_esc(pkg['manifest']['sha256'])}<br>generated {_esc(pkg['manifest']['generated_at_utc'])} · RPC {_esc(pkg['manifest']['rpc'])}</p>
<div class="box">{_esc(pkg['boundaries']['note'])} Verdicts are YES, no, or unknown (incomplete). Times shown in {_esc(tz)}.</div>
<h2>Coverage</h2><ul>{cov or '<li>no completed runs</li>'}</ul>
<h2>Assertions</h2><table><tr><th>subject</th><th>predicate</th><th>object</th><th>verdict</th><th>reason</th><th>rule</th></tr>{''.join(rows) or '<tr><td colspan=6>none</td></tr>'}</table>
<h2>Evidence items</h2><table><tr><th>kind</th><th>key</th><th>block</th><th>finality</th><th>observed</th><th>hash</th></tr>{''.join(evr) or '<tr><td colspan=6>none</td></tr>'}</table>
<p>The JSON export contains raw amounts, decimals, full payloads, gaps and the versions needed to reproduce this package.</p></body></html>"""
