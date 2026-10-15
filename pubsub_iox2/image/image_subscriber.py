#!/usr/bin/env python3
"""iceoryx2 image subscriber for the ws-icp/image service.

Receives 1280x720 BGR frames from the Rust publisher in image_publisher_rs/
over iceoryx2 shared memory and shows them with OpenCV. Each frame carries a
header of timestamp_ns (UNIX time in ns) and seq (a counter starting at 0),
like the zenoh demo in zenoh_flutter/tools/test_publisher.py.

  service   ws-icp/image, publish_subscribe, fixed-size payload, no user header
  payload   ImageFrame (C layout, 2_764_832 bytes, alignment 8):
              u64 timestamp_ns, u64 seq, u32 width, u32 height,
              u32 channels, u32 format (0 = bgr8), u8 data[1280*720*3]
  QoS       history_size 1, subscriber_max_buffer_size 2

Either side can start first (open_or_create). On every tick the subscriber
drains its buffer and shows only the newest frame, so the window never lags
behind the publisher. The overlay and a once-per-second status line report
seq, latency (now - timestamp_ns), received fps and gaps in seq (frames the
publisher sent but this subscriber never saw).

Requires: pip install iceoryx2==0.9.3 numpy opencv-python
Run:      python3 image_subscriber.py [--no-display]
          q / Esc in the window, or Ctrl-C, to stop.
"""
import argparse
import ctypes
import os
import sys
import time

import cv2
import iceoryx2 as iox2
import numpy as np
from iceoryx2.type_name import get_type_name

SERVICE_NAME = "ws-icp/image"
WIDTH, HEIGHT, CHANNELS = 1280, 720, 3
FORMAT_BGR8 = 0
FRAME_BYTES = WIDTH * HEIGHT * CHANNELS
HISTORY_SIZE = 1
SUBSCRIBER_MAX_BUFFER_SIZE = 2
WINDOW = "iox2 " + SERVICE_NAME
STATUS_PERIOD_S = 1.0


class ImageFrame(ctypes.Structure):
    """Payload of ws-icp/image; must match the #[repr(C)] Rust struct field for field."""

    _fields_ = [
        ("timestamp_ns", ctypes.c_uint64),
        ("seq", ctypes.c_uint64),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("channels", ctypes.c_uint32),
        ("format", ctypes.c_uint32),
        ("data", ctypes.c_uint8 * FRAME_BYTES),
    ]

    @staticmethod
    def type_name() -> str:
        # iceoryx2's publish_subscribe() registers get_type_name(T), which returns
        # T.type_name() when it exists. Must equal the Rust #[type_name("ImageFrame")].
        return "ImageFrame"


assert ctypes.sizeof(ImageFrame) == 2_764_832, ctypes.sizeof(ImageFrame)
assert ctypes.alignment(ImageFrame) == 8, ctypes.alignment(ImageFrame)
assert ImageFrame.data.offset == 32, ImageFrame.data.offset
# The exact name publish_subscribe(ImageFrame) registers with the service.
assert get_type_name(ImageFrame) == "ImageFrame", get_type_name(ImageFrame)


class Stats:
    """Counts received frames, seq gaps and latency; prints a status line every STATUS_PERIOD_S."""

    def __init__(self):
        self.last_seq = None
        self.gaps_total = 0  # frames missing from the seq sequence since start
        self.invalid_total = 0  # frames skipped because of a wrong width/height/channels/format
        self.fps = 0.0  # received frames per second over the last status period
        self.receiving = False
        self._reset_period(time.monotonic())

    def _reset_period(self, now):
        self.t_period = now
        self.n_received = self.n_shown = self.gaps = 0
        self.lat_sum_ms = self.lat_max_ms = 0.0

    def on_received(self, seq, latency_ms):
        if self.last_seq is not None:
            if seq > self.last_seq + 1:
                self.gaps += seq - self.last_seq - 1
                self.gaps_total += seq - self.last_seq - 1
            elif seq <= self.last_seq:
                print(f"seq went back from {self.last_seq} to {seq} (publisher restarted?)", flush=True)
        self.last_seq = seq
        self.n_received += 1
        self.lat_sum_ms += latency_ms
        self.lat_max_ms = max(self.lat_max_ms, latency_ms)

    def maybe_print(self):
        now = time.monotonic()
        elapsed = now - self.t_period
        if elapsed < STATUS_PERIOD_S:
            return
        self.fps = self.n_received / elapsed
        if self.n_received:
            self.receiving = True
            print(f"seq {self.last_seq}  fps {self.fps:5.1f}  shown {self.n_shown}  "
                  f"latency avg {self.lat_sum_ms / self.n_received:6.2f} ms max {self.lat_max_ms:6.2f} ms  "
                  f"gaps {self.gaps} (total {self.gaps_total})  invalid {self.invalid_total}", flush=True)
        elif self.receiving:
            self.receiving = False
            print(f"no frames for {elapsed:.1f} s, waiting for a publisher on '{SERVICE_NAME}' ...", flush=True)
        self._reset_period(now)


