# Lab: pub/sub with zenoh and iceoryx2

In this lab you will send messages between two programs in four different ways.
Each step builds on the one before, so go in order. Every step uses two
terminals: one for the publisher (the sender) and one for the subscriber
(the receiver).

Before you start, install the Python packages once:

```sh
pip install -r pubsub_zenoh/requirements.txt
pip install -r pubsub_iox2/requirements.txt
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

## What you have seen

- Steps 1 and 4: zenoh moves bytes over the network, on one machine or many.
  Easy to set up, no agreement on data layout needed.
- Steps 2 and 3: iceoryx2 shares memory inside one machine. Much faster for
  big data like images, but both sides must agree on the exact data layout,
  and it stops at the edge of the computer.

If you want to go further with the router, `full-capability-example.md` shows
a fully configured one with TLS, storage and access control.
