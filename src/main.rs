use std::collections::HashSet;
use std::error::Error;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use image::{GrayImage, Luma, Rgba, RgbaImage, imageops};
use teloxide::net::Download;
use teloxide::prelude::*;
use teloxide::sugar::request::RequestReplyExt;
use teloxide::types::{FileMeta, InputFile};
use tokio::io::AsyncWriteExt;
use tokio::process::Command;
use tokio::sync::Mutex;

type Res<T> = Result<T, Box<dyn Error + Send + Sync>>;

const LOGO: &[u8] = include_bytes!("../logos/kult.png");
const SIZE: u32 = 640;
const LOGO_FRACTION: f64 = 0.24;
const MAX_DURATION: f64 = 60.0;
const MAX_BYTES: u64 = 20_000_000;
const VIDEO_EXTENSIONS: [&str; 6] = ["mp4", "mov", "mkv", "webm", "avi", "m4v"];
const HELP: &str = "Send a video or video note. I will return a video note with the Kultura logo \
    in the upper right, sized to 24% of the frame diameter.\n\n\
    You can also send a video as a file. The input limit is 20 MB. \
    Videos longer than one minute are trimmed to the first 60 seconds. \
    Rectangular videos are cropped to a square from the center.";

#[tokio::main]
async fn main() -> Res<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    dotenvy::dotenv().ok();
    for tool in ["ffmpeg", "ffprobe"] {
        std::process::Command::new(tool)
            .arg("-version")
            .output()
            .map_err(|_| format!("{tool} not found, install FFmpeg"))?;
    }

    if let Some(video) = std::env::args_os().nth(1) {
        let (video, dir) = (Path::new(&video), Path::new("output"));
        std::fs::create_dir_all(dir)?;
        let duration = probe(video).await?.min(MAX_DURATION);
        println!("{}", render(video, dir, duration).await?.display());
        return Ok(());
    }

    let token = std::env::var("TELEGRAM_TOKEN").unwrap_or_default();
    if token.trim().is_empty() {
        return Err("Add TELEGRAM_TOKEN to .env and start the bot again.".into());
    }
    let allowed: HashSet<u64> = std::env::var("ALLOWED_USER_IDS")
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .map(str::parse)
        .collect::<Result<_, _>>()
        .map_err(|_| "ALLOWED_USER_IDS must contain numeric IDs separated by commas.")?;
    let client = teloxide::net::default_reqwest_settings()
        .timeout(Duration::from_secs(120))
        .build()?;
    let bot = Bot::with_client(token.trim(), client);
    let (allowed, busy) = (Arc::new(allowed), Arc::new(Mutex::new(())));
    teloxide::repl(bot, move |bot: Bot, msg: Message| {
        handle(bot, msg, allowed.clone(), busy.clone())
    })
    .await;
    Ok(())
}

async fn handle(
    bot: Bot,
    msg: Message,
    allowed: Arc<HashSet<u64>>,
    busy: Arc<Mutex<()>>,
) -> ResponseResult<()> {
    let file = match video_or_reply(&msg, &allowed) {
        Ok(file) => file,
        Err(text) => {
            bot.send_message(msg.chat.id, text).reply_to(msg.id).await?;
            return Ok(());
        }
    };
    let Ok(_processing) = busy.try_lock() else {
        bot.send_message(
            msg.chat.id,
            "I am processing another video. Please send yours again shortly.",
        )
        .reply_to(msg.id)
        .await?;
        return Ok(());
    };
    let status = bot
        .send_message(msg.chat.id, "Preparing your video note with the logo…")
        .reply_to(msg.id)
        .await?;
    let text = match process(&bot, &msg, file).await {
        Ok(()) => "Done.",
        Err(error) => {
            log::error!("video processing failed for message {}: {error}", msg.id);
            "Could not process or send the video. Try an MP4 file under 20 MB. Error details are in the bot console."
        }
    };
    bot.edit_message_text(msg.chat.id, status.id, text).await?;
    Ok(())
}

