"""Worker loop: lease investigation runs, execute, record assertions; poll due monitoring subscriptions; deliver the
notification outbox. One process; a second instance is safe because leases are conditional updates. Run with
`python -m service worker` or in-process with the web app (`python -m service`)."""
import logging
import os
import signal
import threading
import time

from sleuth import config as C

from . import cases, db, monitor, notify, queue
from .jobs import run_job

log = logging.getLogger("sleuth.service.worker")
IDLE_S = 3.0


class Worker:
    def __init__(self, conn=None, cfg=None, worker_id=None):
        self.conn = conn or db.connect()
        self.cfg = cfg
        self.id = worker_id or f"w-{os.getpid()}"
        self.stop = threading.Event()

    def config(self):
        if self.cfg is None:
            self.cfg = C.load()
        return self.cfg

    def run_one(self):
        """Lease and execute one run. Returns True when something was done."""
        queue.exhaust_stale(self.conn)
        row = queue.lease(self.conn, self.id)
        if not row:
            return False
        payload = (db.loads(row["result_json"]) or {}).get("input") or {}
        done = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(row["id"], done), daemon=True)
        hb.start()
        try:
            state, result = run_job(self.config(), row["kind"], payload, budget_s=row["budget_s"])
        except Exception as e:
            log.exception("job %s crashed", row["id"])
            state, result = "failed_terminal", {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            done.set()
            hb.join(timeout=10)  # the heartbeat connection must be idle before this thread writes the terminal state
        after = None
        if state in ("succeeded", "partial"):
            def after(c):  # assertions commit with the run state or not at all
                cases.record_result_tx(c, row["case_id"], row["id"], row["kind"], result)
        try:
            ok = queue.finish(self.conn, row["id"], self.id, state, result={"input": payload, **result}, error=result.get("error"), after=after)
        except (db.DbError, KeyError, TypeError, AttributeError, ValueError) as e:  # a bad result shape must not strand the run as 'leased'
            log.error("finish/record %s: %s", row["id"], e)
            ok = queue.finish(self.conn, row["id"], self.id, "retry_wait", error=f"record_result: {str(e)[:160]}")
        if not ok:
            log.warning("run %s: lease lost before finish; result dropped", row["id"])
        return True

    def _heartbeat(self, run_id, done):
        """Runs on its own SQLite connection: a sqlite3 connection must never be shared between threads."""
        try:
            c = db.connect()
        except db.DbError as e:
            log.warning("heartbeat %s: no connection: %s", run_id, e)
            return
        try:
            while not done.wait(queue.LEASE_S / 3):
                try:
                    queue.heartbeat(c, run_id, self.id)
                except db.DbError as e:
                    log.warning("heartbeat %s: %s", run_id, e)
        finally:
            c.close()

    def monitor_once(self):
        subs = monitor.due(self.conn)
        if not subs:
            return 0
        from sleuth.client import Client
        n = 0
        for s in subs:
            try:
                client = Client(self.config(), s["chain_id"], budget_s=monitor.BUDGET_S, cache=True)
                monitor.tick(client, self.conn, s)
                n += 1
            except (SystemExit, OSError, ValueError) as e:
                log.error("monitor %s: %s", s["id"], e)
        return n

    def loop(self):
        log.info("worker %s started", self.id)
        while not self.stop.is_set():
            did = False
            try:
                did = self.run_one() or did
                did = bool(self.monitor_once()) or did
                did = bool(notify.deliver_pending(self.conn)) or did
            except db.DbError as e:
                log.error("worker db error: %s", e)
                time.sleep(IDLE_S)
            if not did:
                self.stop.wait(IDLE_S)
        log.info("worker %s stopped", self.id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.stop.set())
    signal.signal(signal.SIGINT, lambda *_: w.stop.set())
    w.loop()


if __name__ == "__main__":
    main()
