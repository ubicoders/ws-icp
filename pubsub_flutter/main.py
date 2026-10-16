#!/usr/bin/env python3
"""pubsub_flutter: synthetic sensor data for the zenoh_flutter viewer, as ROS2 messages over zenoh.

Publishes five topics so every card type in the app can be exercised. Each one
carries a ROS2-style message from the sb vault (style `ros2`) and goes out
through a publisher class that `sb io add pub` generated into
swarmbotix_io/publishers/ (see sb.module.yaml). The keys follow sb's
`<device>/<workspace>/<module>/zenoh/<topic>`; the last column is the zenoh
encoding each publisher stamps on every sample (the generated `WIRE_TAG`),
which is how the viewer and `sb topic listen` pick their decoder:

  elcom/ws_icp/pubsub_flutter/zenoh/imu          50 Hz  application/protobuf;ros2/sensor_msgs/Imu
  elcom/ws_icp/pubsub_flutter/zenoh/temperature  50 Hz  application/flatbuffers;ros2/sensor_msgs/Temperature
  elcom/ws_icp/pubsub_flutter/zenoh/chatter      2 Hz   application/protobuf;ros2/std_msgs/String
  elcom/ws_icp/pubsub_flutter/zenoh/cam          30 Hz  application/protobuf;ros2/sensor_msgs/CompressedImage
  elcom/ws_icp/pubsub_flutter/zenoh/raw          5 Hz   application/protobuf;ros2/std_msgs/UInt8MultiArray

  imu          linear_acceleration (sin t, cos t, 9.806) m/s^2, angular_velocity
               (0.0007, 0.0012, -0.0003) rad/s, identity orientation, covariances
               left empty (unknown).
  temperature  the IMU's temperature from the same tick, 36.4 +- 0.5 degrees C on a
               slow sine, as a FlatBuffers table: the one topic for that decoder.
  chatter      the text "hello world <n>", n counting from 0.
  cam          a 320x240 JPEG (Pillow) with a moving rectangle and "frame <n>",
               format "jpeg". Skipped when Pillow is not installed.
  raw          256 bytes (n + k) % 256 for k = 0..255, layout: one dimension "bytes".

imu, temperature and cam start with a std_msgs/Header: stamp {sec, nanosec} is
the UNIX time of the sample and frame_id is "imu" (imu, temperature) or "cam".
A ROS2 Header has no seq; the running counters are the n above.

The device and workspace segments were baked in by `sb io add` (device from
~/.swarmbotix/sb.config.yml, workspace = the active one). After changing either,
run `sb io update` in this folder to regenerate swarmbotix_io/.

The session is a zenoh peer: it finds the phone, and anything else on the LAN,
by multicast scouting, with no router. Where multicast is blocked, go through a
router instead (`docker compose up -d` in the repo root starts one):
  python3 pubsub_flutter/main.py --connect tcp/<router-ip>:7447

Requires: pip install -r pubsub_flutter/requirements.txt, and the vault compiled
          (proto for all five types, fb for ros2/sensor_msgs/Temperature).
Run:      python3 pubsub_flutter/main.py     (Ctrl-C to stop)
"""
import argparse, importlib.util, io, itertools, json, math, os, subprocess, sys, time, types
from pathlib import Path

import zenoh

# The generated publishers. main.py's folder is first on sys.path, so swarmbotix_io/ imports as a package.
from swarmbotix_io.publishers import cam, chatter, imu, raw, temperature