fn video_or_reply<'a>(
    msg: &'a Message,
    allowed: &HashSet<u64>,
) -> Result<&'a FileMeta, &'static str> {
    if !allowed.is_empty()
        && !msg
            .from
            .as_ref()
            .is_some_and(|user| allowed.contains(&user.id.0))
    {
        return Err("This bot is only available to authorized users.");
    }
    let command = msg
        .text()
        .unwrap_or_default()
        .split([' ', '@'])
        .next()
        .unwrap_or_default();
    if command == "/start" || command == "/help" {
        return Err(HELP);
    }
    let file = if let Some(video) = msg.video() {
        &video.file
    } else if let Some(note) = msg.video_note() {
        &note.file
    } else if let Some(document) = msg.document() {
        let extension = document
            .file_name
            .as_deref()
            .and_then(|name| Path::new(name).extension()?.to_str())
            .map(str::to_ascii_lowercase);
        let is_video = document
            .mime_type
            .as_ref()
            .is_some_and(|mime| mime.type_() == "video")
            || extension.is_some_and(|ext| VIDEO_EXTENSIONS.contains(&ext.as_str()));
        if !is_video {
            return Err("Send a video, such as an MP4 file, or a video note.");
        }
        &document.file
    } else {
        return Err("Send a video or video note. Instructions: /help");
    };
    if u64::from(file.size) > MAX_BYTES {
        return Err("The file exceeds 20 MB. Send a shorter or compressed video.");
    }
    Ok(file)
}

async fn process(bot: &Bot, msg: &Message, file: &FileMeta) -> Res<()> {
    let dir = tempfile::Builder::new().prefix("kultura-").tempdir()?;
    let source = dir.path().join("input-video");
    let remote = bot.get_file(file.id.clone()).await?;
    let mut destination = tokio::fs::File::create(&source).await?;
    bot.download_file(&remote.path, &mut destination).await?;
    destination.flush().await?;
    if std::fs::metadata(&source)?.len() > MAX_BYTES {
        return Err("The input file exceeds 20 MB.".into());
    }
    let duration = probe(&source).await?;
    if duration > MAX_DURATION {
        bot.send_message(
            msg.chat.id,
            "I will use the first 60 seconds, the video note duration limit.",
        )
        .reply_to(msg.id)
        .await?;
    }
    let duration = duration.min(MAX_DURATION);
    let output = render(&source, dir.path(), duration).await?;
    bot.send_video_note(msg.chat.id, InputFile::file(output))
        .duration(duration.round().max(1.0) as u32)
        .length(SIZE)
        .reply_to(msg.id)
        .await?;
    Ok(())
}

