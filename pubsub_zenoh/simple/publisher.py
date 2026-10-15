#!/usr/bin/env python3
"""zenoh "hello" publisher: puts the text "hello <cnt>" on key ws-icp/simple at 50 Hz.

By default it runs as a peer (multicast discovery, no router needed). Pass
--connect to go through the router from docker-compose.yml instead:
  python3 publisher.py --connect tcp/localhost:7447

Requires: pip install eclipse-zenoh
Run:      python3 publisher.py      (Ctrl-C to stop)
"""
import argparse
import time

import zenoh

KEY = "ws-icp/simple"
RATE_HZ = 50


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connect", metavar="ENDPOINT", help="router endpoint, e.g. tcp/localhost:7447")
    args = ap.parse_args()

    conf = zenoh.Config()
    if args.connect:
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", f'["{args.connect}"]')

    session = zenoh.open(conf)
    pub = session.declare_publisher(KEY, encoding=zenoh.Encoding.TEXT_PLAIN)
    period = 1.0 / RATE_HZ

    print(f"publishing 'hello <cnt>' on '{KEY}' at {RATE_HZ} Hz (Ctrl-C to stop)", flush=True)
    cnt = 0
    next_due = time.monotonic()
    try:
        while True:
            pub.put(f"hello {cnt}")
            if cnt % RATE_HZ == 0:
                print(f"sent: hello {cnt}", flush=True)
            cnt += 1
            # Fixed schedule: the next put is due one period after the previous due time,
            # so a slow tick does not lower the average rate. Resync after a long stall.
            next_due += period
            delay = next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif -delay > 1.0:
                next_due = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"stopped after {cnt} messages")
        pub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
