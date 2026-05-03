#!/bin/bash
# Download BOE meeting audio (all 16) and video (most recent 6)
# Audio for Deepgram transcription, video for photos/quotes

cd "$(dirname "$0")"
source .env 2>/dev/null

VIDEOS_DIR="../videos/boe"
mkdir -p "$VIDEOS_DIR"

# 16 most recent meetings (Oct 2024 - Mar 2026)
MEETINGS=(
  "3JzGYFFFFFk|2026-03-12|Regular Meeting"
  "VbgEFwXamTo|2026-02-26|Work Session"
  "RRwsK3F-BEQ|2026-02-12|Regular Meeting"
  "p27Z1GdnRl8|2026-01-22|Work Session"
  "TzciddgWZcE|2026-01-08|Regular Meeting"
  "MqDUuLOvLbQ|2025-05-08|Meeting"
  "cBTo3vCudiQ|2025-04-10|Meeting"
  "awSTEKQyTaU|2025-03-27|Meeting"
  "53vV66enyG8|2025-03-13|Meeting"
  "dH6Bg1ukVt8|2025-02-27|Meeting"
  "zodeWX25CJ8|2025-02-13|Meeting"
  "6DBu2fCHQMo|2025-01-09|Regular Meeting"
  "RsiQIof4ZBs|2024-12-05|Regular Meeting"
  "HAT-jdNYwlY|2024-11-21|Work Session"
  "yBgf-6v0tCE|2024-10-24|Work Session"
  "lxE06e6IiXQ|2024-10-10|Regular Meeting"
)

# First 6 get video + audio, rest get audio only
VIDEO_COUNT=6

echo "=== BOE Meeting Download ==="
echo "Video+Audio for top $VIDEO_COUNT, Audio-only for remaining"
echo ""

count=0
for entry in "${MEETINGS[@]}"; do
  IFS='|' read -r vid date type <<< "$entry"
  count=$((count + 1))

  echo "[$count/16] $date — $type ($vid)"

  # Download video for first 6
  if [ $count -le $VIDEO_COUNT ]; then
    if [ -f "$VIDEOS_DIR/$vid.mp4" ]; then
      echo "  Video: exists"
    else
      echo "  Video: downloading..."
      yt-dlp -f "bestvideo[height<=720]+bestaudio/best[height<=720]" \
        --merge-output-format mp4 \
        -o "$VIDEOS_DIR/$vid.mp4" \
        "https://www.youtube.com/watch?v=$vid" 2>&1 | tail -3
    fi
  fi

  # Download audio for all (for Deepgram)
  if [ -f "$VIDEOS_DIR/${vid}_deepgram.wav" ]; then
    echo "  Audio: exists"
  elif [ -f "$VIDEOS_DIR/$vid.mp4" ]; then
    echo "  Audio: extracting from video..."
    ffmpeg -i "$VIDEOS_DIR/$vid.mp4" -vn -acodec pcm_s16le \
      -ar 16000 -ac 1 "$VIDEOS_DIR/${vid}_deepgram.wav" -y -loglevel warning
  else
    echo "  Audio: downloading..."
    # Download audio-only
    yt-dlp -x --audio-format mp3 --audio-quality 3 \
      -o "$VIDEOS_DIR/${vid}_audio.%(ext)s" \
      "https://www.youtube.com/watch?v=$vid" 2>&1 | tail -2

    # Convert to 16kHz mono wav for Deepgram
    AUDIO_FILE=$(ls "$VIDEOS_DIR/${vid}_audio."* 2>/dev/null | head -1)
    if [ -n "$AUDIO_FILE" ]; then
      ffmpeg -i "$AUDIO_FILE" -ar 16000 -ac 1 \
        "$VIDEOS_DIR/${vid}_deepgram.wav" -y -loglevel warning
      rm -f "$AUDIO_FILE"
    fi
  fi

  # Show status
  [ -f "$VIDEOS_DIR/$vid.mp4" ] && echo "  ✓ Video" || true
  [ -f "$VIDEOS_DIR/${vid}_deepgram.wav" ] && echo "  ✓ Audio (WAV)" || echo "  ✗ Audio missing"
  echo ""
done

echo "=== Done ==="
ls -lh "$VIDEOS_DIR"/*.mp4 2>/dev/null | wc -l | xargs -I{} echo "Videos: {}"
ls -lh "$VIDEOS_DIR"/*_deepgram.wav 2>/dev/null | wc -l | xargs -I{} echo "Audio files: {}"
