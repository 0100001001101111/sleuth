"""`python -m service` runs web + worker in one process (pilot); `python -m service worker` / `web` run one half."""
import logging
import os
import sys
import threading


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mode = argv[1] if len(argv) > 1 else "all"
    if mode not in ("all", "web", "worker"):
        print("usage: python -m service [all|web|worker]", file=sys.stderr)
        return 2
    if mode == "worker":
        from .worker import main as wmain
        wmain()
        return 0
    if mode == "all":
        from .worker import Worker
        w = Worker()
        threading.Thread(target=w.loop, daemon=True, name="worker").start()
    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed: .venv/bin/pip install fastapi uvicorn jinja2 python-multipart", file=sys.stderr)
        return 1
    host = os.environ.get("SLEUTH_HOST", "127.0.0.1")  # loopback by default: expose only behind TLS/a reverse proxy
    uvicorn.run("service.app:app", host=host, port=int(os.environ.get("SLEUTH_PORT", "8400")), log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
