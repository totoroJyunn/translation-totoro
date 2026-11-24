#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

MODEL = Path('/Users/ttl/Library/Caches/Buzz/models/models--ggerganov--whisper.cpp/snapshots/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-large-v3-turbo.bin')
TIME_RE = re.compile(r'^\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$')

@dataclass
class Cue:
    start: float
    end: float
    text: str

def seconds(s):
    parts = str(s).replace(',', '.').split(':')
    if len(parts) > 3 or not 1 <= len(parts):
        raise ValueError(f'时间格式错误: {s}')
    return sum(float(v) * 60 ** i for i, v in enumerate(reversed(parts)))

def stamp(value):
    n = max(0, round(value * 1000))
    h, n = divmod(n, 3600000)
    m, n = divmod(n, 60000)
    s, n = divmod(n, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{n:03d}'

def read_srt(path):
    data = path.read_text(encoding='utf-8-sig').replace('\r\n', '\n').strip()
    if not data:
        return []
    cues = []
    for block in re.split(r'\n\s*\n', data):
        lines = block.splitlines()
        idx = next((i for i, line in enumerate(lines[:2]) if '-->' in line), None)
        if idx is None:
            raise ValueError(f'SRT 中存在无法解析的条目: {path}\n{block[:120]}')
        match = TIME_RE.match(lines[idx])
        if not match:
            raise ValueError(f'无效时间轴: {path}\n{lines[idx]}')
        a, b = map(seconds, match.groups())
        if b < a:
            raise ValueError(f'时间轴起止颠倒: {path}\n{lines[idx]}')
        cues.append(Cue(a, b, '\n'.join(lines[idx + 1:]).strip()))
    return cues

def write_srt(path, cues):
    content = '\n\n'.join(
        f'{i}\n{stamp(c.start)} --> {stamp(c.end)}\n{c.text}'
        for i, c in enumerate(sorted(cues, key=lambda x: (x.start, x.end)), 1)
    )
    # 写入同目录临时文件，再原子替换，避免意外中断破坏文件。
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                     prefix='.srt_', suffix='.tmp', delete=False) as f:
        tmp = Path(f.name)
        f.write(content + ('\n' if content else ''))
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)

def duration(video):
    raw = subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(video)
    ], text=True).strip()
    return float(raw)

def transcribe(video, start, end, total, args, tmpdir):
    result = []
    pos = start
    while pos < end - 0.05:
        stop = min(end, pos + args.chunk)
        a = max(0, pos - args.pad)
        b = min(total, stop + args.pad)
        wav = tmpdir / 'clip.wav'
        out = tmpdir / 'clip_result'
        print(f'    识别 {stamp(pos)} → {stamp(stop)}', flush=True)
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-ss', f'{a:.3f}', '-i', str(video), '-t', f'{b-a:.3f}',
            '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)
        ], check=True)
        # 上一片段可能有输出而本片段完全无语音，必须先删除旧 SRT。
        srt = out.with_suffix('.srt')
        srt.unlink(missing_ok=True)
        cmd = [
            'whisper-cli', '-m', str(args.model), '-f', str(wav),
            '-l', args.language, '-osrt', '-of', str(out),
            '-mc', '0', '-nf', '-sns'
        ]
        if args.vad_model:
            cmd += ['--vad', '-vm', str(args.vad_model)]
        run = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if run.returncode != 0:
            raise RuntimeError(f'whisper-cli 执行失败:\n{run.stderr[-1500:]}')
        if srt.exists() and srt.stat().st_size:
            for c in read_srt(srt):
                x, y = a + c.start, a + c.end
                mid = (x + y) / 2
                if pos - 0.001 <= mid <= stop + 0.001 and c.text.strip():
                    x, y = max(x, pos), min(y, stop)
                    if y > x + 0.03:
                        result.append(Cue(x, y, c.text))
        pos = stop
    return sorted(result, key=lambda c: c.start)

def warn_repeats(cues):
    last, first, n = None, None, 0
    for c in sorted((c for c in cues if c.text.strip()), key=lambda c: c.start):
        key = re.sub(r'[\W_]+', '', c.text, flags=re.UNICODE)
        if len(key) >= 5 and key == last:
            n += 1
        else:
            last, first, n = key, c.start, 1
        if n == 3:
            print(f'  ⚠️ {stamp(first)} 附近有连续重复字幕，请人工复核')