def vault_targets():
    """<messages_root>/ros2/message_targets: $SB_MESSAGE_TARGETS, else sb's messages_root, else ~/.swarmbotix/messages."""
    if os.environ.get("SB_MESSAGE_TARGETS"):
        return Path(os.environ["SB_MESSAGE_TARGETS"])
    try:
        root = subprocess.run(["sb", "config", "get", "messages_root"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        root = Path.home() / ".swarmbotix" / "messages"
    return Path(root) / "ros2" / "message_targets"

TARGETS = vault_targets()

def add_proto_package(pkg):
    """Registers `pkg` as a package spanning every proto/<pkg>/<Leaf>/ folder of the vault.

    protoc's Python output imports its dependencies as `from std_msgs import Header_pb2`,
    which expects all of a package's _pb2 files in one folder; the vault keeps each
    message in a <Leaf>/ folder of its own."""
    mod = types.ModuleType(pkg)
    mod.__path__ = [str(d) for d in sorted((TARGETS / "proto" / pkg).iterdir()) if d.is_dir()]
    sys.modules[pkg] = mod

def load_fb(pkg, leaf):
    """Loads the FlatBuffers bindings fb/<pkg>/<Leaf>/<Leaf>.py by path, under a private
    name, since the fb tree reuses the package names of the proto tree."""
    spec = importlib.util.spec_from_file_location(f"fb_{pkg}_{leaf}", TARGETS / "fb" / pkg / leaf / f"{leaf}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

try:
    import flatbuffers
    for pkg in ("builtin_interfaces", "std_msgs", "geometry_msgs", "sensor_msgs"):
        add_proto_package(pkg)
    from sensor_msgs.Imu_pb2 import Imu
    from sensor_msgs.CompressedImage_pb2 import CompressedImage
    from std_msgs.String_pb2 import String
    from std_msgs.UInt8MultiArray_pb2 import UInt8MultiArray
    fb_time, fb_header, fb_temperature = (load_fb("builtin_interfaces", "Time"), load_fb("std_msgs", "Header"),
                                          load_fb("sensor_msgs", "Temperature"))
except Exception as e:  # missing runtime, vault not compiled, or protobuf older than the protoc that compiled the vault
    sys.exit(f"pubsub_flutter: cannot load the message bindings from {TARGETS} ({e}); run "
             "`pip install -r pubsub_flutter/requirements.txt`, and compile the vault with "
             "`sb message compile --style ros2` (or `sb message compile ros2/sensor_msgs/Temperature --fb`)")

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


def make_jpeg(n):
    img = Image.new("RGB", (320, 240), (20 + (n * 3) % 200, 60, 120))
    d = ImageDraw.Draw(img)
    d.rectangle([20 + (n * 5) % 250, 80, 70 + (n * 5) % 250, 160], fill=(240, 200, 40))
    d.text((10, 10), f"frame {n}", fill=(255, 255, 255))
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=70); return buf.getvalue()

def stamp(header, frame_id, ns):
    """Fills a std_msgs/Header with ns (UNIX time in ns) and frame_id."""
    header.stamp.sec, header.stamp.nanosec = divmod(ns, 1_000_000_000)
    header.frame_id = frame_id

def temperature_flatbuffer(ns, celsius):
    """Serializes a FlatBuffers sensor_msgs/Temperature. FlatBuffers builds inside out:
    the frame_id string, then the Time and Header tables, then the root table."""
    b = flatbuffers.Builder(64)
    frame_id = b.CreateString("imu")
    sec, nanosec = divmod(ns, 1_000_000_000)
    fb_time.TimeStart(b); fb_time.TimeAddSec(b, sec); fb_time.TimeAddNanosec(b, nanosec)
    t = fb_time.TimeEnd(b)
    fb_header.HeaderStart(b); fb_header.HeaderAddStamp(b, t); fb_header.HeaderAddFrameId(b, frame_id)
    h = fb_header.HeaderEnd(b)
    fb_temperature.TemperatureStart(b)
    fb_temperature.TemperatureAddHeader(b, h)
    fb_temperature.TemperatureAddTemperature(b, celsius)
    fb_temperature.TemperatureAddVariance(b, 0.0)  # 0: variance unknown
    b.Finish(fb_temperature.TemperatureEnd(b))
    return bytes(b.Output())


def main():
    ap = argparse.ArgumentParser(description="Publishes the zenoh_flutter demo topics as ROS2 messages.")
    ap.add_argument("--connect", metavar="ENDPOINT",
                    help="run as a client of this router, e.g. tcp/localhost:7447 (default: peer, multicast scouting)")
    args = ap.parse_args()

    conf = zenoh.Config()
    if args.connect:
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", json.dumps([args.connect]))
    else:
        conf.insert_json5("mode", '"peer"')  # zenoh's default, spelled out: the phone finds us by scouting
    session = zenoh.open(conf)

    imu_pub = imu.ImuPublisher(session)
    tmp_pub = temperature.TemperaturePublisher(session)
    txt_pub = chatter.ChatterPublisher(session)
    cam_pub = cam.CamPublisher(session)
    raw_pub = raw.RawPublisher(session)
    t0 = time.monotonic()
    txt_n, cam_n, raw_n = itertools.count(), itertools.count(), itertools.count()

    def put_imu():
        # One sample, published twice: the motion as protobuf Imu, its temperature as FlatBuffers.
        t, ns = time.monotonic() - t0, time.time_ns()
        m = Imu()
        stamp(m.header, "imu", ns)
        m.orientation.w = 1.0
        m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z = 0.0007, 0.0012, -0.0003
        m.linear_acceleration.x, m.linear_acceleration.y, m.linear_acceleration.z = (
            round(math.sin(t), 4), round(math.cos(t), 4), 9.806)
        imu_pub.publish(m)
        tmp_pub.publish(temperature_flatbuffer(ns, round(36.4 + 0.5 * math.sin(t / 4), 3)))

    def put_text():
        txt_pub.publish(String(data=f"hello world {next(txt_n)}"))

    def put_cam():
        m = CompressedImage(format="jpeg", data=make_jpeg(next(cam_n)))
        stamp(m.header, "cam", time.time_ns())
        cam_pub.publish(m)

    def put_raw():
        n = next(raw_n)
        m = UInt8MultiArray(data=bytes((n + k) % 256 for k in range(256)))
        m.layout.dim.add(label="bytes", size=256, stride=256)
        raw_pub.publish(m)

    # [next due (monotonic s), period s, action]. The loop sleeps until the earliest
    # due entry, runs it and moves its due time on by one period from the old due
    # time, so slow ticks do not lower the average rate.
    schedule = [[t0, 1 / 50, put_imu], [t0, 1 / 2, put_text], [t0, 1 / 5, put_raw]]
    if Image: schedule.append([t0, 1 / 60, put_cam])

    mode = f"client of {args.connect}" if args.connect else "peer, multicast scouting"
    print(f"pubsub_flutter: zenoh {mode}, publishing (Ctrl-C to stop):", flush=True)
    for mod, pub, rate in ((imu, imu_pub, "50 Hz"), (temperature, tmp_pub, "50 Hz"), (chatter, txt_pub, "2 Hz"),
                           (cam, cam_pub, "30 Hz" if Image else "off"), (raw, raw_pub, "5 Hz")):
        print(f"  {pub.key_expr:<46} {rate:<6} {mod.WIRE_TAG}", flush=True)
    if not Image:
        print("  cam is off: `pip install pillow` to enable it", flush=True)
    try:
        while True:
            entry = min(schedule, key=lambda e: e[0])
            delay = entry[0] - time.monotonic()
            if delay > 0: time.sleep(delay)
            entry[2]()
            entry[0] += entry[1]
            # After a long stall (suspend, debugger) resync instead of publishing a burst to catch up.
            if time.monotonic() - entry[0] > 1.0: entry[0] = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        for pub in (imu_pub, tmp_pub, txt_pub, cam_pub, raw_pub):
            pub.close()
        try:
            session.close()
        except zenoh.ZError as e:  # a peer that never acknowledged the close gives up after 10 s; nothing is lost
            print(f"pubsub_flutter: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
