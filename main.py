"""Sievec: one number for LA City & County services.

    guava run                      # phone (GUAVA_AGENT_NUMBER) + browser calling
    guava run -- --sim-only        # dashboard + scripted calls, no Guava connection
    uv run main.py --no-phone      # browser calling only

Dashboard: http://localhost:8311
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from datetime import timedelta
from pathlib import Path


def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

import uvicorn  # noqa: E402

from sievec import flow, llm, websearch  # noqa: E402
from sievec.bus import bus  # noqa: E402
from sievec.rules import rules  # noqa: E402
from sievec.server import create_app  # noqa: E402

PORT = int(os.environ.get("SIEVEC_PORT", "8311"))


def serve_dashboard() -> None:
    config = uvicorn.Config(create_app(), host="0.0.0.0", port=PORT, log_level="warning")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True, name="dashboard").start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sim-only", action="store_true", help="Dashboard and simulated calls only.")
    parser.add_argument("--no-phone", action="store_true", help="Don't attach to a phone number.")
    parser.add_argument("--webrtc", metavar="CODE", help="Existing grtc- code (default: create one).")
    parser.add_argument("--warm", metavar="CITIES", default=os.environ.get("SIEVEC_WARM_CITIES", ""),
                        help="Comma-separated cities to learn at startup, e.g. 'Pasadena,Burbank'.")
    args = parser.parse_args()

    try:
        from guava import logging_utils
        logging_utils.configure_logging()
    except Exception:  # noqa: BLE001
        logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("sievec")

    llm.set_trace_sink(bus.trace)
    threading.Thread(target=llm.warm, daemon=True, name="warm").start()

    phone = None if (args.no_phone or args.sim_only) else os.environ.get("GUAVA_AGENT_NUMBER")
    webrtc_code = None
    if not args.sim_only:
        if not os.environ.get("GUAVA_API_KEY"):
            raise SystemExit("Set GUAVA_API_KEY (https://app.goguava.ai/dashboard/api-keys), or pass --sim-only.")
        import guava
        webrtc_code = args.webrtc or guava.Client().create_webrtc_agent(ttl=timedelta(hours=12))

    bus.config = {
        "phone": phone,
        "webrtc_code": webrtc_code,
        "transfer_mode": flow.TRANSFER_MODE,
        "demo_number": flow.DEMO_NUMBER or None,
        "sim_only": args.sim_only,
        "web_search": "parallel" if websearch.available() else None,
        "llm": llm.config(),
        "categories": len(rules.categories),
    }
    from sievec import history
    restored = history.load_past_calls()
    serve_dashboard()
    if args.warm:
        from sievec import learn
        learn.warm_cities([c.strip() for c in args.warm.split(",") if c.strip()], flow.learn_progress)
    log.info("Sievec dashboard on http://localhost:%d  phone=%s  webrtc=%s  transfer=%s  restored=%d calls",
             PORT, phone or "-", webrtc_code or "-", flow.TRANSFER_MODE, restored)

    if args.sim_only:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    from sievec.agent import run
    # Keep the dashboard up and reattach to Guava if its connection drops
    # ("reconnection-failed" ends the runner instead of raising).
    delay = 2
    while True:
        started = time.time()
        try:
            run(phone, webrtc_code)
            log.warning("Guava listeners stopped; reconnecting in %ds", delay)
        except KeyboardInterrupt:
            return
        except Exception:  # noqa: BLE001
            log.exception("Guava runner crashed; reconnecting in %ds", delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return
        delay = 2 if time.time() - started > 120 else min(delay * 2, 60)


if __name__ == "__main__":
    main()
