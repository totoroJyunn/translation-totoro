#!/usr/bin/env python3
"""whisper.cpp 日语 SRT：单次识别、补空白/补尾部、相邻字幕重复检查。

与旧版的关键区别：完整识别不再按 45 秒切割并把片段时间裁掉但保留整句。
已有 SRT 默认不修改手工校对过的字幕；去重必须显式使用 --fix-srt。
依赖系统命令 ffmpeg、ffprobe、whisper-cli；不需要 Python 第三方包或虚拟环境。
"""

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


def seconds(value):
    parts = str(value).replace(',', '.').split(':')
    if not (1 <= len(parts) <= 3):
        raise ValueError(f'时间格式错误: {value}')
    return sum(float(part) * (60 ** i) for i, part in enumerate(reversed(parts)))


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
        start, end = map(seconds, match.groups())
        if end < start:
            raise ValueError(f'时间轴起止颠倒: {path}\n{lines[idx]}')
        cues.append(Cue(start, end, '\n'.join(lines[idx+1:]).strip()))
    return cues


def write_srt(path, cues):
    content = '\n\n'.join(
        f'{i}\n{stamp(c.start)} --> {stamp(c.end)}\n{c.text}'
        for i, c in enumerate(sorted(cues, key=lambda c: (c.start, c.end)), 1)
    )
    with tempfile.NamedTemporaryFile('w', encoding='utf-8-sig', dir=path.parent,
                                     prefix='.srt_', suffix='.tmp', delete=False) as f:
        temp = Path(f.name)
        f.write(content + ('\n' if content else ''))
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def video_duration(video):
    raw = subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(video)
    ], text=True).strip()
    return float(raw)


def transcribe_interval(video, start, end, args, tmpdir):
    """将整个指定区间识别一次；不使用人为分片和重叠补边。"""
    if end - start < 0.05:
        return []
    wav = tmpdir / 'audio.wav'
    out = tmpdir / 'result'
    srt = out.with_suffix('.srt')
    srt.unlink(missing_ok=True)
    print(f'    识别 {stamp(start)} → {stamp(end)}（单次识别，不切45秒片）', flush=True)
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-ss', f'{start:.3f}', '-i', str(video), '-t', f'{end-start:.3f}',
        '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)
    ], check=True)
    cmd = ['whisper-cli', '-m', str(args.model), '-f', str(wav),
           '-l', args.language, '-osrt', '-of', str(out), '-mc', '0', '-nf', '-sns']
    if args.vad_model:
        cmd += ['--vad', '-vm', str(args.vad_model)]
    run = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if run.returncode != 0:
        raise RuntimeError(f'whisper-cli 执行失败:\n{run.stderr[-1500:]}')
    if not srt.exists() or not srt.stat().st_size:
        return []
    result = []
    for c in read_srt(srt):
        x, y = start + c.start, start + c.end
        # 解码器可能返回几毫秒越界；这里不裁剪跨音频边界的内容文字。
        x, y = max(start, x), min(end, y)
        if y > x + 0.03 and c.text.strip():
            result.append(Cue(x, y, c.text))
    return sorted(result, key=lambda c: (c.start, c.end))


def normalized_chars_with_positions(value):
    """忽略标点/空白来比较；保留字符位置以便安全删除原文中的重复后缀。"""
    chars, positions = [], []
    for idx, ch in enumerate(value):
        if ch.isalnum():
            chars.append(ch.casefold())
            positions.append(idx)
    return ''.join(chars), positions


