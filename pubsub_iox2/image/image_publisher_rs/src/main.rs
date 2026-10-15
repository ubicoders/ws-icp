//! iceoryx2 image publisher for the ws-icp workshop.
//!
//! Draws a synthetic 1280x720 BGR frame with OpenCV (background color that
//! changes with seq, a moving yellow rectangle, a "frame N" label and the
//! timestamp) and publishes it at 100 Hz on the iceoryx2 publish-subscribe
//! service `ws-icp/image`. It is the iceoryx2/Rust port of the test/cam part of
//! zenoh_flutter/tools/test_publisher.py.
//!
//! Wire contract (shared with pubsub_iox2/image_subscriber.py, which mirrors it
//! as a ctypes.Structure named `ImageFrame`):
//!   service `ws-icp/image`, publish_subscribe, fixed-size payload, no user header,
//!   payload type name "ImageFrame", size 2_764_832 bytes, alignment 8.
//!
//! Run:  cargo run --release            (Ctrl-C to stop)
//!       cargo run --release -- --show  (also show the published frame locally;
//!                                       or set IMAGE_PUBLISHER_SHOW=1)

use core::fmt;
use core::ptr::{addr_of_mut, copy_nonoverlapping};
use core::time::Duration;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use iceoryx2::prelude::*;

const SERVICE_NAME: &str = "ws-icp/image";
const WIDTH: usize = 1280;
const HEIGHT: usize = 720;
const CHANNELS: usize = 3;
const DATA_LEN: usize = WIDTH * HEIGHT * CHANNELS;
/// `ImageFrame::format` value for row-major, unpadded BGR, 8 bit per channel (OpenCV native).
const FORMAT_BGR8: u32 = 0;
const RATE_HZ: f64 = 100.0;

/// The payload. The layout is the wire contract: the Python subscriber reads
/// it through a ctypes.Structure with the same fields in the same order.
#[repr(C)]
#[derive(ZeroCopySend)]
#[type_name("ImageFrame")]
pub struct ImageFrame {
    /// UNIX time in ns at which the frame was drawn.
    pub timestamp_ns: u64,
    /// 0 for the first published frame, +1 per published frame.
    pub seq: u64,
    pub width: u32,
    pub height: u32,
    pub channels: u32,
    /// 0 = bgr8.
    pub format: u32,
    /// Row-major BGR pixels, no row padding.
    pub data: [u8; DATA_LEN],
}

const _: () = assert!(core::mem::size_of::<ImageFrame>() == 2_764_832);
const _: () = assert!(core::mem::align_of::<ImageFrame>() == 8);

// iceoryx2 requires `Debug` on the payload; print the header only, not 2.7 MB of pixels.
impl fmt::Debug for ImageFrame {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ImageFrame")
            .field("timestamp_ns", &self.timestamp_ns)
            .field("seq", &self.seq)
            .field("width", &self.width)
            .field("height", &self.height)
            .field("channels", &self.channels)
            .field("format", &self.format)
            .finish_non_exhaustive()
    }
}

/// OpenCV side: draws the frame into one heap-allocated, reused CV_8UC3 Mat.
mod canvas {
    use super::{CHANNELS, DATA_LEN, HEIGHT, WIDTH};
    use opencv::core::{CV_8UC3, Mat, Point, Rect, Scalar};
    use opencv::prelude::*;
    use opencv::{Error, Result, highgui, imgproc};

    const PREVIEW_WINDOW: &str = "image_publisher_rs: ws-icp/image";

    pub struct Canvas {
        mat: Mat,
        show: bool,
    }

    impl Canvas {
        pub fn new(show: bool) -> Result<Self> {
            let mat = Mat::new_rows_cols_with_default(
                HEIGHT as i32,
                WIDTH as i32,
                CV_8UC3,
                Scalar::all(0.0),
            )?;
            // The copy into the sample relies on this exact, unpadded layout.
            let ok = mat.is_continuous()
                && mat.typ() == CV_8UC3
                && mat.rows() == HEIGHT as i32
                && mat.cols() == WIDTH as i32
                && mat.channels() == CHANNELS as i32
                && mat.data_bytes()?.len() == DATA_LEN;
            if !ok {
                return Err(Error::new(
                    opencv::core::StsBadArg,
                    "canvas Mat is not a continuous 1280x720 CV_8UC3 image",
                ));
            }
            if show {
                highgui::named_window(
                    PREVIEW_WINDOW,
                    highgui::WINDOW_NORMAL | highgui::WINDOW_KEEPRATIO,
                )?;
                highgui::resize_window(PREVIEW_WINDOW, WIDTH as i32, HEIGHT as i32)?;
            }
            Ok(Self { mat, show })
        }

