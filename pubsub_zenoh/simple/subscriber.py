#!/usr/bin/env python3
"""zenoh "hello" subscriber: prints every "hello <cnt>" from publisher.py on key ws-icp/simple.

By default it runs as a peer (multicast discovery, no router needed). Pass
--connect to go through the router from docker-compose.yml instead:
  python3 subscriber.py --connect tcp/localhost:7447

Requires: pip install eclipse-zenoh
Run:      python3 subscriber.py      (Ctrl-C to stop)
"""
import argparse
import time

import zenoh

KEY = "ws-icp/simple"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connect", metavar="ENDPOINT", help="router endpoint, e.g. tcp/localhost:7447")
    args = ap.parse_args()

    conf = zenoh.Config()
    if args.connect:
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", f'["{args.connect}"]')

    state = {"received": 0, "last_cnt": None}

    def on_sample(sample: zenoh.Sample):
        text = sample.payload.to_string()
        state["received"] += 1
        try:
            cnt = int(text.rsplit(" ", 1)[1])
            if state["last_cnt"] is not None and cnt != state["last_cnt"] + 1:
                print(f"gap: expected {state['last_cnt'] + 1}, got {cnt}")
            state["last_cnt"] = cnt
        except (IndexError, ValueError):
            pass
        print(f"received: {text}", flush=True)

    session = zenoh.open(conf)
    sub = session.declare_subscriber(KEY, on_sample)

    print(f"subscribed to '{KEY}'; waiting ... (Ctrl-C to stop)", flush=True)
    t0 = time.monotonic()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dt = time.monotonic() - t0
        print(f"stopped: {state['received']} messages in {dt:.1f} s ({state['received'] / dt:.1f}/s)")
        sub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