def receive_newest(subscriber, stats):
    """Receives every buffered sample, accounts for each, and returns only the newest (or None).

    Older samples are released right away, so at most one sample is borrowed at a time.
    """
    while True:
        sample = subscriber.receive()
        if sample is None:
            return None
        frame = sample.payload().contents  # ImageFrame over the shared memory, no copy
        stats.on_received(frame.seq, (time.time_ns() - frame.timestamp_ns) / 1e6)
        if not subscriber.has_samples():
            return sample
        sample.delete()


def frame_is_valid(frame):
    return (frame.width, frame.height, frame.channels, frame.format) == (WIDTH, HEIGHT, CHANNELS, FORMAT_BGR8)


def draw_text(img, text, y):
    # White text with a black outline so it stays readable on any image.
    cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)


def display_available():
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def parse_args():
    p = argparse.ArgumentParser(description=f"Show {WIDTH}x{HEIGHT} BGR frames from the iceoryx2 service '{SERVICE_NAME}'.")
    p.add_argument("--no-display", action="store_true", help="do not open a window, only print stats (headless)")
    return p.parse_args()


def run(node, subscriber, display, stats):
    """Polls the subscriber until q / Esc, the window is closed, or SIGINT / SIGTERM."""
    tick = iox2.Duration.from_millis(1)
    warned_invalid = False
    if display:
        # One reusable image: the newest frame is copied into it once and the overlay is
        # drawn on the copy, never on the (read-only, shared) iceoryx2 payload.
        canvas = np.zeros((HEIGHT, WIDTH, CHANNELS), np.uint8)
        draw_text(canvas, f"waiting for frames on {SERVICE_NAME} ...", 36)
        cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        cv2.imshow(WINDOW, canvas)

    try:
        while True:
            node.wait(tick)
            sample = receive_newest(subscriber, stats)
            if sample is not None:
                frame = sample.payload().contents
                if not frame_is_valid(frame):
                    stats.invalid_total += 1
                    if not warned_invalid:
                        warned_invalid = True
                        print(f"warning: skipping frames that are not {WIDTH}x{HEIGHT}x{CHANNELS} format "
                              f"{FORMAT_BGR8} (got {frame.width}x{frame.height}x{frame.channels} format "
                              f"{frame.format}); further ones are only counted", flush=True)
                    sample.delete()
                else:
                    stats.n_shown += 1
                    if display:
                        seq, latency_ms = frame.seq, (time.time_ns() - frame.timestamp_ns) / 1e6
                        # Zero-copy numpy view of the payload's data field in shared memory.
                        view = np.ctypeslib.as_array(frame.data).reshape(HEIGHT, WIDTH, CHANNELS)
                        view.flags.writeable = False
                        np.copyto(canvas, view)
                        del view, frame
                        sample.delete()  # the view is dangling from here on; hand the sample back early
                        draw_text(canvas, f"seq {seq}   latency {latency_ms:6.2f} ms   {stats.fps:5.1f} fps", 36)
                        cv2.imshow(WINDOW, canvas)
                    else:
                        sample.delete()
                sample = frame = None
            if display:
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:  # window closed
                    break
            stats.maybe_print()
    except (iox2.NodeWaitFailure, KeyboardInterrupt):
        pass
    finally:
        if display:
            cv2.destroyAllWindows()
        print(f"stopped: last seq {stats.last_seq}, gaps {stats.gaps_total}, invalid {stats.invalid_total}", flush=True)


def main():
    args = parse_args()
    display = not args.no_display
    if display and not display_available():
        print("no DISPLAY / WAYLAND_DISPLAY set, running as --no-display", flush=True)
        display = False

    iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)
    # The default SignalHandlingMode (HandleTerminationRequests) makes node.wait() raise
    # NodeWaitFailure on SIGINT / SIGTERM, which ends run().
    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    try:
        service = (
            node.service_builder(iox2.ServiceName.new(SERVICE_NAME))
            .publish_subscribe(ImageFrame)
            .history_size(HISTORY_SIZE)
            .subscriber_max_buffer_size(SUBSCRIBER_MAX_BUFFER_SIZE)
            .open_or_create()
        )
    except iox2.PublishSubscribeOpenOrCreateError as e:
        sys.exit(f"cannot open or create '{SERVICE_NAME}': {e!r}. The existing service does not match this "
                 f"subscriber: check the publisher's type name (ImageFrame), size ({ctypes.sizeof(ImageFrame)}), "
                 f"alignment (8), history_size ({HISTORY_SIZE}) and subscriber_max_buffer_size "
                 f"({SUBSCRIBER_MAX_BUFFER_SIZE}).")
    subscriber = service.subscriber_builder().buffer_size(SUBSCRIBER_MAX_BUFFER_SIZE).create()

    print(f"subscribed to '{SERVICE_NAME}' (payload type '{get_type_name(ImageFrame)}', "
          f"{ctypes.sizeof(ImageFrame)} bytes, align {ctypes.alignment(ImageFrame)}); waiting for frames ... "
          f"({'q / Esc in the window or ' if display else ''}Ctrl-C to stop)", flush=True)

    stats = Stats()
    try:
        run(node, subscriber, display, stats)
    finally:
        # Release the port before the service and the node: when Python drops them in another
        # order at exit, iceoryx2 logs "Unable to remove node resources".
        subscriber.delete()
        del subscriber, service, node


if __name__ == "__main__":
    main()
