#!/usr/bin/env python3
"""pubsub_flutter: synthetic sensor data for the zenoh_flutter viewer, as ROS2 messages over zenoh.

Publishes nine topics so every card type in the app can be exercised. Each one
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
  elcom/ws_icp/pubsub_flutter/zenoh/markers      20 Hz  application/protobuf;ros2/custom/MarkerDetections
  elcom/ws_icp/pubsub_flutter/zenoh/cube_poses   20 Hz  application/protobuf;ros2/custom/CubePoses
  elcom/ws_icp/pubsub_flutter/zenoh/image        10 Hz  application/protobuf;ros2/sensor_msgs/Image
  elcom/ws_icp/pubsub_flutter/zenoh/debug_image  10 Hz  application/protobuf;ros2/sensor_msgs/Image

  imu          linear_acceleration (sin t, cos t, 9.806) m/s^2, angular_velocity
               (0.0007, 0.0012, -0.0003) rad/s, identity orientation, covariances
               left empty (unknown).
  temperature  the IMU's temperature from the same tick, 36.4 +- 0.5 degrees C on a
               slow sine, as a FlatBuffers table: the one topic for that decoder.
  chatter      the text "hello world <n>", n counting from 0.
  cam          a 320x240 JPEG (Pillow) with a moving rectangle and "frame <n>",
               format "jpeg". Skipped when Pillow is not installed.
  raw          256 bytes (n + k) % 256 for k = 0..255, layout: one dimension "bytes".
  markers      what camera "sb00" would report about three AprilTags 36h11 (ids 0, 1,
               2, family "april0 (AprilTag 36h11)") circling slowly 1.05-1.45 m in
               front of it, within +-0.3 m in x and y: position, a slow spin about
               the optical axis as quaternion and XYZ Euler degrees, distance.
               Frames 95-99 of every 100 (0.25 s every 5 s) see no marker: the
               empty-list heartbeat a detector sends when nothing is in view.
  cube_poses   two marker cubes (cube_id 0, 1; marker ids 5k..5k+4) going round a
               0.5 m circle in the world at z = 0.05, facing along it: position,
               velocity, rotation as a row-major 3x3 matrix, XYZ Euler degrees and
               quaternion, 2 cameras, residual about 2 mm, alive. Cube 1 goes stale
               for frames 160-199 of every 200 (2 s every 10 s): alive false, pose
               fields unset, last_seen frozen at its last alive frame.
  image        a 640x512 grey test pattern (eight grey bars under a 64 px grid) with a
               white square on a slow Lissajous path and "frame <n>" top left, as
               raw pixels: encoding "mono8", step 640, is_bigendian 0, data
               640 * 512 = 327680 bytes, one per pixel, row by row from the top.
  debug_image  the same scene in colour (colour bars, a yellow square) with three
               green boxes labelled "id 0".."id 2" circling over it, like the outlines
               a marker detector draws on its debug view: encoding "rgb8" (R, G, B
               bytes per pixel), step 1920, data 1920 * 512 = 983040 bytes.
               image and debug_image are skipped, like cam, without Pillow.

imu, temperature, cam, image and debug_image start with a std_msgs/Header: stamp
{sec, nanosec} is the UNIX time of the sample and frame_id is "imu" (imu,
temperature) or "cam" (the three images).
A ROS2 Header has no seq; the running counters are the n above. markers and
cube_poses have no Header but their own stamp_sec/stamp_nanosec (UNIX time) and
frame_seq (counting from 0). They are the ros2/custom messages (proto package
swarmbotix.custom) that the ws-ips pipeline publishes, so a decoder that reads
these reads the real ones too. Their poses are RDF (x right, y down, z forward):
the camera optical frame for markers, the world frame for cube_poses.
image and debug_image are the ros2/sensor_msgs/Image that the ws-ips image relay
(module ips_image_relay) will publish on elcom/ws_ips/ips_image_relay-sbNN/zenoh/image
and .../debug_image: 640x512 copies of a camera stream ("mono8") and of the
detector's debug view ("rgb8").

The device and workspace segments were baked in by `sb io add` (device from
~/.swarmbotix/sb.config.yml, workspace = the active one). After changing either,
run `sb io update` in this folder to regenerate swarmbotix_io/.

The session is a zenoh peer: it finds the phone, and anything else on the LAN,
by multicast scouting, with no router. Where multicast is blocked, go through a
router instead (`docker compose up -d` in the repo root starts one):
  python3 pubsub_flutter/main.py --connect tcp/<router-ip>:7447

Requires: pip install -r pubsub_flutter/requirements.txt, and the vault compiled
          (proto for all eight types, fb for ros2/sensor_msgs/Temperature).
Run:      python3 pubsub_flutter/main.py     (Ctrl-C to stop)
"""
import argparse, functools, importlib.util, io, itertools, json, math, os, subprocess, sys, time, types
from pathlib import Path

