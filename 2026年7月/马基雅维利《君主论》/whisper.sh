#!/bin/bash

# 指定模型路径
MODEL_PATH="/Users/ttl/Library/Caches/Buzz/models/models--ggerganov--whisper.cpp/snapshots/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo.bin"

for file in *.mp4; do
    if [ -f "$file" ]; then
        echo "========================================"
        echo "开始处理: $file"
        
        # 去掉 .mp4 后缀，拼接出 .wav 的文件名
        filename="${file%.mp4}"
        wav_file="${filename}.wav"
        
        # 1. 使用 ffmpeg 提取 16kHz 单声道 wav 音频 (whisper.cpp 标准要求)
        echo "[1/3] 正在提取音频..."
        ffmpeg -i "$file" -ar 16000 -ac 1 -c:a pcm_s16le "$wav_file" -y -loglevel error
        
        # 2. 运行 whisper-cli
        echo "[2/3] 正在生成字幕..."
        whisper-cli -m "$MODEL_PATH" -f "$wav_file" -l ja -osrt -et 2.0 -lpt -0.5
        
        # 3. 清理临时的 wav 文件
        echo "[3/3] 清理临时文件..."
        rm "$wav_file"
        
        echo "完成: $file"
    fi
done

echo "========================================"
echo "所有文件处理完毕！"