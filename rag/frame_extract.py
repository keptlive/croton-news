"""
Frame extraction, cropping, upscaling & sharpening pipeline for croton.news.

Extracts frames from meeting videos, crops to regions of interest,
upscales via Replicate API (Real-ESRGAN / GFPGAN), and sharpens.

Usage:
    # Extract + crop a frame
    python3 frame_extract.py 1143 300 --crop board
    python3 frame_extract.py 1143 300 --crop audience
    python3 frame_extract.py 1143 300 --crop document
    python3 frame_extract.py 1143 300 --crop custom --box 100,50,400,350

    # Extract + detect layout + show all crops
    python3 frame_extract.py 1143 300 --preview

    # Extract + crop + upscale (requires REPLICATE_API_TOKEN)
    python3 frame_extract.py 1143 300 --crop board --upscale
    python3 frame_extract.py 1129 300 --crop face --upscale

    # Extract + crop + upscale + sharpen
    python3 frame_extract.py 1143 300 --crop board --upscale --sharpen

    # Batch: extract speaker frames from transcript timestamps
    python3 frame_extract.py 1143 --speakers --upscale

    # List available videos
    python3 frame_extract.py --list
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from io import BytesIO

try:
    from PIL import Image, ImageFilter, ImageEnhance
except ImportError:
    if __name__ == "__main__":
        print("Pillow required: pip install Pillow")
        sys.exit(1)
    else:
        raise


# ── Paths ────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEOS_DIR = os.environ.get("VIDEOS_DIR", os.path.join(SCRIPT_DIR, "..", "site", "videos"))
# VPS path fallback
if not os.path.isdir(VIDEOS_DIR):
    VIDEOS_DIR = "/opt/croton-news/videos"
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "frames")
TRANSCRIPTS_DIR = os.path.join(SCRIPT_DIR, "transcripts")

REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")


# ── Layout Detection ─────────────────────────────────────────────────

# Known quad-split layout for Board of Trustees work sessions
# Top half: board dais, bottom-left: audience, bottom-right: document
QUAD_SPLIT = {
    "board":    (0, 0, 1280, 360),     # top half — full board panel
    "audience": (0, 360, 640, 720),     # bottom-left — audience/speakers
    "document": (640, 360, 1280, 720),  # bottom-right — screen share
    # Individual board positions (Board of Trustees quad-split, left to right)
    # Seat 1: empty railing area — skip
    "seat_2": (120, 20, 380, 350),     # left trustee (woman with braids)
    "seat_3": (300, 20, 530, 350),     # Village Manager Bryan Healy
    "seat_4": (470, 20, 730, 350),     # Mayor Brian Pugh (center, behind seal)
    "seat_5": (870, 20, 1100, 350),    # trustee (gray jacket)
    "seat_6": (1020, 20, 1280, 350),   # trustee (far right, woman)
}

# Podium speaker view — camera zoomed on public speaker at podium
# Speaker is right-center, podium with village seal visible
PODIUM_VIEW = {
    "podium":   (520, 20, 1060, 600),   # speaker at podium (540x580)
    "speaker":  (560, 30, 1000, 420),    # tighter face/upper body (440x390)
    "audience": (0, 50, 550, 650),       # audience visible to left
    "full":     (0, 0, 1280, 720),
}

# Single camera — full frame is already the shot
SINGLE_CAM = {
    "full": (0, 0, 1280, 720),
    "face": None,  # auto-detect via center crop
}


def _region_color(img, box):
    """Get average RGB of a region."""
    region = img.crop(box).resize((16, 16)).convert("RGB")
    raw = list(region.tobytes())
    pixels = [(raw[i], raw[i+1], raw[i+2]) for i in range(0, len(raw), 3)]
    return tuple(int(sum(c) / len(pixels)) for c in zip(*[p[:3] for p in pixels]))


def detect_layout(img):
    """Detect frame layout: quad-split, podium, closeup, or wide.

    Uses heuristics based on color regions and known visual anchors.
    """
    w, h = img.size

    # Check if bottom-right quadrant looks like a document (high brightness = quad split)
    br_quad = img.crop((w//2, h//2, w, h))
    br_small = br_quad.resize((16, 16)).convert("RGB")
    br_raw = list(br_small.tobytes())
    br_pixels = [(br_raw[i], br_raw[i+1], br_raw[i+2]) for i in range(0, len(br_raw), 3)]
    br_brightness = sum(sum(p[:3]) / 3 for p in br_pixels) / len(br_pixels)

    # Top vs bottom color difference
    top_avg = _region_color(img, (0, 0, w, h//2))
    bot_avg = _region_color(img, (0, h//2, w, h))
    half_diff = sum(abs(a - b) for a, b in zip(top_avg, bot_avg))

    # Check for quad-split: bright document pane in bottom-right + distinct top/bottom
    if half_diff > 60 and br_brightness > 160:
        return "quad"

    # Check for podium view: village seal is a blue circle in the lower-right area
    # The seal region (~600-800 x, ~450-650 y) has strong blue component
    seal_area = _region_color(img, (600, 450, 820, 660))
    # Seal is blue on white: high blue channel, relatively lower red/green
    blue_dominance = seal_area[2] - (seal_area[0] + seal_area[1]) / 2
    # Also check: right side of frame has a person (skin tones / clothing)
    # and left side has audience (darker, chairs)
    left_avg = _region_color(img, (0, 100, 300, 500))
    right_avg = _region_color(img, (600, 50, 1100, 400))

    if blue_dominance > 15:
        return "podium"

    # Close-up: very uniform scene (wood paneling, single person)
    if half_diff < 30:
        return "closeup"

    return "wide"


def auto_face_crop(img, padding=0.3):
    """Simple center-weighted face crop for close-up shots.

    Assumes the speaker is roughly centered in the frame.
    Returns a square-ish crop around the center-upper region.
    """
    w, h = img.size
    # Face is usually in upper-center for close-ups
    face_w = int(w * 0.5)
    face_h = int(h * 0.7)
    x1 = (w - face_w) // 2
    y1 = int(h * 0.05)
    x2 = x1 + face_w
    y2 = y1 + face_h
    # Add padding
    pad_x = int(face_w * padding)
    pad_y = int(face_h * padding)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)
    return (x1, y1, x2, y2)


# ── Frame Extraction ─────────────────────────────────────────────────

def extract_frame(video_path, timestamp, output_path=None):
    """Extract a single frame from video at given timestamp (seconds)."""
    if output_path is None:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        vid_name = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(OUTPUT_DIR, f"{vid_name}_t{timestamp}.png")

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "1",  # highest quality
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def crop_frame(img, box):
    """Crop image to box (x1, y1, x2, y2)."""
    return img.crop(box)


# ── Upscaling via Replicate API ──────────────────────────────────────

def _replicate_predict(version, input_data, label=""):
    """Run a Replicate prediction, wait for result, return output URL."""
    payload = json.dumps({"version": version, "input": input_data}).encode()
    req = urllib.request.Request(
        "https://api.replicate.com/v1/predictions",
        data=payload,
        headers={
            "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
            "Content-Type": "application/json",
            "Prefer": "wait",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)
        result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  ! Replicate error {e.code}: {e.read().decode()[:200]}")
        return None

    status = result.get("status")
    output = result.get("output")
    poll_url = result.get("urls", {}).get("get")

    if status == "succeeded" and output:
        return output[0] if isinstance(output, list) else output

    if not poll_url:
        return None

    for _ in range(90):
        time.sleep(3)
        poll_req = urllib.request.Request(poll_url, headers={
            "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        })
        result = json.loads(urllib.request.urlopen(poll_req, timeout=30).read())
        status = result.get("status")
        if status == "succeeded":
            output = result.get("output")
            return output[0] if isinstance(output, list) else output
        elif status == "failed":
            print(f"  ! {label} failed: {result.get('error', '')[:200]}")
            return None
        print(f"  ... {label} {status}")

    print(f"  ! {label} timed out")
    return None


def _img_to_data_uri(img):
    """Convert PIL Image to data URI."""
    buf = BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"


def _download_image(url):
    """Download image from URL, return PIL Image."""
    resp = urllib.request.urlopen(url, timeout=60)
    return Image.open(BytesIO(resp.read()))


def upscale_replicate(img, model="auto", scale=2, layout=None):
    """Upscale image using the best Replicate workflow for the content type.

    Workflows (tested, ranked by quality):
        topaz       — Topaz Low Resolution V2 + face enhance (~$0.03)
                      Best for: speakers, faces, close-ups
        esrgan      — Real-ESRGAN 2x + GFPGAN face enhance (~$0.002)
                      Best for: board shots, wide scenes, group photos
        auto        — Picks topaz for podium/closeup, esrgan for quad/wide

    Returns PIL Image.
    """
    if not REPLICATE_API_TOKEN:
        print("  ! REPLICATE_API_TOKEN not set, skipping upscale")
        return img

    # Auto-select based on layout
    if model == "auto":
        if layout in ("podium", "closeup"):
            model = "topaz"
        else:
            model = "esrgan"

    data_uri = _img_to_data_uri(img)

    if model == "topaz":
        print(f"  Upscaling with Topaz Low Res V2 ({scale}x)...")
        version = "2fdc3b86a01d338ae89ad58e5d9241398a8a01de9b0dda41ba8a0434c8a00dc3"
        url = _replicate_predict(version, {
            "image": data_uri,
            "enhance_model": "Low Resolution V2",
            "upscale_factor": f"{scale}x",
            "face_enhancement": True,
            "face_enhancement_creativity": 0,
            "output_format": "png",
        }, "topaz")

    elif model == "esrgan":
        print(f"  Upscaling with Real-ESRGAN + GFPGAN ({scale}x)...")
        version = "f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa"
        url = _replicate_predict(version, {
            "image": data_uri,
            "scale": scale,
            "face_enhance": True,
        }, "esrgan")

    else:
        print(f"  ! Unknown model '{model}', using esrgan")
        return upscale_replicate(img, model="esrgan", scale=scale, layout=layout)

    if url:
        print(f"  Downloading result...")
        return _download_image(url)

    print("  ! Upscale failed, returning original")
    return img


# ── Sharpening ───────────────────────────────────────────────────────

def sharpen(img, amount=1.5):
    """Sharpen image using unsharp mask.

    amount: 1.0 = subtle, 2.0 = strong, 3.0 = aggressive
    """
    # Unsharp mask: radius=2, percent=amount*100, threshold=3
    sharpened = img.filter(ImageFilter.UnsharpMask(radius=2, percent=int(amount * 100), threshold=3))
    # Slight contrast boost
    enhancer = ImageEnhance.Contrast(sharpened)
    return enhancer.enhance(1.05)


# ── Speaker Frame Extraction ─────────────────────────────────────────

def extract_speaker_frames(event_id, upscale=False, upscale_model="real-esrgan", sharpen_amount=1.5):
    """Extract one frame per speaker from a meeting transcript.

    Uses speaker_map and first utterance timestamps to get a frame
    of each unique speaker.
    """
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f"transcript-{event_id}.json")
    if not os.path.exists(transcript_path):
        print(f"No transcript for event {event_id}")
        return []

    with open(transcript_path) as f:
        transcript = json.load(f)

    speaker_map = transcript.get("speaker_map", {})
    utterances = transcript.get("utterances", [])

    # Find first timestamp for each speaker
    speaker_times = {}
    for u in utterances:
        speaker = u.get("speaker", "")
        if speaker not in speaker_times:
            start = u.get("start", 0)
            # Skip first 5 seconds of each speaker's first utterance (they may be settling in)
            speaker_times[speaker] = start + 2

    video_path = os.path.join(VIDEOS_DIR, f"{event_id}.mp4")
    if not os.path.exists(video_path):
        print(f"No video for event {event_id}")
        return []

    results = []
    for speaker_key, timestamp in speaker_times.items():
        name = speaker_map.get(speaker_key, speaker_key)
        safe_name = name.replace(" ", "_").replace("/", "_")
        print(f"\n{'='*60}")
        print(f"Speaker: {name} (t={timestamp}s)")

        # Extract frame
        frame_path = extract_frame(video_path, timestamp)
        img = Image.open(frame_path)

        # Detect layout and crop
        layout = detect_layout(img)
        print(f"  Layout: {layout}")

        if layout == "podium":
            cropped = crop_frame(img, PODIUM_VIEW["podium"])
        elif layout == "closeup":
            box = auto_face_crop(img)
            cropped = crop_frame(img, box)
        elif layout == "quad":
            cropped = crop_frame(img, QUAD_SPLIT["board"])
        else:
            cropped = img  # wide shot — keep full frame

        # Save crop
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        crop_path = os.path.join(OUTPUT_DIR, f"{event_id}_{safe_name}.png")
        cropped.save(crop_path)
        print(f"  Cropped: {cropped.size[0]}x{cropped.size[1]} → {crop_path}")

        # Upscale
        if upscale:
            cropped = upscale_replicate(cropped, model=upscale_model, scale=2, layout=layout)

        # Sharpen
        if sharpen_amount > 0:
            cropped = sharpen(cropped, sharpen_amount)

        # Save final
        final_path = os.path.join(OUTPUT_DIR, f"{event_id}_{safe_name}_final.png")
        cropped.save(final_path, quality=95)
        print(f"  Final: {cropped.size[0]}x{cropped.size[1]} → {final_path}")
        results.append({"speaker": name, "timestamp": timestamp, "path": final_path})

    return results


# ── Preview Mode ─────────────────────────────────────────────────────

def preview_crops(video_path, timestamp):
    """Extract frame and save all possible crops for preview."""
    frame_path = extract_frame(video_path, timestamp)
    img = Image.open(frame_path)
    layout = detect_layout(img)

    vid_name = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"Frame: {img.size[0]}x{img.size[1]}")
    print(f"Layout detected: {layout}")
    print(f"Full frame: {frame_path}")

    if layout == "quad":
        regions = QUAD_SPLIT
    elif layout == "podium":
        regions = PODIUM_VIEW
    elif layout == "closeup":
        regions = {"full": (0, 0, img.size[0], img.size[1]), "face": auto_face_crop(img)}
    else:
        regions = {"full": (0, 0, img.size[0], img.size[1])}

    for name, box in regions.items():
        if box is None:
            box = auto_face_crop(img)
        cropped = crop_frame(img, box)
        out = os.path.join(OUTPUT_DIR, f"{vid_name}_t{timestamp}_{name}.png")
        cropped.save(out)
        print(f"  {name}: {cropped.size[0]}x{cropped.size[1]} → {out}")


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract, crop, upscale & sharpen meeting video frames")
    parser.add_argument("event_id", nargs="?", help="Event ID (video filename without .mp4)")
    parser.add_argument("timestamp", nargs="?", type=int, help="Timestamp in seconds")
    parser.add_argument("--crop", choices=["board", "audience", "document", "podium", "speaker",
                                           "face", "full",
                                           "seat_2", "seat_3", "seat_4", "seat_5", "seat_6",
                                           "custom"],
                        default="full", help="Crop region")
    parser.add_argument("--box", help="Custom crop box: x1,y1,x2,y2")
    parser.add_argument("--upscale", action="store_true", help="Upscale via Replicate API")
    parser.add_argument("--upscale-model", default="auto",
                        choices=["auto", "topaz", "esrgan"],
                        help="Upscaling model (auto picks topaz for faces, esrgan for scenes)")
    parser.add_argument("--scale", type=int, default=4, help="Upscale factor (2 or 4)")
    parser.add_argument("--sharpen", action="store_true", help="Apply sharpening")
    parser.add_argument("--sharpen-amount", type=float, default=1.5, help="Sharpen strength (1.0-3.0)")
    parser.add_argument("--preview", action="store_true", help="Save all crop regions for preview")
    parser.add_argument("--speakers", action="store_true", help="Extract one frame per speaker")
    parser.add_argument("--list", action="store_true", help="List available videos")
    parser.add_argument("--output", "-o", help="Output path (default: frames/<event>_t<time>_<crop>.png)")

    args = parser.parse_args()

    if args.list:
        if os.path.isdir(VIDEOS_DIR):
            videos = sorted(f for f in os.listdir(VIDEOS_DIR) if f.endswith(".mp4"))
            print(f"Videos in {VIDEOS_DIR}:")
            for v in videos:
                size_mb = os.path.getsize(os.path.join(VIDEOS_DIR, v)) / (1024 * 1024)
                print(f"  {v} ({size_mb:.0f} MB)")
        else:
            print(f"Videos directory not found: {VIDEOS_DIR}")
        return

    if not args.event_id:
        parser.print_help()
        return

    video_path = os.path.join(VIDEOS_DIR, f"{args.event_id}.mp4")
    if not os.path.exists(video_path):
        print(f"Video not found: {video_path}")
        return

    # Speaker extraction mode
    if args.speakers:
        results = extract_speaker_frames(
            args.event_id,
            upscale=args.upscale,
            upscale_model=args.upscale_model,
            sharpen_amount=args.sharpen_amount if args.sharpen else 0,
        )
        print(f"\n{'='*60}")
        print(f"Extracted {len(results)} speaker frames")
        return

    if args.timestamp is None:
        print("Timestamp required (in seconds)")
        return

    # Preview mode
    if args.preview:
        preview_crops(video_path, args.timestamp)
        return

    # Single frame extraction
    print(f"Extracting frame from {args.event_id} at t={args.timestamp}s...")
    frame_path = extract_frame(video_path, args.timestamp)
    img = Image.open(frame_path)
    layout = detect_layout(img)
    print(f"  Layout: {layout}, Frame: {img.size[0]}x{img.size[1]}")

    # Crop
    if args.crop == "custom" and args.box:
        box = tuple(int(x) for x in args.box.split(","))
    elif args.crop == "face":
        box = auto_face_crop(img)
    elif args.crop in PODIUM_VIEW:
        box = PODIUM_VIEW[args.crop]
    elif args.crop in QUAD_SPLIT:
        box = QUAD_SPLIT[args.crop]
    else:
        box = (0, 0, img.size[0], img.size[1])

    cropped = crop_frame(img, box)
    print(f"  Crop '{args.crop}': {cropped.size[0]}x{cropped.size[1]}")

    # Upscale
    if args.upscale:
        cropped = upscale_replicate(cropped, model=args.upscale_model, scale=args.scale, layout=layout)

    # Sharpen
    if args.sharpen:
        cropped = sharpen(cropped, args.sharpen_amount)
        print(f"  Sharpened (amount={args.sharpen_amount})")

    # Save
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if args.output:
        out_path = args.output
    else:
        suffix = f"_{args.upscale_model}" if args.upscale else ""
        suffix += "_sharp" if args.sharpen else ""
        vid_name = os.path.splitext(os.path.basename(video_path))[0]
        out_path = os.path.join(OUTPUT_DIR, f"{vid_name}_t{args.timestamp}_{args.crop}{suffix}.png")

    cropped.save(out_path, quality=95)
    print(f"  Saved: {out_path} ({cropped.size[0]}x{cropped.size[1]})")


if __name__ == "__main__":
    main()

# BOE (Board of Education) meeting layout — split screen
# Top ~40%: panoramic overview of board table
# Bottom ~60%: main camera (speaker at podium / audience)
BOE_SPLIT = {
    "panoramic": (0, 0, 1280, 290),      # top panoramic strip
    "main": (0, 300, 1280, 720),          # bottom main camera view
    "speaker": (400, 290, 1280, 720),     # right side of main (podium area)
}