import zenoh

# The generated publishers. main.py's folder is first on sys.path, so swarmbotix_io/ imports as a package.
from swarmbotix_io.publishers import cam, chatter, cube_poses, debug_image, image, imu, markers, raw, temperature


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

    protoc's Python output imports its dependencies as `from std_msgs import Header_pb2`
    (or `from custom import MarkerDetections_pb2`, in CubePoses_pb2), which expects all
    of a package's _pb2 files in one folder; the vault keeps each message in a <Leaf>/
    folder of its own."""
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
    for pkg in ("builtin_interfaces", "std_msgs", "geometry_msgs", "sensor_msgs", "custom"):
        add_proto_package(pkg)
    from sensor_msgs.Imu_pb2 import Imu
    from sensor_msgs.CompressedImage_pb2 import CompressedImage
    from sensor_msgs.Image_pb2 import Image as ImageMsg  # renamed: `Image` is Pillow's below
    from std_msgs.String_pb2 import String
    from std_msgs.UInt8MultiArray_pb2 import UInt8MultiArray
    from custom.MarkerDetections_pb2 import MarkerDetections
    from custom.CubePoses_pb2 import CubePoses
    fb_time, fb_header, fb_temperature = (load_fb("builtin_interfaces", "Time"), load_fb("std_msgs", "Header"),
                                          load_fb("sensor_msgs", "Temperature"))
except Exception as e:  # missing runtime, vault not compiled, or protobuf older than the protoc that compiled the vault
    sys.exit(f"pubsub_flutter: cannot load the message bindings from {TARGETS} ({e}); run "
             "`pip install -r pubsub_flutter/requirements.txt`, and compile the vault with "
             "`sb message compile --style ros2` (or `sb message compile ros2/sensor_msgs/Temperature --fb`)")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None


def make_jpeg(n):
    img = Image.new("RGB", (320, 240), (20 + (n * 3) % 200, 60, 120))
    d = ImageDraw.Draw(img)
    d.rectangle([20 + (n * 5) % 250, 80, 70 + (n * 5) % 250, 160], fill=(240, 200, 40))
    d.text((10, 10), f"frame {n}", fill=(255, 255, 255))
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=70); return buf.getvalue()

# image and debug_image: the frame size of the ws-ips cameras that ips_image_relay copies.
IMG_W, IMG_H = 640, 512

@functools.cache
def load_font(size):
    """Pillow's built-in font at `size` px (Pillow before 10.1 only has a small bitmap one)."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

