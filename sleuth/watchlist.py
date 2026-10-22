"""Persistent per-token watchlist: who is tracked, why, and the supporting tx hashes. Plain JSON, human-editable."""
import json, time
from . import config as C

PATH = C.DATA / "watchlist.json"


def _load_all():
    if PATH.exists():
        try:
            return json.loads(PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return {"_corrupt_backup": PATH.read_text()[:2000]}
    return {}


class Watchlist:
    def __init__(self, chain_id, token):
        self.key = f"{chain_id}:{token.lower()}"
        self.all = _load_all()
        self.data = self.all.setdefault(self.key, {"addresses": {}, "pools": [], "launch": None, "last_block": None})

    # Evidence-backed roles accumulate in `roles` (an address can be launcher AND seller); `role` stays the
    # highest-priority one so old single-role readers keep working and a dev label can never be downgraded.
    PRIORITY = ("launcher", "dev", "initial_recipient", "funder", "traced", "sibling", "seller", "tracked")

    def add(self, addr, role, reason, evidence):
        if not addr:
            return
        a = addr.lower()
        e = self.data["addresses"].get(a)
        if e:
            roles = set(e.get("roles") or ([e["role"]] if e.get("role") else []))
            roles.add(role)
            primary = next((r for r in self.PRIORITY if r in roles), role)
            ev = list(dict.fromkeys((e.get("evidence") or []) + [x for x in evidence if x]))
            e.update(role=primary, roles=sorted(roles), evidence=ev[:12], updated=time.time())
            if reason and reason not in e["reason"]:
                e["reason"] = (e["reason"] + "; " + reason)[:400]
        else:
            self.data["addresses"][a] = {"role": role, "roles": [role], "reason": reason,
                                         "evidence": [x for x in evidence if x][:12], "added": time.time()}

    def save(self):
        C.DATA.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.all, indent=1))
        tmp.replace(PATH)

    def by_role(self, *roles):
        return [a for a, e in self.data["addresses"].items()
                if set(e.get("roles") or ([e["role"]] if e.get("role") else [])) & set(roles)]
