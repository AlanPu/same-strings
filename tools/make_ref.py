"""生成「参照播放页」：8 位演奏家的完整视频 + 同一乐句对比。

和练习器是两条不同的产物，目的也不同：
  · 练习器（dist/site）—— 考你，30 秒盲听四选一；
  · 参照页（dist/ref）—— 教你，完整看/听一个人的处理，或者把同一句话在 8 个人之间来回切换。

参照页的两块内容：
  1. 完整听：work/ref/video/*.mp4（原视频，多 P 的已拼接），逐个播放；
  2. 同一句对比：从 work/pcm/*.wav 转出的 96k 音轨（已裁头、已统一响度），
     配合 S2 的 warp 表把「锚点第 t 秒」换算到每个人的音轨位置。

产物：
  dist/ref/index.html
  dist/ref/video/<pid>.mp4
  dist/ref/audio/<pid>.m4a
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssm import audio, config  # noqa: E402
from ssm.config import BuildError, load_project  # noqa: E402

REF_DIR = config.ROOT / "work" / "ref"
VIDEO_SRC = REF_DIR / "video"
OUT = config.DIR_DIST / "ref"
COMPARE_BITRATE = "96k"


def find_video(pid: str) -> tuple[Path | None, list[Path]]:
    """找某位演奏家的视频：单个 mp4，或按 P 分开的若干段。

    Returns:
        (单个文件或 None, 分 P 文件列表)
    """
    single = VIDEO_SRC / f"{pid}.mp4"
    if single.exists():
        return single, []
    parts = sorted(VIDEO_SRC.glob(f"{pid}.p*.mp4"),
                   key=lambda p: int("".join(c for c in p.stem.split(".")[-1] if c.isdigit()) or 0))
    return None, parts


def concat_parts(pid: str, parts: list[Path], dst: Path) -> None:
    """把分 P 的视频按顺序拼成一个（流复制，不重编码）。"""
    if len(parts) == 1:
        shutil.copyfile(parts[0], dst)
        return
    listfile = REF_DIR / f"_{pid}.concat.txt"
    listfile.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
                        encoding="utf-8")
    audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
              "-f", "concat", "-safe", "0", "-i", str(listfile),
              "-c", "copy", str(dst)], f"拼接 {pid} 的分 P")
    listfile.unlink(missing_ok=True)


def music_start(path: Path, probe_sec: float = 120.0) -> float:
    """估出视频里音乐真正开始的时刻。

    为什么需要：这些录像的开头不都是有声的。哈德利希前 20 秒在 -45…-53 dBFS，
    穆特前 8 秒是数字静音、到第 36 秒才有乐声，文格洛夫要到第 31 秒。
    用户点开听到一片安静，第一反应就是「这个没声音」。

    做法：只看前 ``probe_sec`` 秒，以 0.5 s 为窗算 RMS。把这段里的 95 百分位
    当作「音乐电平」，阈值取它往下 20 dB —— 掌声和环境噪声比音乐低太多，
    过不了线。再从第一个「连续 4 个窗口超过阈值」的位置往回退 2 秒作为缓冲，
    不要正好切在起音上。

    Args:
        path: 视频文件。
        probe_sec: 只分析开头多少秒（全曲解码没必要，也慢）。

    Returns:
        音乐起点（秒）；判断不出来或本来就一开就有声时返回 0.0。
    """
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "head.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-t", str(probe_sec),
             "-ac", "1", "-ar", "8000", str(wav)],
            check=True, capture_output=True)
        x, sr = sf.read(wav, dtype="float32")
    win = int(sr * 0.5)
    if len(x) < win * 8:
        return 0.0
    rms = np.array([float(np.sqrt(np.mean(x[i:i + win] ** 2)))
                    for i in range(0, len(x) - win + 1, win)])
    db = 20 * np.log10(np.maximum(rms, 1e-9))
    threshold = float(np.percentile(db, 95)) - 20.0
    above = db > threshold
    for i in range(len(above) - 3):
        if above[i] and above[i + 1] and above[i + 2] and above[i + 3]:
            onset = max(0.0, i * 0.5 - 2.0)
            return 0.0 if onset < 3.0 else round(onset, 1)
    return 0.0


def main() -> int:
    proj = load_project()
    log = lambda m: print(m, flush=True)  # noqa: E731

    (OUT / "video").mkdir(parents=True, exist_ok=True)
    (OUT / "audio").mkdir(parents=True, exist_ok=True)

    # ---- 视频 ---------------------------------------------------
    log("== 视频 ==")
    vid_sec: dict[str, float] = {}
    start_at: dict[str, float] = {}
    for p in proj.performers:
        single, parts = find_video(p.id)
        dst = OUT / "video" / f"{p.id}.mp4"
        if single is None and not parts:
            log(f"  {p.id}: 缺视频，先跑 tools/fetch_ref.py")
            return 2
        tmp = REF_DIR / f"_{p.id}.joined.mp4"
        if single:
            src = single
        else:
            log(f"  {p.id}: 拼接 {len(parts)} 个分 P")
            concat_parts(p.id, parts, tmp)
            src = tmp
        # moov 放到文件头，否则 iOS 上拖动进度条要等整个文件
        audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                  "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)],
                 f"重封装 {p.id}")
        if tmp.exists():
            tmp.unlink()
        mb = dst.stat().st_size / 1024 / 1024
        # 每位演奏家的演奏速度不同，视频长度也就不一样（最快的和最慢的差好几分钟），
        # 所以列表里的时长必须实测，不能统一填锚点的全长。
        vid_sec[p.id] = audio.duration(dst)
        start_at[p.id] = music_start(dst)
        note = f"，开场静音 {start_at[p.id]:.0f}s" if start_at[p.id] > 0 else ""
        log(f"  {p.id}: {mb:.0f} MB, {vid_sec[p.id] / 60:.1f} 分{note}")

    # ---- 对比音轨 -----------------------------------------------
    log("== 对比音轨 ==")
    for p in proj.performers:
        pcm = config.DIR_PCM / f"{p.id}.wav"
        if not pcm.exists():
            raise BuildError(f"缺少 {pcm}")
        dst = OUT / "audio" / f"{p.id}.m4a"
        audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                  "-i", str(pcm), "-c:a", "aac", "-b:a", COMPARE_BITRATE,
                  "-movflags", "+faststart", str(dst)], f"转码 {p.id}")
        log(f"  {p.id}: {dst.stat().st_size / 1024 / 1024:.0f} MB")

    # ---- 数据 ---------------------------------------------------
    anchor_pcm = config.DIR_PCM / f"{proj.anchor}.wav"
    duration = audio.duration(anchor_pcm)
    warp: dict[str, dict] = {}
    for p in proj.performers:
        if p.id == proj.anchor:
            continue                       # 锚点自己就是恒等映射，前端不必给表
        w = json.loads((config.DIR_WARP / f"{p.id}.json").read_text(encoding="utf-8"))
        warp[p.id] = {"map": [round(float(x), 3) for x in w["map"]]}

    movements = json.loads((config.WORK / "movements.json").read_text(encoding="utf-8"))
    data = {
        "work": proj.title,
        "duration": round(duration, 3),
        "warpStep": config.WARP_STEP,
        "performers": [{"id": p.id, "display": p.display, "fullName": p.fullName,
                        "videoSec": round(vid_sec.get(p.id, 0.0), 1),
                        "musicStart": start_at.get(p.id, 0.0)}
                       for p in proj.performers],
        "movements": [{"start": m["start"], "end": m["end"], "label": m["label"] or "全曲"}
                      for m in movements],
        "warp": warp,
    }

    tpl = (config.WEB / "ref.html").read_text(encoding="utf-8")
    if "__DATA__" not in tpl:
        raise BuildError("web/ref.html 缺少 __DATA__ 占位")
    html = tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    out = OUT / "index.html"
    out.write_text(html, encoding="utf-8")

    log("== 产物 ==")
    log(f"  {out}（{out.stat().st_size / 1024:.0f} KB）")
    log(f"  {OUT / 'video'}（{sum(1 for _ in (OUT / 'video').glob('*.mp4'))} 个）")
    log(f"  {OUT / 'audio'}（{sum(1 for _ in (OUT / 'audio').glob('*.m4a'))} 个）")
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    log(f"  合计 {total / 1024 / 1024 / 1024:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
