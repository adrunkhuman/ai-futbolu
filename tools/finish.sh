#!/bin/sh
# Local finishing only; generated clips and paid services are not touched.
set -eu
cd "$(dirname "$0")/.."

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo "usage: $0 EPISODE [OUTPUT.mp4]" >&2
    exit 2
fi
episode=$1
case "$episode" in
    ''|*[!a-zA-Z0-9_-]*) echo "invalid episode basename: $episode" >&2; exit 2 ;;
esac
output=${2:-video/$episode.final.mp4}
if [ -e "$output" ] || [ -L "$output" ]; then
    echo "refusing to overwrite: $output" >&2
    exit 1
fi

exec ffmpeg -hide_banner -loglevel error -nostdin -n \
    -i "video/$episode.mp4" -i assets/watermark.png \
    -filter_complex "[1:v]scale=120:-1,format=rgba,colorchannelmixer=aa=0.85[logo];[0:v][logo]overlay=W-w-20:20,ass=video/$episode.ass[out]" \
    -map '[out]' -map '0:a?' -c:v libx264 -preset medium -crf 20 \
    -c:a copy "$output"
