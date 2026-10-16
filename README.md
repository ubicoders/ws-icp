# Lab: pub/sub with zenoh and iceoryx2

In this lab you will send messages between two programs in five different ways.
Each step builds on the one before, so go in order. Every step uses two
terminals: one for the publisher (the sender) and one for the subscriber
(the receiver).

Before you start, install the Python packages once:

```sh
pip install -r pubsub_zenoh/requirements.txt
pip install -r pubsub_iox2/requirements.txt
pip install -r pubsub_flutter/requirements.txt
```

---

## Step 1. Hello over zenoh

zenoh is a network pub/sub library. In this step both programs run on your
computer and find each other on their own.

**Terminal 1.** Start the publisher. It sends `hello 0`, `hello 1`, `hello 2`, ...
fifty times per second.

```sh
python3 pubsub_zenoh/simple/publisher.py
```

**Terminal 2.** Start the subscriber.

```sh
python3 pubsub_zenoh/simple/subscriber.py
```

You should see `received: hello 123`, `received: hello 124`, ... scrolling by
in terminal 2. The numbers continue from wherever the publisher is right now.

Press Ctrl-C in terminal 2. The last line tells you how many messages arrived
and the rate, which should be close to 50 per second. Then Ctrl-C in terminal 1.

> If terminal 2 shows a `gap:` line, a message was skipped. That is normal
> once or twice at startup.

---

## Step 2. Hello over iceoryx2

iceoryx2 does the same job without a network. The publisher writes into a
block of shared memory and the subscriber reads that same memory. Nothing is
copied and nothing goes through a socket.

**Terminal 1.** Start the publisher.

```sh
python3 pubsub_iox2/simple/publisher.py
```

**Terminal 2.** Start the subscriber.

```sh
python3 pubsub_iox2/simple/subscriber.py
```

You should see the same `received: hello N` stream as in step 1.

Stop both with Ctrl-C.

Notice one thing in the code: both scripts define the exact same `HelloMsg`
struct. With shared memory, the subscriber must know the memory layout, so the
two sides have to agree on it ahead of time. zenoh did not need that, because
it just sent bytes.

---

## Step 3. Images over iceoryx2

Now something bigger. A Rust program draws a 1280x720 colour image, 100 times
per second, and a Python program shows it. Each image is 2.7 MB. Thanks to
shared memory, the Python side displays the publisher's pixels in place without
ever copying them.

**Terminal 1.** Build the publisher once, then run it.

```sh
cd pubsub_iox2/image/image_publisher_rs
cargo build --release
./target/release/image_publisher_rs
```

The first build takes a minute. Once it runs, you will see a line per second
such as `seq 300 | 100.0 fps | 0 subscriber(s)`.

> If the build fails because of OpenCV, install the dev libraries:
> `sudo apt install libopencv-dev clang libclang-dev` and build again.

**Terminal 2.** Start the subscriber, from the project root.

```sh
python3 pubsub_iox2/image/image_subscriber.py
```

A window opens with a moving yellow rectangle and a frame counter. The overlay
in the corner shows the latency from "drawn" to "shown", which should be a
couple of milliseconds. Back in terminal 1 the count of subscribers goes to 1.

Press `q` in the window to close the subscriber, then Ctrl-C in terminal 1.

> No screen (SSH session)? Run the subscriber with `--no-display`. It then
> prints the frame stats instead of opening a window.

---

## Step 4. Hello across two computers

iceoryx2 cannot do this step: shared memory only exists inside one computer.
zenoh can, and this is where the router from `docker-compose.yml` comes in.
The router is a meeting point. Both programs connect to it, and it forwards
messages between them, so they do not need to discover each other.

You need a second computer on the same network with `pip install eclipse-zenoh`
done and a copy of `pubsub_zenoh/simple/subscriber.py`.

**On this computer, terminal 1.** Start the router and wait until it reports
healthy.

```sh
docker compose up -d
docker compose ps
```

**Still terminal 1.** Start the publisher, exactly as in step 1.

```sh
python3 pubsub_zenoh/simple/publisher.py
```

Nothing to configure here: a zenoh peer looks for routers on the local
network by multicast and connects to any it finds, so the publisher attaches
to the router on its own.

**Still this computer.** Note its address on the network, in case you need
it below.

```sh
hostname -I
```

Take the first address, for example `192.168.1.79`.

**On the other computer.** Start the subscriber, exactly as in step 1.

```sh
python3 subscriber.py
```

> Note: if nothing arrives after a few seconds, your network is blocking
> multicast discovery. Give the subscriber the router's address instead,
> using the address you found above:
> `python3 subscriber.py --connect tcp/192.168.1.79:7447`

You should see the `received: hello N` stream on the other computer, at the
same 50 per second as in step 1, even though the publisher is here.

Try swapping the roles: run the publisher on the other computer and the
subscriber here. It works the same way, because both only talk to the router.

When you are done, stop the publisher with Ctrl-C and shut the router down:

```sh
docker compose down
```

---

## Step 5. PC publisher to the phone (sb + ROS2 messages)

Now the receiver is a phone. The program in `pubsub_flutter/` makes up sensor
data and publishes it over zenoh, and the zenoh_flutter app shows it as cards.
Two things are new compared to step 1:

- The messages are standard ROS2 types (`sensor_msgs/Imu`, `std_msgs/String`,
  ...) from the message vault of the `sb` tool, instead of a struct or a string
  we made up ourselves.
- The publisher code was generated by `sb`. You only build the message and
  call `publish`.

This step needs the `sb` tool installed, because the program reads the ROS2
message definitions from its vault in `~/.swarmbotix/`.

It publishes nine topics:

