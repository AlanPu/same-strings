"""ffmpeg / ffprobe / soundfile 的薄封装。

所有音频处理只在这里调用外部程序，其余模块不直接拼命令行。
原则（规格 §12.5）：解码一次成 PCM，最后只编码一次，不做有损→有损二次转码。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import soundfile as sf

from . import config


# ---------------------------------------------------------------- 基础执行
def have(binary: str) -> bool:
    """检查外部命令是否存在。"""
    return shutil.which(binary) is not None


def sh(cmd: Sequence[str], what: str = "") -> str:
    """执行命令，失败时抛 BuildError 并附带 stderr 尾部。

    Args:
        cmd: 命令行参数列表。
        what: 人类可读的操作描述，用于错误信息。

    Returns:
        str: 标准错误输出（ffmpeg 的信息都在 stderr）。
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        raise config.BuildError(f"{what or cmd[0]} 失败 (exit {proc.returncode}):\n" + "\n".join(tail))
    return proc.stderr or ""


# ---------------------------------------------------------------- 探测
def ffprobe(path: Path | str) -> dict[str, Any]:
    """返回文件的 format + 首个音频流信息。"""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise config.BuildError(f"ffprobe {path} 失败: {proc.stderr[-400:]}")
    data = json.loads(proc.stdout)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})
    return {
        "duration": float(fmt.get("duration") or audio.get("duration") or 0.0),
        "codec": audio.get("codec_name"),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
        "bit_rate": int(audio.get("bit_rate") or fmt.get("bit_rate") or 0),
        "nb_frames": audio.get("nb_frames"),
        "raw": fmt,
    }


def duration(path: Path | str) -> float:
    """音频时长（秒）。"""
    return ffprobe(path)["duration"]


# ---------------------------------------------------------------- 解码 / 切割
def decode_wav(src: Path, dst: Path, sr: int = config.SR_OUTPUT) -> Path:
    """把任意音轨解码成 44.1kHz/16bit/立体声 PCM。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-vn", "-map_metadata", "-1", "-ac", "2", "-ar", str(sr),
        "-c:a", "pcm_s16le", str(dst)], f"解码 {src.name}")
    return dst


def block_dbfs(path: Path, block_sec: float = 0.01) -> tuple[np.ndarray, int, int]:
    """按块计算 RMS 电平（dBFS）。

    Args:
        path: PCM wav 路径。
        block_sec: 块长（秒）。

    Returns:
        (每块 dBFS 数组, 采样率, 总采样数)
    """
    with sf.SoundFile(str(path)) as fh:
        sr = fh.samplerate
        total = len(fh)
        bs = max(1, int(round(block_sec * sr)))
        levels = []
        for blk in fh.blocks(blocksize=bs, dtype="float32", always_2d=True):
            rms = float(np.sqrt(np.mean(np.square(blk), dtype=np.float64)))
            levels.append(20.0 * np.log10(rms + 1e-12))
    return np.asarray(levels, dtype=np.float32), sr, total


def keep_intervals(
    levels: np.ndarray,
    sr: int,
    total: int,
    block_sec: float,
    thresh_db: float = config.SILENCE_DBFS,
    head_tail_min: float = config.SILENCE_MIN_SEC,
    gap_min: float = config.GAP_MIN_SEC,
    gap_boundaries: Iterable[int] | None = None,
) -> list[tuple[int, int]]:
    """根据电平序列算出需要保留的采样区间。

    做两件事：
      1) 削掉首尾超过 head_tail_min 的静音；
      2) 在给定的分 P 拼接位置上，若存在超过 gap_min 的静音缝隙则移除。

    Args:
        levels: block_dbfs() 返回的每块电平。
        sr: 采样率。
        total: 总采样数。
        block_sec: 块长（秒）。
        thresh_db: 静音阈值。
        head_tail_min: 首尾静音最短长度（秒）。
        gap_min: 拼接缝最短长度（秒）。
        gap_boundaries: 分 P 拼接处的采样点位置列表。

    Returns:
        按时间排序、互不重叠的 (start_sample, end_sample) 列表。
    """
    loud = levels > thresh_db
    if not np.any(loud):
        return [(0, total)]
    first = int(np.argmax(loud))
    last = int(len(loud) - 1 - np.argmax(loud[::-1]))

    start = 0
    end = total
    if first * block_sec >= head_tail_min:
        start = int(first * block_sec * sr)
    if (len(loud) - 1 - last) * block_sec >= head_tail_min:
        end = int((last + 1) * block_sec * sr)

    cuts: list[tuple[int, int]] = [(start, end)]
    for boundary in sorted(gap_boundaries or []):
        # 在拼接点附近找连续的静音块
        lo = max(0, int((boundary / sr - 0.6) / block_sec))
        hi = min(len(loud), int((boundary / sr + 0.6) / block_sec) + 1)
        window = loud[lo:hi]
        b_idx = int(round(boundary / sr / block_sec)) - lo
        if b_idx <= 0 or b_idx >= len(window):
            continue
        # 向左向右扩展连续静音
        left = b_idx
        while left > 0 and not window[left - 1]:
            left -= 1
        right = b_idx
        while right < len(window) - 1 and not window[right + 1]:
            right += 1
        run_start = (lo + left) * block_sec * sr
        run_end = (lo + right + 1) * block_sec * sr
        if (run_end - run_start) / sr < gap_min:
            continue
        new: list[tuple[int, int]] = []
        for a, b in cuts:
            if run_end <= a or run_start >= b:
                new.append((a, b))
                continue
            if run_start - a > 0:
                new.append((a, min(run_start, b)))
            if b - run_end > 0:
                new.append((max(a, run_end), b))
        cuts = [(int(a), int(b)) for a, b in new if b - a > sr * 0.5]
    return cuts


def cut_intervals(src: Path, dst: Path, intervals: Sequence[tuple[int, int]]) -> Path:
    """按采样区间保留音频并拼接成一条，输出 16bit PCM。

    Args:
        src: 源 PCM wav。
        dst: 目标 PCM wav。
        intervals: keep_intervals() 的结果，采样点单位。
    """
    if len(intervals) == 1:
        a, b = intervals[0]
        chain = f"[0:a]atrim=start_sample={a}:end_sample={b},asetpts=PTS-STARTPTS[out]"
    else:
        parts, labels = [], []
        for i, (a, b) in enumerate(intervals):
            parts.append(f"[0:a]atrim=start_sample={a}:end_sample={b},asetpts=PTS-STARTPTS[p{i}]")
            labels.append(f"[p{i}]")
        chain = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(intervals)}:v=0:a=1[out]"
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-filter_complex", chain, "-map", "[out]",
        "-ac", "2", "-ar", str(config.SR_OUTPUT), "-c:a", "pcm_s16le", str(dst)],
       f"切割 {src.name}")
    return dst


# ---------------------------------------------------------------- 响度
_LOUDNORM_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def measure_loudness(path: Path, extra_input: list[str] | None = None) -> dict[str, float]:
    """用 loudnorm 的测量模式取积分响度与真峰值。

    Args:
        path: 要被测量的音频。
        extra_input: 额外的输入参数（如 ["-ss", "12", "-t", "25"]）。

    Returns:
        {"i": 积分响度 LUFS, "tp": 真峰值 dBTP, "lra": 响度范围 LU}
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    cmd += extra_input or []
    cmd += ["-i", str(path), "-af",
            f"loudnorm=I={config.TARGET_LUFS}:TP={config.TARGET_TP}:LRA=11:print_format=json",
            "-f", "null", "-"]
    stderr = sh(cmd, f"测量响度 {Path(path).name}")
    match = _LOUDNORM_RE.search(stderr)
    if not match:
        raise config.BuildError(f"loudnorm 未返回 JSON：{stderr[-300:]}")
    data = json.loads(match.group(0))
    return {"i": float(data["input_i"]), "tp": float(data["input_tp"]), "lra": float(data["input_lra"])}


