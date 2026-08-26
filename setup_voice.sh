#!/usr/bin/env bash
# Build the local speech recognizer PocketTUI's mic key talks to.
#
# Everything lands in voice/ next to app.py and nothing leaves this machine:
# whisper.cpp compiled here with one ~142 MB English model, then optionally the
# ~600 MB Parakeet-TDT ONNX model app.py prefers when it is present. No sudo,
# and safe to re-run — each step skips if its output is already in place.
#
# Run from wherever app.py lives — a git checkout or the directory install.sh
# unpacked the tarball into. Nothing here reads this script's own repository;
# whisper.cpp is cloned fresh into .whisper-build.
#
# --parakeet / --whisper / --all install only the named engine(s), no prompts —
# this is how install.sh drives a menu choice. With no flag: an interactive
# terminal is asked which to install (same menu install.sh offers); anything
# else (piped, redirected) defaults to both, matching this script's behavior
# before the flags existed.
set -eu

C_RESET=""; C_STEP=""; C_OK=""; C_DIM=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    C_RESET=$'\033[0m'
    C_STEP=$'\033[36m'
    C_OK=$'\033[32m'
    C_DIM=$'\033[2m'
fi

DO_WHISPER=0
DO_PARAKEET=0
case "${1:-}" in
  --parakeet) DO_PARAKEET=1 ;;
  --whisper)  DO_WHISPER=1 ;;
  --all)      DO_WHISPER=1; DO_PARAKEET=1 ;;
  "")
    if [ -t 0 ]; then
      echo "Which voice engine should PocketTUI use?"
      echo "  ${C_STEP}1) Parakeet${C_RESET} ${C_OK}(recommended, ~600 MB download, fastest)${C_RESET}"
      echo "  ${C_STEP}2) Whisper${C_RESET} ${C_DIM}(~142 MB, builds whisper.cpp)${C_RESET}"
      echo "  ${C_STEP}3) Both${C_RESET}"
      echo "  ${C_STEP}4) None${C_RESET} ${C_DIM}— use phone dictation${C_RESET}"
      printf 'choice %s[1-4]%s: ' "$C_DIM" "$C_RESET"
      read -r choice
      case "$choice" in
        1) DO_PARAKEET=1 ;;
        2) DO_WHISPER=1 ;;
        3) DO_WHISPER=1; DO_PARAKEET=1 ;;
        *) echo "skipped — nothing installed."; exit 0 ;;
      esac
    else
      DO_WHISPER=1
      DO_PARAKEET=1
    fi
    ;;
  *) echo "usage: $0 [--parakeet | --whisper | --all]"; exit 1 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
VOICE="$HERE/voice"
WORK="$HERE/.whisper-build"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
PARAKEET="$VOICE/parakeet"
# The English-only v2 build. v3 is multilingual and measurably worse on English,
# which is all this dictates.
PARAKEET_MODEL="sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
PARAKEET_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$PARAKEET_MODEL.tar.bz2"

# Build tools are only needed to compile whisper.cpp — Parakeet is a plain
# download, so a Parakeet-only run must not be blocked by a missing compiler.
if [ "$DO_WHISPER" = 1 ]; then
  command -v cmake >/dev/null || { echo "need cmake (apt install cmake)"; exit 1; }
  command -v make >/dev/null || { echo "need make (apt install make)"; exit 1; }
  command -v c++ >/dev/null || { echo "need a C++ compiler (apt install g++)"; exit 1; }
  command -v git >/dev/null || { echo "need git"; exit 1; }
fi
command -v ffmpeg >/dev/null || echo "warning: ffmpeg missing — voice needs it at runtime"

mkdir -p "$VOICE"

if [ "$DO_WHISPER" = 1 ] && [ ! -x "$VOICE/whisper-cli" ]; then
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

if [ "$DO_WHISPER" = 1 ] && [ ! -s "$VOICE/ggml-base.en.bin" ]; then
  # Fetched straight from HuggingFace rather than via whisper.cpp's own
  # download script, which uses curl options older curl builds reject.
  echo "downloading ggml-base.en.bin (~142 MB)..."
  if command -v curl >/dev/null; then
    curl -fL --retry 3 -o "$VOICE/ggml-base.en.bin.part" "$MODEL_URL"
  else
    wget -O "$VOICE/ggml-base.en.bin.part" "$MODEL_URL"
  fi
  # Named only once complete, so an interrupted download cannot look installed.
  mv "$VOICE/ggml-base.en.bin.part" "$VOICE/ggml-base.en.bin"
fi

if [ "$DO_WHISPER" = 1 ]; then
  LD_LIBRARY_PATH="$VOICE" "$VOICE/whisper-cli" --help >/dev/null 2>&1 \
    || { echo "whisper-cli will not run — check the build above"; exit 1; }
fi

# Parakeet-TDT, the engine app.py prefers when it is present. It decodes an
# utterance in a fraction of the time base.en takes and writes ordinary English
# better; whisper stays as the fallback when it is also installed, so this half
# is optional and a skipped or failed download leaves a working install behind.
#
# The wheel that runs it (sherpa-onnx) is in requirements.txt, which install.sh
# installs into the venv — there is nothing for this script to pip-install.
if [ "$DO_PARAKEET" = 1 ] && [ ! -d "$PARAKEET/$PARAKEET_MODEL" ]; then
  mkdir -p "$PARAKEET"
  echo "downloading $PARAKEET_MODEL (~600 MB)..."
  if command -v curl >/dev/null; then
    curl -fL --retry 3 -o "$PARAKEET/model.tar.bz2.part" "$PARAKEET_URL"
  else
    wget -O "$PARAKEET/model.tar.bz2.part" "$PARAKEET_URL"
  fi
  # Unpacked from the .part name and only then moved into place, so an
  # interrupted download or a truncated archive cannot look installed —
  # app.py's probe checks for the four files, and a half-unpacked directory
  # would otherwise read as a model it could load.
  tar -xjf "$PARAKEET/model.tar.bz2.part" -C "$PARAKEET"
  rm -f "$PARAKEET/model.tar.bz2.part"
  echo "installed $PARAKEET_MODEL"
fi

echo "voice ready: $VOICE"
echo "restart PocketTUI to pick it up."