def process(video, args):
    total = duration(video)
    srt = video.with_suffix('.srt')
    with tempfile.TemporaryDirectory(prefix='whisper_repair_') as d:
        tmpdir = Path(d)
        if args.start is not None or args.end is not None:
            a = seconds(args.start) if args.start is not None else 0.0
            b = seconds(args.end) if args.end is not None else total
            if not (0 <= a < b <= total + 0.1):
                raise ValueError(f'指定区间超出视频时长: {stamp(total)}')
            print(f'\n{video.name}：单独识别指定区间')
            cues = transcribe(video, a, min(b, total), total, args, tmpdir)
            target = video.with_name(f'{video.stem}.range_{round(a*1000)}-{round(b*1000)}.srt')
            write_srt(target, cues)
            warn_repeats(cues)
            print(f'  已保存: {target.name}（原字幕未改动）')
            return
        old = read_srt(srt) if srt.exists() else []
        if not srt.exists():
            print(f'\n{video.name}：首次完整识别')
            cues = transcribe(video, 0, total, total, args, tmpdir)
        else:
            print(f'\n{video.name}：检查已有 SRT（{len(old)} 条）')
            cues = list(old)
            filled = 0
            for i, c in enumerate(cues):
                if c.text.strip() or c.end <= c.start:
                    continue
                print(f'  补空白条目 #{i+1}: {stamp(c.start)} → {stamp(c.end)}')
                fresh = transcribe(video, max(0, c.start), min(total, c.end), total, args, tmpdir)
                if fresh:
                    # 保留空条目的原始时间轴与编号位置；不触碰相邻已校对字幕。
                    cues[i] = Cue(c.start, c.end, ''.join(x.text.strip() for x in fresh))
                    filled += 1
            print(f'  已补空白条目: {filled}')
            last = min(total, max((c.end for c in old), default=0))
            gap = total - last
            if gap >= args.tail_min:
                print(f'  发现结尾缺失 {gap:.1f} 秒，从 {stamp(last)} 补录')
                cues.extend(transcribe(video, last, total, total, args, tmpdir))
            if cues == old:
                print('  没有新增字幕；原文件保持不变')
                return
            backup = srt.with_name(srt.name + '.bak.' + datetime.now().strftime('%Y%m%d_%H%M%S'))
            shutil.copy2(srt, backup)
            print(f'  已备份: {backup.name}')
        write_srt(srt, cues)
        warn_repeats(cues)
        print(f'  完成: {srt.name}（共 {len(cues)} 条）')

def main():
    p = argparse.ArgumentParser(description='whisper.cpp 批量识别 / SRT 自动修复')
    p.add_argument('videos', nargs='*', type=Path, help='视频路径；不写则处理当前目录全部 mp4')
    p.add_argument('--start', help='时间段开始，如 12:30 或 01:12:30')
    p.add_argument('--end', help='时间段结束，如 15:00；不写则到视频末尾')
    p.add_argument('--chunk', type=float, default=45, help='每段识别秒数（默认45）')
    p.add_argument('--pad', type=float, default=0.7, help='每段两端音频上下文秒数（默认0.7）')
    p.add_argument('--tail-min', type=float, default=15, help='至少缺失多少秒才补尾部（默认15）')
    p.add_argument('--language', default='ja', help='识别语言（默认 ja）')
    p.add_argument('--model', type=Path, default=MODEL)
    p.add_argument('--vad-model', type=Path, help='可选 Silero VAD ggml 模型路径')
    args = p.parse_args()
    if args.chunk <= 0 or args.pad < 0 or args.tail_min < 0:
        p.error('--chunk 必须大于0；--pad 和 --tail-min 不能为负数')
    for app in ('ffmpeg', 'ffprobe', 'whisper-cli'):
        if shutil.which(app) is None:
            p.error(f'找不到 {app}，请确认它已安装并在 PATH 中')
    if not args.model.is_file():
        p.error(f'找不到 Whisper 模型: {args.model}')
    if args.vad_model and not args.vad_model.is_file():
        p.error(f'找不到 VAD 模型: {args.vad_model}')
    videos = args.videos or sorted(Path.cwd().glob('*.mp4'))
    if not videos:
        p.error('当前目录没有 MP4 文件')
    failed = []
    for video in videos:
        try:
            if not video.is_file():
                raise FileNotFoundError(video)
            process(video, args)
        except Exception as e:
            print(f'❌ {video}: {e}')
            failed.append(str(video))
    if failed:
        raise SystemExit(f'处理失败 {len(failed)} 个文件: {", ".join(failed)}')

if __name__ == '__main__':
    main()