def find_overlaps(cues, min_chars=6, max_gap=1.5):
    """仅检查紧邻字幕前条后缀 == 后条前缀，不靠语义猜测。"""
    findings = []
    for i in range(len(cues) - 1):
        a, b = cues[i], cues[i+1]
        if b.start < a.start or b.start - a.end > max_gap:
            continue
        if not a.text.strip() or not b.text.strip():
            continue
        norm_a, pos_a = normalized_chars_with_positions(a.text)
        norm_b, pos_b = normalized_chars_with_positions(b.text)
        nmax = min(len(norm_a), len(norm_b))
        common = next((n for n in range(nmax, min_chars-1, -1)
                       if norm_a.endswith(norm_b[:n])), 0)
        if common:
            # 从前一条删掉这段后缀，其余位置不动；若前一条将全空，交给人工处理。
            start_idx = pos_a[len(norm_a) - common]
            retained = a.text[:start_idx].rstrip()
            # 自动模式非常保守；常见的连续口头重复不应被自动消除。
            auto_safe = common >= 10 and len(normalized_chars_with_positions(retained)[0]) >= 3
            findings.append({
                'i': i, 'j': i+1, 'n': common,
                'phrase': a.text[start_idx:].strip(),
                'old_a': a.text, 'b': b.text, 'new_a': retained,
                'auto_safe': auto_safe,
            })
    return findings


def write_overlap_report(srt, cues, findings):
    report = srt.with_name(srt.stem + '.overlap_report.txt')
    lines = [f'文件: {srt.name}', f'字幕条数: {len(cues)}',
             f'疑似相邻重复: {len(findings)} 处',
             '说明: 这是基于文字的检测，不能代替音频复听。',
             '推荐只删除前一条中提前出现的重复后缀；实际时间点请以视频为准。', '']
    for f in findings:
        a, b = cues[f['i']], cues[f['j']]
        lines.extend([
            f"#{f['i']+1} / #{f['j']+1}  {stamp(a.start)} → {stamp(b.end)}",
            f"重叠文字({f['n']}字): {f['phrase']}",
            f"前条: {f['old_a']}",
            f"后条: {f['b']}",
            f"建议前条: {f['new_a'] or '[整条重复，必须人工核对]'}",
            f"可自动修订: {'是' if f['auto_safe'] else '否，需要复听'}", ''
        ])
    if findings:
        report.write_text('\n'.join(lines), encoding='utf-8-sig')
        print(f'  ⚠️ 发现 {len(findings)} 处疑似跨字幕重复，详见: {report.name}')
    else:
        print('  未发现明显的相邻字幕文字重叠')
    return report if findings else None


def check_or_fix_srt(path, do_fix=False):
    if not path.is_file():
        raise FileNotFoundError(path)
    cues = read_srt(path)
    findings = find_overlaps(cues)
    write_overlap_report(path, cues, findings)
    if not do_fix:
        print('  仅检查：原 SRT 完全未改动')
        return
    # 对低置信度内容不自动删除；以新文件输出，绝不覆盖原字幕。
    revised = [Cue(c.start, c.end, c.text) for c in cues]
    repaired = 0
    for f in findings:
        if f['auto_safe']:
            revised[f['i']].text = f['new_a']
            repaired += 1
    target = path.with_name(path.stem + '.dedup.srt')
    if target.resolve() == path.resolve():
        raise ValueError('输出文件与输入文件相同')
    if target.exists():
        raise FileExistsError(f'修复输出文件已经存在，为安全起见不覆盖: {target}')
    write_srt(target, revised)
    print(f'  已生成: {target.name}（保守修复 {repaired} 处，其余保持不变；时间码不动）')
    print('  注意：文字去重≠语音时间校准。正式压制前请复听报告中的位置。')