async fn run(mut command: Command, seconds: u64) -> Res<String> {
    let output = tokio::time::timeout(
        Duration::from_secs(seconds),
        command.kill_on_drop(true).output(),
    )
    .await??;
    if !output.status.success() {
        let tail = &output.stderr[output.stderr.len().saturating_sub(3000)..];
        let program = command.as_std().get_program().to_string_lossy();
        return Err(format!("{program}: {}", String::from_utf8_lossy(tail)).into());
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

async fn probe(source: &Path) -> Res<f64> {
    let mut command = Command::new("ffprobe");
    command
        .args([
            "-v",
            "error",
            "-select_streams",
            "V:0",
            "-of",
            "default=nw=1",
        ])
        .args([
            "-show_entries",
            "stream=width,height,duration:format=duration",
        ])
        .arg(source);
    let (mut width, mut height, mut duration) = (0, 0, None);
    for (key, value) in run(command, 30)
        .await?
        .lines()
        .filter_map(|line| line.split_once('='))
    {
        match key {
            "width" => width = value.parse().unwrap_or(0),
            "height" => height = value.parse().unwrap_or(0),
            "duration" => duration = duration.or(value.parse::<f64>().ok()),
            _ => {}
        }
    }
    if width < 2 || height < 2 {
        return Err("No video stream found in the file.".into());
    }
    match duration {
        Some(duration) if duration.is_finite() && duration > 0.0 => Ok(duration),
        _ => Err("Could not determine the video duration.".into()),
    }
}

fn prepare_overlay(destination: &Path) -> Res<(u32, u32)> {
    let mut logo = image::load_from_memory(LOGO)?.into_rgba8();
    for pixel in logo.pixels_mut() {
        let alpha = u16::from(pixel[3]);
        for channel in &mut pixel.0[..3] {
            *channel = ((u16::from(*channel) * alpha + 127) / 255) as u8;
        }
    }
    let width = (f64::from(SIZE) * LOGO_FRACTION).round() as u32 * 2;
    let height =
        (f64::from(width) * f64::from(logo.height()) / f64::from(logo.width())).round() as u32;
    let mut logo = imageops::resize(&logo, width, height.max(1), imageops::FilterType::Lanczos3);
    for pixel in logo.pixels_mut() {
        let alpha = u16::from(pixel[3]);
        for channel in &mut pixel.0[..3] {
            if let Some(value) = (u16::from(*channel) * 255 + alpha / 2).checked_div(alpha) {
                *channel = value.min(255) as u8;
            }
        }
    }

    let alpha = |x: i64, y: i64| match (u32::try_from(x - 4), u32::try_from(y - 4)) {
        (Ok(x), Ok(y)) => logo.get_pixel_checked(x, y).map_or(0, |pixel| pixel[3]),
        _ => 0,
    };
    let dilated = GrayImage::from_fn(logo.width() + 8, logo.height() + 8, |x, y| {
        let mut max = 0;
        for dy in -2..=2 {
            for dx in -2..=2 {
                max = max.max(alpha(i64::from(x) + dx, i64::from(y) + dy));
            }
        }
        Luma([max])
    });
    let outline = imageops::blur(&dilated, 0.6);
    let mut overlay = RgbaImage::from_fn(outline.width(), outline.height(), |x, y| {
        Rgba([
            255,
            255,
            255,
            (f32::from(outline.get_pixel(x, y)[0]) * 0.85).round() as u8,
        ])
    });
    imageops::overlay(&mut overlay, &logo, 4, 4);
    overlay.save(destination)?;
    let frame = f64::from(SIZE * 2);
    Ok((
        (frame * 0.84).round() as u32 - overlay.width(),
        (frame * 0.18).round() as u32,
    ))
}

async fn render(source: &Path, dir: &Path, duration: f64) -> Res<PathBuf> {
    let overlay = dir.join("logo.png");
    let (x, y) = prepare_overlay(&overlay)?;
    let output = dir.join("kultura.mp4");
    let filters = format!(
        "[0:V:0]setpts=PTS-STARTPTS,\
         scale=1280:1280:force_original_aspect_ratio=increase:flags=lanczos,\
         crop=1280:1280,setsar=1,fps=30,format=yuv444p[base];\
         [1:v]format=rgba[logo];\
         [base][logo]overlay={x}:{y}:format=auto:eof_action=repeat:shortest=0,\
         scale=640:640:flags=lanczos,format=yuv420p[out]"
    );
    let duration = format!("{duration:.6}");
    let mut command = Command::new("ffmpeg");
    command
        .args(["-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i"])
        .arg(source)
        .arg("-i")
        .arg(&overlay)
        .args([
            "-filter_complex",
            &filters,
            "-map",
            "[out]",
            "-map",
            "0:a:0?",
            "-t",
            &duration,
        ])
        .args([
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-maxrate", "4M", "-bufsize", "8M",
        ])
        .args([
            "-threads", "2", "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
        ])
        .args([
            "-af",
            "asetpts=PTS-STARTPTS",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
        ])
        .arg(&output);
    run(command, 300).await?;
    if std::fs::metadata(&output)?.len() > 49_000_000 {
        return Err("The output file is too large to send.".into());
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ff(args: &[&str]) -> Vec<u8> {
        let output = std::process::Command::new(args[0])
            .args(&args[1..])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        output.stdout
    }

    fn streams(path: &Path) -> String {
        let entries =
            "stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate:format=duration";
        let path = path.to_str().unwrap();
        String::from_utf8(ff(&[
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            entries,
            "-of",
            "compact",
            path,
        ]))
        .unwrap()
    }

    fn message(user: u64, size: u32) -> Message {
        serde_json::from_value(serde_json::json!({
            "message_id": 1, "date": 0,
            "chat": {"id": user, "type": "private", "first_name": "a"},
            "from": {"id": user, "is_bot": false, "first_name": "a"},
            "video": {"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1, "duration": 1, "mime_type": "video/mp4", "file_size": size},
        }))
        .unwrap()
    }

    #[test]
    fn gates_users_and_sizes() {
        let allowed = HashSet::from([123]);
        assert!(video_or_reply(&message(123, 100), &allowed).is_ok());
        assert!(
            video_or_reply(&message(999, 100), &allowed)
                .unwrap_err()
                .contains("authorized")
        );
        assert!(
            video_or_reply(&message(123, 20_000_001), &allowed)
                .unwrap_err()
                .contains("20 MB")
        );
    }

    #[tokio::test]
    async fn renders_square_note_with_logo_and_audio() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("square.mp4");
        ff(&[
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x640:rate=30:duration=1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            source.to_str().unwrap(),
        ]);
        let output = render(&source, dir.path(), 0.7).await.unwrap();

        let info = streams(&output);
        for want in [
            "codec_name=h264",
            "pix_fmt=yuv420p",
            "width=640",
            "height=640",
            "avg_frame_rate=30/1",
            "codec_name=aac",
        ] {
            assert!(info.contains(want), "{want} missing from {info}");
        }
        let duration: f64 = info
            .lines()
            .find_map(|line| line.strip_prefix("format|duration="))
            .unwrap()
            .parse()
            .unwrap();
        assert!((0.65..=0.8).contains(&duration));
        let bytes = std::fs::read(&output).unwrap();
        let offset = |tag: &[u8]| bytes.windows(4).position(|window| window == tag).unwrap();
        assert!(offset(b"moov") < offset(b"mdat"));

        let logo_region = |path: &Path| {
            let raw = ff(&[
                "ffmpeg",
                "-v",
                "error",
                "-i",
                path.to_str().unwrap(),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ]);
            let frame = image::RgbImage::from_raw(SIZE, SIZE, raw).unwrap();
            imageops::crop_imm(&frame, 282, 115, 256, 77)
                .to_image()
                .into_raw()
        };
        let changed: u64 = logo_region(&output)
            .iter()
            .zip(logo_region(&source))
            .map(|(a, b)| u64::from(a.abs_diff(b)))
            .sum();
        assert!(changed > 50_000);
    }

    #[tokio::test]
    async fn crops_rectangular_silent_input_to_square() {
        for size in ["960x540", "360x640"] {
            let dir = tempfile::tempdir().unwrap();
            let source = dir.path().join("silent.mp4");
            let input = format!("color=c=gray:size={size}:rate=15:duration=0.4");
            ff(&[
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                &input,
                "-c:v",
                "libx264",
                source.to_str().unwrap(),
            ]);
            let output = render(&source, dir.path(), probe(&source).await.unwrap())
                .await
                .unwrap();
            let info = streams(&output);
            assert!(
                info.contains("width=640") && info.contains("height=640"),
                "{info}"
            );
            assert!(!info.contains("codec_type=audio"));
        }
    }

    #[test]
    fn overlay_is_antialiased_and_inside_circle() {
        let logo = image::load_from_memory(LOGO).unwrap().into_rgba8();
        assert_eq!(
            (logo.get_pixel(10, 10)[3], logo.get_pixel(20, 10)[3]),
            (0, 0)
        );
        assert!(logo.get_pixel(700, 30)[3] > 200);

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("logo.png");
        let (x, y) = prepare_overlay(&path).unwrap();
        let overlay = image::open(&path).unwrap().into_rgba8();
        let (w, h, center) = (overlay.width(), overlay.height(), f64::from(SIZE));
        for (px, py) in [(x, y), (x + w, y), (x, y + h), (x + w, y + h)] {
            assert!((f64::from(px) - center).hypot(f64::from(py) - center) <= center * 0.96);
        }
        assert!(overlay.pixels().any(|pixel| (1..255).contains(&pixel[3])));
        assert_eq!(overlay.get_pixel(0, 0)[3], 0);
    }

    #[tokio::test]
    async fn rejects_non_video() {
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), "not a video").unwrap();
        assert!(probe(file.path()).await.is_err());
    }
}
