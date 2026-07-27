import asyncio
import os
import re
import sys
import time
import subprocess
import platform
import shutil
from datetime import datetime
import edge_tts
from mutagen.mp3 import MP3
# 引入语言检测库
from langdetect import detect, DetectorFactory

# 设置随机种子以保证语言检测的稳定性
DetectorFactory.seed = 0

# ================= 配置区 =================
OUTPUT_DIR = "./"  # 所有生成的学习资料将自动存放在此文件夹

# 核心：语言与微软TTS语音映射表（你可以根据需要在此自行增减或修改声音）
# 格式为 '语言代码': '微软TTS声音名称'
VOICE_MAPPING = {
    'zh': "zh-CN-YunjianNeural",    # 中文 (如果你喜欢云希可以换成 zh-CN-YunxiNeural)
    'en': "en-US-RyanNeural",       # 英语
    'fr': "fr-FR-DeniseNeural",     # 法语
    'de': "de-DE-KillianNeural",    # 德语
    'ja': "ja-JP-NanamiNeural",     # 日语
    'ko': "ko-KR-SunHiNeural",      # 韩语
    'es': "es-ES-AlvaroNeural",     # 西班牙语
    'el': "el-GR-NestorasNeural",   # 希腊语 (注意: edge-tts没有古希腊语，此处用现代希腊语兜底)
}

DEFAULT_VOICE = "en-US-RyanNeural" # 无法识别或不支持的语言时，默认使用的兜底声音
# ==========================================

def get_clipboard_text():
    """跨平台获取剪贴板文本"""
    system = platform.system()
    try:
        if system == 'Darwin':
            return subprocess.check_output(['pbpaste'], text=True, encoding='utf-8').strip()
        elif system == 'Windows':
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return text.strip()
        else:
            return ""
    except Exception as e:
        print(f"⚠️ 读取剪贴板失败: {e}")
        return ""

