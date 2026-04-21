#!/bin/bash

# 指定模型路径
MODEL_PATH="/Users/ttl/Library/Caches/Buzz/models/models--ggerganov--whisper.cpp/snapshots/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo.bin"

for file in *.mp4; do
    if [ -f "$file" ]; then
        filename="${file%.mp4}"
        wav_file="${filename}.wav"
        srt_file="${filename}.srt"
        
        echo "========================================"
        if [ ! -f "$srt_file" ]; then
            echo "跳过: $file (未找到同名字幕文件 $srt_file)"
            continue
        fi

        echo "开始处理: $file"

        # 1. 解析当前字幕的最后一条时间戳和序号
        # 抓取最后一行类似 "00:21:31,135 --> 00:21:32,403" 的时间轴
        last_timeline=$(grep -Eo '[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} --> [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3}' "$srt_file" | tail -n 1)
        
        if [ -z "$last_timeline" ]; then
            echo "错误: 无法在 $srt_file 中找到有效时间戳，跳过。"
            continue
        fi

        # 提取结束时间 (例如 00:21:32,403)
        last_end_time=$(echo "$last_timeline" | awk '{print $3}')
        # 提取最后的序号 (纯数字行)
        last_index=$(grep -Eo '^[0-9]+$' "$srt_file" | tail -n 1)

        echo "检测到现有字幕最后序号: $last_index"
        echo "检测到现有字幕断点时间: $last_end_time"

        # 将 HH:MM:SS,mmm 转换为毫秒计算 offset
        IFS=':, ' read -r h m s ms <<< "${last_end_time//,/:}"
        # 使用 10# 强制按十进制计算，避免 bash 将 08/09 误认为错误的八进制
        offset_ms=$(( 10#$h * 3600000 + 10#$m * 60000 + 10#$s * 1000 + 10#$ms ))
        echo "计算得偏移量 (毫秒): $offset_ms"

        # 2. 提取 16kHz 单声道 wav 音频
        echo "[1/4] 正在提取音频..."
        ffmpeg -i "$file" -ar 16000 -ac 1 -c:a pcm_s16le "$wav_file" -y -loglevel error

        # 3. 运行 whisper-cli，加入 -ot 参数跳过前面的音频
        echo "[2/4] 正在生成后续字幕..."
        whisper-cli -m "$MODEL_PATH" -f "$wav_file" -l ja -osrt -et 2.0 -lpt -0.5 -ot "$offset_ms"
        
        # whisper 生成的临时字幕文件默认名为 filename.wav.srt
        gen_srt="${wav_file}.srt"

        if [ ! -f "$gen_srt" ]; then
            echo "错误: whisper 未能生成后续字幕 $gen_srt"
            rm -f "$wav_file"
            continue
        fi

        # 4. 合并字幕并修正序号
        echo "[3/4] 正在合并并修正字幕序号..."
        
        # 确保原文件末尾有换行符，防止拼接粘连
        echo "" >> "$srt_file"
        
        # 使用 awk 读取新生成的 SRT，遇到序号行时，加上原字幕最后的序号（使 1 变成 370）
        awk -v offset="$last_index" '
        /^[0-9]+[ \t]*$/ { print $1 + offset; next }
        { print }
        ' "$gen_srt" >> "$srt_file"

        # 5. 清理临时文件
        echo "[4/4] 清理临时文件..."
        rm -f "$wav_file"
        rm -f "$gen_srt"
        
        echo "完成补全: $file"
    fi
done

echo "========================================"
echo "所有文件补全完毕！"