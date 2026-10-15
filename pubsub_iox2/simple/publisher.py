#!/usr/bin/env python3
"""iceoryx2 "hello" publisher: sends the text "hello <cnt>" at 50 Hz.

  service   ws-icp/simple, publish_subscribe, fixed-size payload
  payload   HelloMsg: u64 cnt, char text[64] (NUL-terminated)

Requires: pip install iceoryx2==0.9.3
Run:      python3 publisher.py      (Ctrl-C to stop)
"""
import ctypes
import time

import iceoryx2 as iox2

SERVICE_NAME = "ws-icp/simple"
RATE_HZ = 50


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
    publisher = service.publisher_builder().create()
    period = iox2.Duration.from_millis(1000 // RATE_HZ)

    print(f"publishing 'hello <cnt>' on '{SERVICE_NAME}' at {RATE_HZ} Hz (Ctrl-C to stop)", flush=True)
    cnt = 0
    try:
        while True:
            node.wait(period)  # raises NodeWaitFailure on SIGINT / SIGTERM
            sample = publisher.loan_uninit()
            msg = sample.payload().contents  # write straight into shared memory
            msg.cnt = cnt
            msg.text = f"hello {cnt}".encode()
            sample.assume_init().send()
            if cnt % RATE_HZ == 0:
                print(f"sent: hello {cnt}", flush=True)
            cnt += 1
    except (iox2.NodeWaitFailure, KeyboardInterrupt):
        pass
    finally:
        print(f"stopped after {cnt} messages")
        publisher.delete()  # release the port before the service and node
        del publisher, service, node


if __name__ == "__main__":
    main()