def process(video, args):
    total = video_duration(video)
    srt = video.with_suffix('.srt')
    with tempfile.TemporaryDirectory(prefix='whisper_repair_') as d:
        tmpdir = Path(d)
        if args.start is not None or args.end is not None:
            a = seconds(args.start) if args.start is not None else 0.0
            b = seconds(args.end) if args.end is not None else total
            if not (0 <= a < b <= total + 0.1):
                raise ValueError(f'指定区间超出视频时长: {stamp(total)}')
            print(f'\n{video.name}: 仅识别指定区间（不会触碰原字幕）')
            cues = transcribe_interval(video, a, min(b, total), args, tmpdir)
            target = video.with_name(f'{video.stem}.range_{round(a*1000)}-{round(b*1000)}.srt')
            write_srt(target, cues)
            write_overlap_report(target, cues, find_overlaps(cues))
            print(f'  已保存: {target.name}')
            return
        old = read_srt(srt) if srt.exists() else []
        if not srt.exists():
            print(f'\n{video.name}: 首次完整识别')
            cues = transcribe_interval(video, 0, total, args, tmpdir)
        else:
            print(f'\n{video.name}: 检查已有 SRT（{len(old)} 条）')
            cues = [Cue(c.start, c.end, c.text) for c in old]
            filled = 0
            for i, c in enumerate(cues):
                if c.text.strip() or c.end <= c.start:
                    continue
                print(f'  补空白条目 #{i+1}: {stamp(c.start)} → {stamp(c.end)}')
                fresh = transcribe_interval(video, max(0, c.start), min(total, c.end), args, tmpdir)
                if fresh:
                    cues[i].text = ''.join(x.text.strip() for x in fresh)
                    filled += 1
            print(f'  已补空白条目: {filled}')
            last = min(total, max((c.end for c in old), default=0))
            gap = total - last
            if gap >= args.tail_min:
                print(f'  发现结尾缺失 {gap:.1f} 秒，从 {stamp(last)} 补录')
                cues.extend(transcribe_interval(video, last, total, args, tmpdir))
            if cues == old:
                # 即使没有新增，也检查已存在的相邻文字重复。
                write_overlap_report(srt, old, find_overlaps(old))
                print('  没有新增字幕；原 SRT 保持不变')
                return
            backup = srt.with_name(srt.name + '.bak.' + datetime.now().strftime('%Y%m%d_%H%M%S'))
            shutil.copy2(srt, backup)
            print(f'  已备份: {backup.name}')
        write_srt(srt, cues)
        write_overlap_report(srt, cues, find_overlaps(cues))
        print(f'  完成: {srt.name}（{len(cues)} 条）')


def main():
    p = argparse.ArgumentParser(description='whisper.cpp 日语字幕识别／检测相邻文字重复')
    p.add_argument('videos', nargs='*', type=Path, help='视频路径；不写则处理当前目录的全部 mp4')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--check-srt', type=Path, help='仅检查现有 SRT 是否有跨字幕重复，不需要视频')
    mode.add_argument('--fix-srt', type=Path, help='保守修复明显重复，另存 *.dedup.srt，不修改原 SRT')
    p.add_argument('--start', help='可选时间区间开始，如 12:30 或 01:12:30')
    p.add_argument('--end', help='可选时间区间结束')
    p.add_argument('--tail-min', type=float, default=15, help='至少缺失多少秒才补尾部（默认15）')
    p.add_argument('--language', default='ja', help='识别语言（默认ja）')
    p.add_argument('--model', type=Path, default=MODEL)
    p.add_argument('--vad-model', type=Path, help='可选 Silero VAD ggml 模型路径')
    # 保留旧参数名，以免用户之前的启动命令报错，但不再切固定长度的音频片段。
    p.add_argument('--chunk', type=float, default=None, help='已弃用：新版不按固定秒数切片')
    p.add_argument('--pad', type=float, default=None, help='已弃用：新版不补边重叠识别')
    args = p.parse_args()
    if args.check_srt or args.fix_srt:
        check_or_fix_srt(args.check_srt or args.fix_srt, do_fix=bool(args.fix_srt))
        return
    if args.tail_min < 0:
        p.error('--tail-min 不能为负数')
    if args.chunk is not None or args.pad is not None:
        print('提示：--chunk 和 --pad 已弃用，新版使用整段一次识别，避免人为跨片段重复。')
    for app in ('ffmpeg', 'ffprobe', 'whisper-cli'):
        if shutil.which(app) is None:
            p.error(f'找不到 {app}，请确认已安装并位于 PATH')
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
        except Exception as exc:
            print(f'❌ {video}: {exc}')
            failed.append(str(video))
    if failed:
        raise SystemExit(f'处理失败 {len(failed)} 个文件: {", ".join(failed)}')


if __name__ == '__main__':
    main()