        /// Draws frame `seq` stamped with `timestamp_ns` (same picture as
        /// test_publisher.py's make_jpeg, scaled from 320x240 to 1280x720).
        pub fn render(&mut self, seq: u64, timestamp_ns: u64) -> Result<()> {
            // Background: RGB (20 + 3n % 200, 60, 120), written as BGR.
            let red = 20.0 + ((seq * 3) % 200) as f64;
            self.mat.set_to_def(&Scalar::new(120.0, 60.0, red, 0.0))?;

            // Moving yellow rectangle, RGB (240, 200, 40) -> BGR (40, 200, 240).
            let x = 80 + ((seq * 20) % 1000) as i32;
            imgproc::rectangle(
                &mut self.mat,
                Rect::new(x, 240, 200, 240),
                Scalar::new(40.0, 200.0, 240.0, 0.0),
                imgproc::FILLED,
                imgproc::LINE_8,
                0,
            )?;

            let white = Scalar::new(255.0, 255.0, 255.0, 0.0);
            imgproc::put_text(
                &mut self.mat,
                &format!("frame {seq}"),
                Point::new(40, 90),
                imgproc::FONT_HERSHEY_SIMPLEX,
                2.0,
                white,
                4,
                imgproc::LINE_AA,
                false,
            )?;
            imgproc::put_text(
                &mut self.mat,
                &format!("timestamp_ns {timestamp_ns}"),
                Point::new(40, 680),
                imgproc::FONT_HERSHEY_SIMPLEX,
                1.2,
                white,
                2,
                imgproc::LINE_AA,
                false,
            )?;
            Ok(())
        }

        /// The drawn pixels: row-major BGR, exactly DATA_LEN bytes.
        pub fn bytes(&self) -> Result<&[u8]> {
            self.mat.data_bytes()
        }

        /// Shows the frame when --show is on. Returns false when the user
        /// pressed Esc or q in the preview window.
        pub fn show(&self) -> Result<bool> {
            if !self.show {
                return Ok(true);
            }
            highgui::imshow(PREVIEW_WINDOW, &self.mat)?;
            let key = highgui::wait_key(1)?;
            Ok(key != 27 && key != 'q' as i32)
        }
    }
}

fn unix_time_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