@functools.cache
def test_pattern(mode):
    """The still background of image (mode "L", grey) and debug_image ("RGB"): eight
    vertical bars, grey levels dark to light or colour bars, under a black 64 px grid."""
    img = Image.new(mode, (IMG_W, IMG_H))
    d = ImageDraw.Draw(img)
    bars = ([16 + 32 * k for k in range(8)] if mode == "L" else
            [(191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
             (191, 0, 191), (191, 0, 0), (0, 0, 191), (24, 24, 24)])
    for k, fill in enumerate(bars):
        d.rectangle([k * IMG_W // 8, 0, (k + 1) * IMG_W // 8 - 1, IMG_H - 1], fill=fill)
    for x in range(64, IMG_W, 64): d.line([x, 0, x, IMG_H - 1], fill="black")
    for y in range(64, IMG_H, 64): d.line([0, y, IMG_W - 1, y], fill="black")
    return img

def draw_scene(mode, n, square):
    """Frame n of the image scene in `mode`: the test pattern, an 80 px square of colour
    `square` on a slow Lissajous path, and "frame <n>" top left. Returns image and Draw."""
    img = test_pattern(mode).copy()
    d = ImageDraw.Draw(img)
    a = n / 20
    cx, cy = int(IMG_W / 2 + 230 * math.sin(a)), int(IMG_H / 2 + 170 * math.sin(1.7 * a))
    d.rectangle([cx - 40, cy - 40, cx + 40, cy + 40], fill=square, outline="black", width=2)
    d.rectangle([8, 8, 200, 46], fill="black")
    d.text((16, 12), f"frame {n}", fill="white", font=load_font(28))
    return img, d

def make_mono(n):
    """image frame n as "mono8" pixels: one byte per pixel, IMG_W * IMG_H bytes."""
    img, _ = draw_scene("L", n, "white")
    return img.tobytes()

def make_rgb(n):
    """debug_image frame n as "rgb8" pixels (3 * IMG_W * IMG_H bytes): the scene in colour
    with three green boxes, each with a centre cross and an "id <k>" label, circling it."""
    img, d = draw_scene("RGB", n, (255, 230, 40))
    green = (0, 255, 0)
    for k in range(3):
        a = n / 40 + k * 2 * math.pi / 3  # one lap in 25 s at 10 Hz
        cx, cy = int(IMG_W / 2 + 150 * math.cos(a)), int(IMG_H / 2 + 110 * math.sin(a))
        d.rectangle([cx - 36, cy - 36, cx + 36, cy + 36], outline=green, width=3)
        d.line([cx - 6, cy, cx + 6, cy], fill=green, width=2)
        d.line([cx, cy - 6, cx, cy + 6], fill=green, width=2)
        x0, y0, x1, y1 = d.textbbox((cx - 36, cy - 62), f"id {k}", font=load_font(18))
        d.rectangle([x0 - 3, y0 - 2, x1 + 3, y1 + 2], fill="black")
        d.text((cx - 36, cy - 62), f"id {k}", fill=green, font=load_font(18))
    return img.tobytes()

def stamp(header, frame_id, ns):
    """Fills a std_msgs/Header with ns (UNIX time in ns) and frame_id."""
    header.stamp.sec, header.stamp.nanosec = divmod(ns, 1_000_000_000)
    header.frame_id = frame_id

def set_yaw(q, euler_deg, yaw, rotation=None):
    """Fills a custom Quat, a Vec3 of XYZ Euler degrees and, if given, a row-major Mat3
    with one rotation of yaw radians about Z. The angle is wrapped to [-180, 180] and
    rounded to 0.1 degree first, so the three agree (to 1e-6) and q.w >= 0."""
    deg = round(math.degrees(math.atan2(math.sin(yaw), math.cos(yaw))), 1)
    r = math.radians(deg)
    q.x, q.y, q.z, q.w = 0.0, 0.0, round(math.sin(r / 2), 6), round(math.cos(r / 2), 6)
    euler_deg.x, euler_deg.y, euler_deg.z = 0.0, 0.0, deg
    if rotation is not None:
        c, s = round(math.cos(r), 6), round(math.sin(r), 6)
        rotation.m.extend([c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0])

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
    mrk_pub = markers.MarkersPublisher(session)
    cub_pub = cube_poses.CubePosesPublisher(session)
    img_pub = image.ImagePublisher(session)
    dbg_pub = debug_image.DebugImagePublisher(session)
    t0 = time.monotonic()
    txt_n, cam_n, raw_n = itertools.count(), itertools.count(), itertools.count()
    mrk_n, cub_n, img_n, dbg_n = itertools.count(), itertools.count(), itertools.count(), itertools.count()
    last_fused = {}  # cube_id -> UNIX ns of the cube's last alive frame

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

    def put_image():
        # Raw 8-bit grey pixels, one byte each: step (bytes per row) = width.
        m = ImageMsg(height=IMG_H, width=IMG_W, encoding="mono8", is_bigendian=0, step=IMG_W,
                     data=make_mono(next(img_n)))
        stamp(m.header, "cam", time.time_ns())
        img_pub.publish(m)

    def put_debug_image():
        # Raw R, G, B bytes per pixel: step = 3 * width.
        m = ImageMsg(height=IMG_H, width=IMG_W, encoding="rgb8", is_bigendian=0, step=3 * IMG_W,
                     data=make_rgb(next(dbg_n)))
        stamp(m.header, "cam", time.time_ns())
        dbg_pub.publish(m)

    def put_raw():
        n = next(raw_n)
        m = UInt8MultiArray(data=bytes((n + k) % 256 for k in range(256)))
        m.layout.dim.add(label="bytes", size=256, stride=256)
        raw_pub.publish(m)

    def put_markers():
        # Three tags on small circles in front of camera sb00, each spinning slowly in the
        # image plane; frames 95-99 of every 100 go out with an empty `markers` list.
        t, ns, n = time.monotonic() - t0, time.time_ns(), next(mrk_n)
        m = MarkerDetections(camera="sb00", frame_seq=n)
        m.stamp_sec, m.stamp_nanosec = divmod(ns, 1_000_000_000)
        if n % 100 < 95:
            for k in range(3):
                a = 0.4 * t + k * 2 * math.pi / 3  # one lap in 16 s
                d = m.markers.add(id=k, family="april0 (AprilTag 36h11)")
                p = d.position
                p.x = round(0.18 * (k - 1) + 0.08 * math.cos(a), 4)
                p.y = round(0.05 * (k - 1) + 0.08 * math.sin(a), 4)
                p.z = round(1.1 + 0.15 * k + 0.05 * math.sin(a / 2), 4)
                set_yaw(d.orientation, d.euler_deg, 0.3 * t + k)
                d.distance = round(math.hypot(p.x, p.y, p.z), 4)
        mrk_pub.publish(m)

    def put_cubes():
        # Both cubes on one 0.5 m circle, half a lap apart, heading along it. A stale cube
        # keeps only cube_id, alive (false, so absent on the wire) and last_seen.
        t, ns, n = time.monotonic() - t0, time.time_ns(), next(cub_n)
        m = CubePoses(frame_seq=n)
        m.stamp_sec, m.stamp_nanosec = divmod(ns, 1_000_000_000)
        w = 0.3  # rad/s: 0.15 m/s, one lap in 21 s
        for k in (0, 1):
            alive = not (k == 1 and n % 200 >= 160)
            c = m.cubes.add(cube_id=k, alive=alive)
            if alive:
                last_fused[k] = ns
                a = w * t + k * math.pi
                c.position.x, c.position.y, c.position.z = round(0.5 * math.cos(a), 4), round(0.5 * math.sin(a), 4), 0.05
                c.velocity.x, c.velocity.y, c.velocity.z = (round(-0.5 * w * math.sin(a), 4),
                                                            round(0.5 * w * math.cos(a), 4), 0.0)
                set_yaw(c.orientation, c.euler_deg, a + math.pi / 2, c.rotation)
                c.marker_ids.extend(range(5 * k, 5 * k + 5))
                c.num_cameras = 2
                c.residual_rms = round(0.002 + 0.0003 * math.sin(2 * t + k), 5)
            c.last_seen_sec, c.last_seen_nanosec = divmod(last_fused[k], 1_000_000_000)
        cub_pub.publish(m)

    # [next due (monotonic s), period s, action]. The loop sleeps until the earliest
    # due entry, runs it and moves its due time on by one period from the old due
    # time, so slow ticks do not lower the average rate.
    schedule = [[t0, 1 / 50, put_imu], [t0, 1 / 2, put_text], [t0, 1 / 5, put_raw],
                [t0, 1 / 20, put_markers], [t0, 1 / 20, put_cubes]]
    if Image: schedule += [[t0, 1 / 60, put_cam], [t0, 1 / 10, put_image], [t0, 1 / 10, put_debug_image]]

    mode = f"client of {args.connect}" if args.connect else "peer, multicast scouting"
    print(f"pubsub_flutter: zenoh {mode}, publishing (Ctrl-C to stop):", flush=True)
    for mod, pub, rate in ((imu, imu_pub, "50 Hz"), (temperature, tmp_pub, "50 Hz"), (chatter, txt_pub, "2 Hz"),
                           (cam, cam_pub, "30 Hz" if Image else "off"), (raw, raw_pub, "5 Hz"),
                           (markers, mrk_pub, "20 Hz"), (cube_poses, cub_pub, "20 Hz"),
                           (image, img_pub, "10 Hz" if Image else "off"),
                           (debug_image, dbg_pub, "10 Hz" if Image else "off")):
        print(f"  {pub.key_expr:<46} {rate:<6} {mod.WIRE_TAG}", flush=True)
    if not Image:
        print("  cam, image and debug_image are off: `pip install pillow` to enable them", flush=True)
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
        for pub in (imu_pub, tmp_pub, txt_pub, cam_pub, raw_pub, mrk_pub, cub_pub, img_pub, dbg_pub):
            pub.close()
        try:
            session.close()
        except zenoh.ZError as e:  # a peer that never acknowledged the close gives up after 10 s; nothing is lost
            print(f"pubsub_flutter: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
