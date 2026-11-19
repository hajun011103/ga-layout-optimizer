#!/bin/bash
# 사례 프레임으로 README·발표용 그림을 만든다.
#   bash demo/video/export_media.sh <work_dir> <cases.mp4> [gif_dir]
#   → docs/readme/cases.gif (README 결과 GIF), docs/readme/morph-fpso.gif (맨 위 GIF),
#     gif_dir 가 있으면 사례별 NN_이름.gif/.mp4 (발표 자료용, 1280px)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd); W=$(cd "$1" && pwd); CASES_MP4=$2; G=${3:-}
FF=${FFMPEG:-ffmpeg}; FR=$W/frames
HOLD="tpad=start_mode=clone:start_duration=1:stop_mode=clone:stop_duration=2"
$FF -y -loglevel error -i "$CASES_MP4" -vf "fps=10,scale=960:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 "$ROOT/docs/readme/cases.gif"
$FF -y -loglevel error -framerate 20 -i "$FR/case03/f%03d.png" -vf "scale=960:-2:flags=lanczos,$HOLD,fps=15,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 "$ROOT/docs/readme/morph-fpso.gif"
if [ -n "$G" ]; then
  mkdir -p "$G"
  python3 - "$ROOT/docs/data.js" <<'PY' > "$W/names.txt"
import json, sys, re
s = open(sys.argv[1], encoding="utf-8").read(); j = json.loads(s[s.index("=") + 1:].strip().rstrip(";"))
for i, c in enumerate(j["cases"] if isinstance(j, dict) else j):
    print(f"{i+1:02d}_" + re.sub(r"[\s:·()]+", "", c["title"].replace("실제 도면:", "도면_")))
PY
  while read -r name; do i=${name:0:2}
    $FF -y -loglevel error -framerate 20 -i "$FR/case$i/f%03d.png" -vf "scale=1280:-2:flags=lanczos,$HOLD,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a" -loop 0 "$G/$name.gif"
    $FF -y -loglevel error -framerate 20 -i "$FR/case$i/f%03d.png" -vf "scale=1280:-2:flags=lanczos,$HOLD,format=yuv420p" -c:v libx264 -crf 18 -r 30 -movflags +faststart "$G/$name.mp4"
  done < "$W/names.txt"
fi
ls -lh "$ROOT/docs/readme/cases.gif" "$ROOT/docs/readme/morph-fpso.gif"
