#!/bin/bash
# 녹화 조각을 1920x1080/30fps 로 맞춰 이어 붙인다.
#   bash demo/video/assemble.sh <work_dir> demo  [out.mp4]   — 사용 방법 + 사례 (전체 데모)
#   bash demo/video/assemble.sh <work_dir> cases [out.mp4]   — 사례 변형만 (README 의 cases.mp4)
# <work_dir> 에는 cards/ (cards.py), frames/caseNN/ (record_cases.py),
# studio_events.json · chat_events.json · web_events.json (record_*.py) 이 있어야 한다.
set -euo pipefail
W=$(cd "$1" && pwd); MODE=${2:-demo}; OUT=${3:-$W/smr_ga_$MODE.mp4}
C=$W/cards; FR=$W/frames; FF=${FFMPEG:-ffmpeg}
SEG=$W/seg; rm -rf "$SEG"; mkdir -p "$SEG"
ENC=(-c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p -r 30 -an)
n=0
calc() { python3 -c "print(round($1,2))"; }
card() {  # png, seconds
  n=$((n+1)); local d=$2
  $FF -y -loglevel error -loop 1 -framerate 30 -t $d -i "$1" -vf "format=yuv420p,fade=in:st=0:d=0.4,fade=out:st=$(calc "$d-0.4"):d=0.4" "${ENC[@]}" "$SEG/$(printf %02d $n).mp4"
}
clip() {  # webm, from, to
  n=$((n+1)); local d=$(calc "$3-$2")
  $FF -y -loglevel error -ss $2 -to $3 -i "$1" -vf "fps=30,scale=1920:1080:flags=lanczos,format=yuv420p,fade=in:st=0:d=0.4,fade=out:st=$(calc "$d-0.4"):d=0.4" "${ENC[@]}" "$SEG/$(printf %02d $n).mp4"
}
morph() {  # case index (1..)
  n=$((n+1)); local i=$(printf %02d $1)
  $FF -y -loglevel error -loop 1 -framerate 30 -t 6 -i "$C/head$i.png" -framerate 20 -i "$FR/case$i/f%03d.png" \
    -filter_complex "[1]scale=-2:900:flags=lanczos,tpad=start_mode=clone:start_duration=1:stop_mode=clone:stop_duration=2,fps=30[c];[0][c]overlay=(W-w)/2:165:shortest=1,format=yuv420p,fade=in:st=0:d=0.4,fade=out:st=5.6:d=0.4" \
    "${ENC[@]}" "$SEG/$(printf %02d $n).mp4"
}
ev() { python3 -c "import json;e=dict(json.load(open('$1'))['events']);print(e['$2'])"; }
vid() { python3 -c "import json;print(json.load(open('$1'))['video'])"; }
NC=$(ls -d "$FR"/case* | wc -l)

if [ "$MODE" = demo ]; then
  card "$C/intro.png" 4
  card "$C/how.png" 10
  card "$C/ch1.png" 3
  SJ=$W/studio_events.json; SV=$(vid "$SJ"); RS=$(ev "$SJ" run_start); RD=$(ev "$SJ" run_done)
  clip "$SV" "$(ev "$SJ" start)" "$(calc "$RS+4")"          # 계산 대기 구간은 잘라낸다
  clip "$SV" "$(calc "$RD-0.3")" "$(ev "$SJ" end)"
  card "$C/ch2.png" 3
  CJ=$W/chat_events.json; clip "$(vid "$CJ")" "$(ev "$CJ" start)" "$(ev "$CJ" end)"
  card "$C/ch3.png" 9
  card "$C/ch4.png" 3
  WJ=$W/web_events.json; clip "$(vid "$WJ")" "$(ev "$WJ" start)" "$(ev "$WJ" end)"
  card "$C/ch5.png" 3
fi
for i in $(seq 1 $NC); do morph $i; done
[ "$MODE" = demo ] && card "$C/end.png" 4

ls "$SEG"/*.mp4 | sed "s/^/file '/; s/$/'/" > "$SEG/list.txt"
$FF -y -loglevel error -f concat -safe 0 -i "$SEG/list.txt" -c copy -movflags +faststart "$OUT"
echo "$OUT"