fn show_requested() -> bool {
    let env_on = std::env::var("IMAGE_PUBLISHER_SHOW")
        .map(|v| matches!(v.as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false);
    env_on || std::env::args().skip(1).any(|a| a == "--show")
}

/// A terminal opened inside a snap-packaged app (VS Code from snap, for
/// example) exports GTK_PATH, GIO_MODULE_DIR, LOCPATH, ... that point into
/// /snap/<app>/. When highgui brings up its Qt/GTK window, those modules get
/// loaded against the snap's own glibc and the process dies with
/// "symbol lookup error: /snap/core20/.../libpthread.so.0: undefined symbol
/// __libc_pthread_init". Only the GUI path needs them gone, and the vars are
/// meaningless outside the snap, so drop any that point into /snap/.
fn drop_snap_gui_env() {
    const VARS: [&str; 5] = [
        "GTK_PATH",
        "GTK_EXE_PREFIX",
        "GIO_MODULE_DIR",
        "GSETTINGS_SCHEMA_DIR",
        "LOCPATH",
    ];
    for var in VARS {
        if std::env::var_os(var).is_some_and(|v| v.to_string_lossy().contains("/snap/")) {
            // SAFETY: called from main before any other thread exists.
            unsafe { std::env::remove_var(var) };
        }
    }
}

fn main() -> Result<(), Box<dyn core::error::Error>> {
    if std::env::args().skip(1).any(|a| a == "-h" || a == "--help") {
        println!("usage: image_publisher_rs [--show]   (or IMAGE_PUBLISHER_SHOW=1)");
        println!("publishes 1280x720 bgr8 ImageFrame samples at {RATE_HZ} Hz on iceoryx2 service {SERVICE_NAME}");
        return Ok(());
    }
    let show = show_requested();
    if show {
        drop_snap_gui_env();
    }

    let node = NodeBuilder::new()
        .name(&NodeName::new("image_publisher_rs")?)
        .create::<ipc::Service>()?;
    let service_name = ServiceName::new(SERVICE_NAME)?;

    // 2.7 MB per sample: keep the buffers small so the shared-memory data
    // segment stays bounded. A late subscriber gets the latest frame (history 1).
    // If the subscriber already created the service with smaller QoS values,
    // opening with these requirements fails; then open it as it is, since the
    // type (name, size, alignment) is what has to match.
    let service = match node
        .service_builder(&service_name)
        .publish_subscribe::<ImageFrame>()
        .history_size(1)
        .subscriber_max_buffer_size(2)
        .open_or_create()
    {
        Ok(service) => service,
        Err(e) => {
            eprintln!("note: {SERVICE_NAME} exists with other QoS settings ({e:?}); opening it as it is");
            node.service_builder(&service_name)
                .publish_subscribe::<ImageFrame>()
                .open_or_create()?
        }
    };
    let publisher = service.publisher_builder().create()?;

    let mut canvas = canvas::Canvas::new(show)?;

    println!(
        "publishing ImageFrame on iceoryx2 service '{SERVICE_NAME}': {WIDTH}x{HEIGHT} bgr8, {} bytes/frame, {RATE_HZ} Hz{} (Ctrl-C to stop)",
        core::mem::size_of::<ImageFrame>(),
        if show { ", local preview on" } else { "" },
    );

    // Same scheduler as test_publisher.py: the next due time moves on by one
    // period from the old due time, so slow ticks do not lower the average
    // rate, and after a long stall it resyncs instead of bursting to catch up.
    let period = Duration::from_secs_f64(1.0 / RATE_HZ);
    let mut next_due = Instant::now();
    let mut seq: u64 = 0;
    let mut stat_start = Instant::now();
    let mut stat_frames: u32 = 0;
    let mut subscribers: usize = 0;

    // node.wait() returns an error on SIGINT/SIGTERM, which ends the loop.
    while node
        .wait(next_due.saturating_duration_since(Instant::now()))
        .is_ok()
    {
        let timestamp_ns = unix_time_ns();
        canvas.render(seq, timestamp_ns)?;
        let pixels = canvas.bytes()?;
        assert_eq!(pixels.len(), DATA_LEN);

        // Write the header fields and copy the pixels straight into the
        // loaned shared-memory sample; the frame never lives on the stack.
        match publisher.loan_uninit() {
            Ok(mut sample) => {
                let frame: *mut ImageFrame = sample.payload_mut().as_mut_ptr();
                // SAFETY: `frame` points to a properly aligned, writable
                // ImageFrame-sized chunk owned by `sample`; every field is
                // written below (and the struct has no padding), so the payload
                // is fully initialized before `assume_init`.
                let sample = unsafe {
                    addr_of_mut!((*frame).timestamp_ns).write(timestamp_ns);
                    addr_of_mut!((*frame).seq).write(seq);
                    addr_of_mut!((*frame).width).write(WIDTH as u32);
                    addr_of_mut!((*frame).height).write(HEIGHT as u32);
                    addr_of_mut!((*frame).channels).write(CHANNELS as u32);
                    addr_of_mut!((*frame).format).write(FORMAT_BGR8);
                    copy_nonoverlapping(
                        pixels.as_ptr(),
                        addr_of_mut!((*frame).data).cast::<u8>(),
                        DATA_LEN,
                    );
                    sample.assume_init()
                };
                match sample.send() {
                    Ok(n) => {
                        subscribers = n;
                        seq += 1;
                        stat_frames += 1;
                    }
                    Err(e) => eprintln!("send of frame {seq} failed: {e:?}"),
                }
            }
            Err(e) => eprintln!("loan for frame {seq} failed: {e:?}"),
        }

        if !canvas.show()? {
            break;
        }

        let elapsed = stat_start.elapsed();
        if elapsed >= Duration::from_secs(1) {
            println!(
                "seq {} | {:.1} fps | {} subscriber(s)",
                seq.saturating_sub(1),
                stat_frames as f64 / elapsed.as_secs_f64(),
                subscribers,
            );
            stat_start = Instant::now();
            stat_frames = 0;
        }

        next_due += period;
        let now = Instant::now();
        if now.saturating_duration_since(next_due) > Duration::from_secs(1) {
            next_due = now;
        }
    }

    println!("stopped after {seq} frames");
    Ok(())
}
