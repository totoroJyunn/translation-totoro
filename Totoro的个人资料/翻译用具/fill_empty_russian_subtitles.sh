#!/bin/bash

# ==========================================
# 使用 Whisper.cpp 填充 SRT 中的空白字幕
#
# 功能：
# 1. 遍历当前目录下的 MP4 文件
# 2. 查找同名 SRT 文件
# 3. 找出有时间轴、但文本为空的字幕条目
# 4. 按照每条字幕的时间范围提取音频
# 5. 使用 Whisper 识别俄语
# 6. 将识别结果填入原来的空白字幕
# 7. 保留原有序号、时间轴和非空字幕
# 8. 自动备份原字幕
# ==========================================

set -u

MODEL_PATH="/Users/ttl/Library/Caches/Buzz/models/models--ggerganov--whisper.cpp/snapshots/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo.bin"

LANGUAGE="ru"

# 提取音频前后额外保留的秒数。
# 适当增加前后文，避免句子被截断。
PADDING="0.35"

# 临时目录
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

# 检查依赖
for cmd in whisper-cli ffmpeg ffprobe awk; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "错误：找不到命令 $cmd"
        exit 1
    fi
done

if [ ! -f "$MODEL_PATH" ]; then
    echo "错误：找不到 Whisper 模型："
    echo "$MODEL_PATH"
    exit 1
fi

# 将 SRT 时间转换成秒。
# 例如：00:01:52,312 -> 112.312
srt_time_to_seconds() {
    local t="$1"
    awk -v t="$t" 'BEGIN {
        gsub(",", ".", t)
        split(t, a, ":")
        printf "%.3f", a[1]*3600 + a[2]*60 + a[3]
    }'
}

