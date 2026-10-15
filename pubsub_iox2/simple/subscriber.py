#!/usr/bin/env python3
"""iceoryx2 "hello" subscriber: prints every "hello <cnt>" from publisher.py.

  service   ws-icp/simple, publish_subscribe, fixed-size payload
  payload   HelloMsg: u64 cnt, char text[64] (NUL-terminated)

Requires: pip install iceoryx2==0.9.3
Run:      python3 subscriber.py      (Ctrl-C to stop)
"""
import ctypes
import time

import iceoryx2 as iox2

SERVICE_NAME = "ws-icp/simple"


class HelloMsg(ctypes.Structure):
    _fields_ = [
        ("cnt", ctypes.c_uint64),
        ("text", ctypes.c_char * 64),
    ]

    @staticmethod
    def type_name() -> str:
        return "HelloMsg"


def main():
    iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)
    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    service = (
        node.service_builder(iox2.ServiceName.new(SERVICE_NAME))
        .publish_subscribe(HelloMsg)
        .open_or_create()
    )
    subscriber = service.subscriber_builder().create()

    print(f"subscribed to '{SERVICE_NAME}'; waiting ... (Ctrl-C to stop)", flush=True)
    received, last_cnt, t0 = 0, None, time.monotonic()
    try:
        while True:
            node.wait(iox2.Duration.from_millis(5))  # raises NodeWaitFailure on SIGINT / SIGTERM
            while True:
                sample = subscriber.receive()
                if sample is None:
                    break
                msg = sample.payload().contents
                cnt, text = msg.cnt, msg.text.decode()
                received += 1
                if last_cnt is not None and cnt != last_cnt + 1:
                    print(f"gap: expected {last_cnt + 1}, got {cnt}")
                last_cnt = cnt
                print(f"received: {text}", flush=True)
                sample.delete()
    except (iox2.NodeWaitFailure, KeyboardInterrupt):
        pass
    finally:
        dt = time.monotonic() - t0
        print(f"stopped: {received} messages in {dt:.1f} s ({received / dt:.1f}/s)")
        subscriber.delete()  # release the port before the service and node
        del subscriber, service, node


if __name__ == "__main__":
    main()