| Key | Rate | Message | What is in it |
|---|---|---|---|
| `elcom/ws_icp/pubsub_flutter/zenoh/imu` | 50 Hz | `sensor_msgs/Imu` | acceleration x and y swinging between -1 and 1, z = 9.806 |
| `elcom/ws_icp/pubsub_flutter/zenoh/temperature` | 50 Hz | `sensor_msgs/Temperature` | about 36.4 degrees, as FlatBuffers instead of protobuf |
| `elcom/ws_icp/pubsub_flutter/zenoh/chatter` | 2 Hz | `std_msgs/String` | `hello world 0`, `hello world 1`, ... |
| `elcom/ws_icp/pubsub_flutter/zenoh/cam` | 30 Hz | `sensor_msgs/CompressedImage` | a 320x240 JPEG with a moving yellow rectangle |
| `elcom/ws_icp/pubsub_flutter/zenoh/raw` | 5 Hz | `std_msgs/UInt8MultiArray` | 256 bytes counting up |
| `elcom/ws_icp/pubsub_flutter/zenoh/markers` | 20 Hz | `custom/MarkerDetections` | three AprilTags circling in front of camera `sb00`, with position, angle and distance; every 5 seconds a few frames with no tag |
| `elcom/ws_icp/pubsub_flutter/zenoh/cube_poses` | 20 Hz | `custom/CubePoses` | two cubes going round a 0.5 m circle; every 10 seconds cube 1 drops out (`alive` false) for 2 seconds |
| `elcom/ws_icp/pubsub_flutter/zenoh/image` | 10 Hz | `sensor_msgs/Image` | a 640x512 grey test picture with a moving white square and a frame counter, as raw pixels: encoding `mono8`, one byte per pixel (327680 bytes) |
| `elcom/ws_icp/pubsub_flutter/zenoh/debug_image` | 10 Hz | `sensor_msgs/Image` | the same picture in colour with three green boxes labelled `id 0` to `id 2`, like a marker detector's overlay: encoding `rgb8`, three bytes per pixel (983040 bytes) |

`markers` and `cube_poses` are not standard ROS2 types: they are the same
`ros2/custom/*` messages that the ws-ips pipeline publishes, so the app can
decode the real ones too.

`image` and `debug_image` are uncompressed, unlike the JPEG on `cam`: the
message says how to read its `data` bytes with `width`, `height`, `encoding`
and `step` (bytes per row, so `data` is `step` x `height` bytes). They are the
same message that the ws-ips image relay will publish (keys
`elcom/ws_ips/ips_image_relay-sbNN/zenoh/image` and `.../debug_image`), so the
app renders those too.

The key is built by `sb` from five parts: device (`elcom`, from
`~/.swarmbotix/sb.config.yml`), workspace (`ws_icp`), module
(`pubsub_flutter`), transport (`zenoh`) and topic. The app listens on exactly
these keys.

**Terminal 1.** Start the publisher, from the project root.

```sh
python3 pubsub_flutter/main.py
```

It prints the nine keys, each with the message type it is tagged with, such
as `application/protobuf;ros2/sensor_msgs/Imu`. That tag travels with every
message, so the receiver knows how to decode the bytes.

**Terminal 2.** Check that the topics are there.

```sh
sb topic list
```

You should see the nine `elcom/ws_icp/pubsub_flutter/zenoh/...` keys with their
message types (along with anything else running on your network). Then watch
one of them:

```sh
sb topic listen elcom/ws_icp/pubsub_flutter/zenoh/imu
```

One JSON line per message, 50 per second, with `linearAcceleration` changing
from line to line. Press Ctrl-C and try
`sb topic listen elcom/ws_icp/pubsub_flutter/zenoh/chatter`, which shows the
`hello world N` lines.

> If `sb topic list` takes a few seconds to return, or Ctrl-C takes up to
> 10 seconds, other zenoh programs on the network are slow to say goodbye.
> That is harmless.

**On the phone.** Connect it to the same Wi-Fi as this computer and open the
zenoh_flutter app. Like the publisher, it runs as a peer: the two find each
other by multicast, with no router. You should see the nine topics: the IMU
values moving, the temperature, the `hello world N` text, the camera picture
with its frame counter, the raw bytes, the three markers, the two cubes, and
the grey test picture with its colour debug version.

> Note: if the phone shows nothing, the Wi-Fi is blocking multicast (common on
> guest and campus networks). Use the router from step 4 instead. Start it with
> `docker compose up -d`, start the publisher with
> `python3 pubsub_flutter/main.py --connect tcp/localhost:7447`, and give the
> app the router's address, `tcp/<address from hostname -I>:7447`.

Stop the publisher with Ctrl-C.

Where the code comes from: inside `pubsub_flutter/`, `sb io list` shows the nine
publishers. Each was added with one command, such as

```sh
sb io add pub imu -m ros2/sensor_msgs/Imu --zenoh
```

which wrote `swarmbotix_io/publishers/imu.py`, the class that owns the key and
stamps the type tag. `main.py` imports those classes, fills in the ROS2
messages and publishes them on a fixed schedule.

---

## What you have seen

- Steps 1 and 4: zenoh moves bytes over the network, on one machine or many.
  Easy to set up, no agreement on data layout needed.
- Steps 2 and 3: iceoryx2 shares memory inside one machine. Much faster for
  big data like images, but both sides must agree on the exact data layout,
  and it stops at the edge of the computer.
- Step 5: with standard ROS2 messages and code generated by `sb`, a phone
  app decodes what a PC publishes, because every message carries its type.

If you want to go further with the router, `full-capability-example.md` shows
a fully configured one with TLS, storage and access control.
