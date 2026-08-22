#!/usr/bin/env bash
# Build the local speech recognizer PocketTUI's mic key talks to.
#
# Everything lands in voice/ next to app.py and nothing leaves this machine:
# whisper.cpp compiled here, one ~148 MB English model downloaded once. No sudo,
# and safe to re-run — each step skips if its output is already in place.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
VOICE="$HERE/voice"
WORK="$HERE/.whisper-build"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"

command -v cmake >/dev/null || { echo "need cmake (apt install cmake)"; exit 1; }
command -v git >/dev/null || { echo "need git"; exit 1; }
command -v ffmpeg >/dev/null || echo "warning: ffmpeg missing — voice needs it at runtime"

mkdir -p "$VOICE"

if [ ! -x "$VOICE/whisper-cli" ]; then
  [ -d "$WORK" ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$WORK"
  cmake -S "$WORK" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF
  cmake --build "$WORK/build" --config Release -j "$(nproc 2>/dev/null || echo 4)"
  # The binary loads its ggml/whisper shared objects by RUNPATH, which cmake
  # bakes to the build tree — so they have to travel with it and app.py sets
  # LD_LIBRARY_PATH to this directory.
  cp "$WORK/build/bin/whisper-cli" "$VOICE/"
  find "$WORK/build/bin" -maxdepth 1 -name '*.so*' -exec cp -P {} "$VOICE/" \;
  echo "built whisper-cli"
fi

if [ ! -s "$VOICE/ggml-base.en.bin" ]; then
  # Fetched straight from HuggingFace rather than via whisper.cpp's own
  # download script, which uses curl options older curl builds reject.
  echo "downloading ggml-base.en.bin (~148 MB)..."
  if command -v curl >/dev/null; then
    curl -fL --retry 3 -o "$VOICE/ggml-base.en.bin.part" "$MODEL_URL"
  else
    wget -O "$VOICE/ggml-base.en.bin.part" "$MODEL_URL"
  fi
  # Named only once complete, so an interrupted download cannot look installed.
  mv "$VOICE/ggml-base.en.bin.part" "$VOICE/ggml-base.en.bin"
fi

LD_LIBRARY_PATH="$VOICE" "$VOICE/whisper-cli" --help >/dev/null 2>&1 \
  || { echo "whisper-cli will not run — check the build above"; exit 1; }

echo "voice ready: $VOICE"
echo "restart PocketTUI to pick it up."