def format_time(seconds):
    """将秒数格式化为 SRT 时间戳格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millisecs = int((seconds * 1000) % 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"

def split_text_into_sentences(text, ideal_length=35):
    """按标点和换行初步切分文本（多语言通用简化版）"""
    # 针对多语言，主要依赖换行和中英文末尾标点进行切分
    lines = text.split('\n')
    valid_sentences = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 使用常见的断句标点切分：. ! ? 。 ！ ？ ; ；
        chunks = re.split(r'([.!?.。！？;；])', line)
        current = ""
        for i in range(0, len(chunks)-1, 2):
            sentence = chunks[i] + chunks[i+1]
            if len(current) + len(sentence) < ideal_length:
                current += " " + sentence if current else sentence
            else:
                if current: valid_sentences.append(current.strip())
                current = sentence
        if len(chunks) % 2 != 0:
            current += " " + chunks[-1] if current else chunks[-1]
        if current.strip():
            valid_sentences.append(current.strip())
            
    return valid_sentences

def open_editor_and_wait(filepath):
    """跨平台调用系统默认编辑器"""
    print(f"\n" + "="*50)
    print(f"✍️  已为您自动打开文本源文件！")
    print(f"👉 1. 请在窗口中修改字幕（一行代表一句字幕，想合并就删掉换行连在一起）。")
    print(f"👉 2. 修改完成后，请务必按下【保存】(Ctrl+S / Cmd+S)。")
    print(f"👉 3. 切回本终端窗口，按下【回车键】继续生成。")
    print("="*50 + "\n")
    
    system = platform.system()
    try:
        if system == 'Windows':
            subprocess.run(['notepad', filepath])
            return 
        elif system == 'Darwin':
            subprocess.run(['open', '-e', filepath])
        else:
            subprocess.run(['nano', filepath])
    except Exception as e:
        print(f"⚠️ 自动打开编辑器失败: {e}。请手动打开 {filepath} 修改。")
        
    input("▶️  我已保存完毕，按【回车键】立即开始生成...")

def extract_text_from_srt(srt_path):
    """从已有的 SRT 文件中提取纯文本（忽略序号和时间轴）"""
    sentences = []
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()
    
    blocks = re.split(r'\n\n+', content)
    for block in blocks:
        lines = block.split('\n')
        if len(lines) >= 3:
            text = " ".join(lines[2:]).strip()
            if text:
                sentences.append(text)
    return sentences

def process_clipboard_and_edit(txt_path):
    """处理全新剪贴板任务的工作流"""
    text = get_clipboard_text()
    if not text:
        print("❌ 剪贴板是空的！请先复制文本。")
        sys.exit(1)
    print("📥 成功从剪贴板获取文本！正在进行初步切分...")
    sentences = split_text_into_sentences(text)
    
    with open(txt_path, "w", encoding="utf-8") as f:
        for s in sentences:
            f.write(s + "\n")
            
    open_editor_and_wait(txt_path)
    
    with open(txt_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def check_ffmpeg():
    """检查是否安装了 ffmpeg"""
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False

def get_voice_for_text(text):
    """核心函数：智能识别单句文本的语言并匹配声音"""
    # 过滤掉仅有标点和数字的文本
    clean_text = re.sub(r'[^\w\s\u4e00-\u9fa5]', '', text).strip()
    if not clean_text:
        return DEFAULT_VOICE, "unknown"
        
    try:
        lang = detect(text)
        # 微软TTS可能需要微调映射，比如 zh-cn, zh-tw 统一转成 zh
        lang_short = lang.split('-')[0] 
        
        if lang_short in VOICE_MAPPING:
            return VOICE_MAPPING[lang_short], lang_short
        else:
            return DEFAULT_VOICE, f"{lang_short}(未配置,已兜底)"
    except Exception:
        # 识别失败时优雅降级
        return DEFAULT_VOICE, "fallback"

async def main():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    now = datetime.now()
    base_name = f"{now.month}.{now.day}_auto" # 文件名后缀改为auto
    
    txt_path = os.path.join(OUTPUT_DIR, f"{base_name}.txt")
    mp3_path = os.path.join(OUTPUT_DIR, f"{base_name}.mp3")
    mp4_path = os.path.join(OUTPUT_DIR, f"{base_name}.mp4")
    srt_path = os.path.join(OUTPUT_DIR, f"{base_name}.srt")
    temp_chunk_dir = os.path.join(OUTPUT_DIR, f"chunks_{base_name}")

    if os.path.exists(srt_path):
        print(f"\n📁 检测到今天已存在字幕文件：{base_name}.srt")
        choice = input("👉 直接按【回车】读取该 SRT 的文本，重新生成音频和视频并覆盖。\n👉 或输入【N】读取剪贴板的全新文本。\n请选择: ").strip().lower()
        
        if choice != 'n':
            print("\n📥 正在从现有 SRT 中提取您修改过的字幕文本...")
            final_sentences = extract_text_from_srt(srt_path)
        else:
            final_sentences = process_clipboard_and_edit(txt_path)
    else:
        final_sentences = process_clipboard_and_edit(txt_path)
        
    total_sentences = len(final_sentences)
    if total_sentences == 0:
        print("❌ 文本内容为空，程序退出。")
        sys.exit(1)
        
    print(f"\n✅ 文本确认完毕！共 {total_sentences} 句字幕。开始智能识别语言并生成语音...")

    if not os.path.exists(temp_chunk_dir):
        os.makedirs(temp_chunk_dir)
    else:
        shutil.rmtree(temp_chunk_dir)
        os.makedirs(temp_chunk_dir)

    chunk_files = []
    metadata = []

    for i, sentence in enumerate(final_sentences):
        chunk_path = os.path.join(temp_chunk_dir, f"chunk_{i:04d}.mp3")
        
        # 【新增控制逻辑】逐句动态动态检测语种并获取对应的Voice
        target_voice, detected_lang = get_voice_for_text(sentence)
        print(f"🎙️ [{i+1}/{total_sentences}] 识别语种: [{detected_lang}] ➡️ 使用声音: {target_voice.split('-')[2]}")
        
        max_retries = 5
        success = False
        
        for attempt in range(max_retries):
            try:
                # 传入动态获取到的声音
                communicate = edge_tts.Communicate(sentence, target_voice)
                await communicate.save(chunk_path)
                success = True
                break
            except edge_tts.exceptions.NoAudioReceived:
                print(f"⚠️ 第 {i+1} 句微软TTS拒绝发音，已跳过。")
                break
            except Exception as e:
                if "503" in str(e) or "HandshakeError" in str(e):
                    wait_time = 5 * (attempt + 1)
                    print(f"⚠️ 网络限流，等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)
                else:
                    print(f"❌ 发生未知错误: {e}")
                    break
                    
        if success:
            try:
                duration = MP3(chunk_path).info.length
                metadata.append({"text": sentence, "duration": duration})
                chunk_files.append(chunk_path)
            except Exception as e:
                print(f"⚠️ 无法读取音频时长: {e}")

        await asyncio.sleep(0.4) 

    if chunk_files:
        print("\n📦 正在合并音频流...")
        with open(mp3_path, "wb") as outfile:
            for chunk_path in chunk_files:
                with open(chunk_path, "rb") as infile:
                    outfile.write(infile.read())
                    
        print("📝 正在生成全新的 SRT 字幕文件...")
        current_time = 0.0
        with open(srt_path, "w", encoding="utf-8") as srt_file:
            for idx, item in enumerate(metadata):
                start_time = current_time
                end_time = current_time + item["duration"]
                srt_file.write(f"{idx + 1}\n")
                srt_file.write(f"{format_time(start_time)} --> {format_time(end_time)}\n")
                srt_file.write(f"{item['text']}\n\n")
                current_time = end_time

        if check_ffmpeg():
            print("🎬 正在调用 FFmpeg 生成纯黑背景视频 (.mp4)...")
            ffmpeg_cmd = [
                'ffmpeg', '-y',
                '-f', 'lavfi', '-i', 'color=c=black:s=1280x720:r=24',
                '-i', mp3_path,
                '-c:v', 'libx264', '-tune', 'stillimage',
                '-c:a', 'aac', '-b:a', '192k',
                '-pix_fmt', 'yuv420p',
                '-shortest',
                mp4_path
            ]
            try:
                subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"✅ 视频生成成功：{mp4_path}")
            except Exception as e:
                print(f"❌ 视频生成失败: {e}")
        else:
            print("⚠️ 警告：未检测到 ffmpeg，跳过视频合成。仅保留 mp3 录音文件。")

        print("🧹 正在清理临时文件...")
        if os.path.exists(temp_chunk_dir):
            shutil.rmtree(temp_chunk_dir)
        if os.path.exists(mp3_path) and os.path.exists(mp4_path):
            os.remove(mp3_path)
        if os.path.exists(txt_path):
            os.remove(txt_path)

        print(f"\n🎉 大功告成！\n▶️ 最终视频: {mp4_path}\n▶️ 最终字幕: {srt_path}")
        
        try:
            abs_dir = os.path.abspath(OUTPUT_DIR)
            if platform.system() == "Windows":
                os.startfile(abs_dir)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", abs_dir])
        except:
            pass
            
    else:
        print("❌ 生成失败，未获取到有效音频。")

if __name__ == "__main__":
    asyncio.run(main())