# 去除首尾空白
trim() {
    printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

shopt -s nullglob
files=( *.mp4 )

if [ ${#files[@]} -eq 0 ]; then
    echo "当前目录没有 MP4 文件。"
    exit 0
fi

for video in "${files[@]}"; do

    echo
    echo "=========================================="

    filename="${video%.mp4}"
    srt_file="${filename}.srt"

    if [ ! -f "$srt_file" ]; then
        echo "跳过：没有找到 $srt_file"
        continue
    fi

    echo "视频：$video"
    echo "字幕：$srt_file"

    # 每个视频单独建立临时目录
    WORK_DIR="$TEMP_DIR/work"
    mkdir -p "$WORK_DIR"
    rm -f "$WORK_DIR"/*

    # 备份字幕
    backup="${srt_file}.bak"

    if [ ! -f "$backup" ]; then
        cp "$srt_file" "$backup"
        echo "已备份：$backup"
    fi

    # 获取字幕总时长，防止时间超出视频
    duration="$(ffprobe -v error \
        -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 \
        "$video" 2>/dev/null)"

    # 用 Python 解析 SRT。
    # 对每条字幕：
    # - 保留原序号
    # - 保留原时间轴
    # - 仅对文本为空的条目执行识别
    #
    # 将空白条目的序号、起止时间输出到 TSV。
    EMPTY_LIST="$WORK_DIR/empty.tsv"

    python3 - "$srt_file" "$EMPTY_LIST" <<'PY'
import re
import sys

srt_path, output_path = sys.argv[1:3]

with open(srt_path, "r", encoding="utf-8-sig") as f:
    content = f.read()

# 兼容 Windows 和 Unix 换行
content = content.replace("\r\n", "\n").replace("\r", "\n")

blocks = re.split(r"\n[ \t]*\n", content.strip())

empty = []

for block_index, block in enumerate(blocks):
    lines = block.split("\n")

    if len(lines) < 2:
        continue

    index = lines[0].strip()

    match = re.match(
        r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2},\d{3})",
        lines[1]
    )

    if not index.isdigit() or not match:
        continue

    start, end = match.groups()

    # 只有时间轴后完全没有文字，或只有空白字符时才处理。
    text = "\n".join(lines[2:]).strip()

    if not text:
        empty.append((int(index), start, end, block_index))

with open(output_path, "w", encoding="utf-8") as f:
    for index, start, end, block_index in empty:
        f.write(f"{index}\t{start}\t{end}\t{block_index}\n")

print(f"找到 {len(empty)} 条空白字幕。")
PY

    if [ ! -s "$EMPTY_LIST" ]; then
        echo "没有需要填充的空白字幕，跳过。"
        continue
    fi

    # 将原始 SRT 分块保存为 JSON，方便后续精确修改
    ORIGINAL_JSON="$WORK_DIR/original.json"

    python3 - "$srt_file" "$ORIGINAL_JSON" <<'PY'
import json
import re
import sys

src, dst = sys.argv[1:3]

with open(src, "r", encoding="utf-8-sig") as f:
    content = f.read()

content = content.replace("\r\n", "\n").replace("\r", "\n")
blocks = re.split(r"\n[ \t]*\n", content.strip())

with open(dst, "w", encoding="utf-8") as f:
    json.dump(blocks, f, ensure_ascii=False)
PY

    # 保存识别结果：序号 -> 文本
    RESULTS="$WORK_DIR/results.json"

    echo '{}' > "$RESULTS"

    while IFS=$'\t' read -r index start_time end_time block_index; do

        echo
        echo "------------------------------------------"
        echo "处理字幕：$index"
        echo "时间范围：$start_time --> $end_time"

        start_seconds="$(srt_time_to_seconds "$start_time")"
        end_seconds="$(srt_time_to_seconds "$end_time")"

        # 在字幕区间前后增加少量上下文。
        # 但是不能超出视频时长。
        extract_start="$(awk -v s="$start_seconds" -v p="$PADDING" \
            'BEGIN { v=s-p; if(v<0)v=0; printf "%.3f",v }')"

        extract_end="$(awk -v e="$end_seconds" -v p="$PADDING" -v d="$duration" \
            'BEGIN {
                v=e+p
                if (d > 0 && v > d) v=d
                printf "%.3f",v
            }')"

        audio_file="$WORK_DIR/${index}.wav"
        whisper_prefix="$WORK_DIR/result_${index}"

        echo "提取音频：${extract_start}s - ${extract_end}s"

        if ! ffmpeg -nostdin -y -loglevel error \
            -ss "$extract_start" \
            -i "$video" \
            -t "$(awk -v s="$extract_start" -v e="$extract_end" \
                'BEGIN { printf "%.3f",e-s }')" \
            -ar 16000 \
            -ac 1 \
            -c:a pcm_s16le \
            "$audio_file"; then

            echo "警告：音频提取失败，跳过字幕 $index"
            continue
        fi

        # 调用 Whisper 识别。
        # 单条字幕单独识别，便于精确填回原时间段。
        if ! whisper-cli \
            -m "$MODEL_PATH" \
            -f "$audio_file" \
            -l "$LANGUAGE" \
            -nt \
            -np \
            -otxt \
            -of "$whisper_prefix" \
            >/dev/null 2>&1; then

            echo "警告：Whisper 识别失败，跳过字幕 $index"
            continue
        fi

        txt_file="${whisper_prefix}.txt"

        if [ ! -f "$txt_file" ]; then
            echo "警告：没有生成识别文本，跳过字幕 $index"
            continue
        fi

        # 去除文本前后空白并保存 JSON。
        # 由 Python 处理转义，避免俄语标点和引号破坏数据。
        python3 - "$RESULTS" "$index" "$txt_file" <<'PY'
import json
import sys

results_path, index, txt_path = sys.argv[1:4]

with open(results_path, "r", encoding="utf-8") as f:
    results = json.load(f)

with open(txt_path, "r", encoding="utf-8-sig") as f:
    text = " ".join(f.read().split())

if text:
    results[index] = text

with open(results_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
PY

        # 输出识别文本供检查
        recognized="$(python3 - "$RESULTS" "$index" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    results = json.load(f)

print(results.get(sys.argv[2], ""))
PY
)"

        if [ -n "$recognized" ]; then
            echo "识别结果：$recognized"
        else
            echo "警告：没有识别到文本。"
        fi

    done < "$EMPTY_LIST"

    # 所有空白字幕识别完成后，统一回填原 SRT。
    # 仅修改空白文本块，不更改序号和时间轴。
    NEW_SRT="$WORK_DIR/filled.srt"

    python3 - "$ORIGINAL_JSON" "$RESULTS" "$NEW_SRT" <<'PY'
import json
import re
import sys

original_path, results_path, output_path = sys.argv[1:4]

with open(original_path, "r", encoding="utf-8") as f:
    blocks = json.load(f)

with open(results_path, "r", encoding="utf-8") as f:
    results = json.load(f)

filled = 0

for i, block in enumerate(blocks):
    lines = block.split("\n")

    if len(lines) < 2:
        continue

    index = lines[0].strip()

    if not index.isdigit():
        continue

    if not re.match(
        r"^\s*\d{2}:\d{2}:\d{2},\d{3}\s*-->",
        lines[1]
    ):
        continue

    # 仅处理原本没有文本的字幕块。
    if "\n".join(lines[2:]).strip():
        continue

    recognized = results.get(index, "").strip()

    if recognized:
        # 将识别结果放回该条字幕的文本位置。
        lines = lines[:2] + [recognized]
        blocks[i] = "\n".join(lines)
        filled += 1

with open(output_path, "w", encoding="utf-8") as f:
    f.write("\n\n".join(blocks) + "\n")

print(f"成功填充 {filled} 条字幕。")
PY

    # 替换前确认结果文件存在
    if [ -f "$NEW_SRT" ]; then
        cp "$NEW_SRT" "$srt_file"
        echo
        echo "完成：$srt_file"
        echo "原始备份：$backup"
    else
        echo "错误：没有生成结果文件，原字幕未修改。"
    fi

done

echo
echo "=========================================="
echo "所有字幕处理完毕！"