def apply_gain(src: Path, dst: Path, gain_db: float) -> Path:
    """施加静态增益（不做任何动态处理，保住演奏动态）。"""
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-af", f"volume={gain_db:.3f}dB:precision=double",
        "-ac", "2", "-ar", str(config.SR_OUTPUT), "-c:a", "pcm_s16le", str(dst)],
       f"增益 {src.name}")
    return dst


# ---------------------------------------------------------------- 片段编码
def encode_clip(
    pcm: Path,
    dst: Path,
    start: float,
    dur: float,
    gain_db: float = 0.0,
    fade_ms: int = config.FADE_MS,
) -> Path:
    """从 PCM 精确切一段并一次性编码为 AAC。

    Args:
        pcm: 归一化后的 PCM wav。
        dst: 目标 m4a。
        start: 起始时间（秒）。
        dur: 时长（秒），必须精确。
        gain_db: 叠加的微调增益。
        fade_ms: 首尾淡入淡出毫秒数。
    """
    fade = fade_ms / 1000.0
    chain = (
        f"atrim=start={start:.6f}:duration={dur:.6f},asetpts=PTS-STARTPTS,"
        f"volume={gain_db:.3f}dB,"
        f"afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={dur - fade:.3f}:d={fade:.3f}"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(pcm),
        "-af", chain, "-t", f"{dur:.6f}",
        "-c:a", config.CLIP_CODEC, "-b:a", config.CLIP_BITRATE,
        "-ar", str(config.SR_OUTPUT), "-ac", "2",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", str(dst)],
       f"编码片段 {dst.name}")
    return dst


def measure_region(pcm: Path, start: float, dur: float) -> dict[str, float]:
    """测量 PCM 某一区间的响度（用于片段内均衡）。"""
    return measure_loudness(pcm, ["-ss", f"{start:.6f}", "-t", f"{dur:.6f}"])


# ---------------------------------------------------------------- 立体声对照
def stereo_ab(
    left_src: Path,
    right_src: Path,
    dst: Path,
    start_left: float,
    start_right: float,
    dur: float,
    bitrate: str = "160k",
) -> Path:
    """把两段音频做成一左一右的对照文件（左=anchor，右=目标）。

    Args:
        left_src: 左声道来源 PCM（通常是 anchor）。
        right_src: 右声道来源 PCM。
        dst: 输出 m4a。
        start_left: 左声道起始秒。
        start_right: 右声道起始秒。
        dur: 时长秒。
        bitrate: 编码码率（对照用，稍高一些便于听细节）。
    """
    chain = (
        f"[0:a]atrim=start={start_left:.6f}:duration={dur:.6f},asetpts=PTS-STARTPTS,"
        f"pan=mono|c0=0.5*c0+0.5*c1[l];"
        f"[1:a]atrim=start={start_right:.6f}:duration={dur:.6f},asetpts=PTS-STARTPTS,"
        f"pan=mono|c0=0.5*c0+0.5*c1[r];"
        f"[l][r]amerge=inputs=2[out]"
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(left_src), "-i", str(right_src),
        "-filter_complex", chain, "-map", "[out]",
        "-c:a", "aac", "-b:a", bitrate, "-ar", str(config.SR_OUTPUT), "-ac", "2",
        "-map_metadata", "-1", str(dst)],
       f"对照音频 {dst.name}")
    return dst
