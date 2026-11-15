"""下载 8 位演奏家的原始视频，供参照页（dist/ref）使用。

画质策略：先问 yt-dlp「这个源最高能给到多少」，再按 `-S "res:1080,vcodec:h264"`
排序取格式。两点必须说清楚：

1. **为什么不用 `bestvideo[ext=mp4]`**：`best` 系列按「码率优先」排，而 B 站的
   HEVC 流码率低、分辨率高，`[ext=mp4]` 又会同时命中 hevc 和 avc1，结果经常拿到
   HEVC 或者干脆拿到低分辨率的 avc1（实测 fischer 明明有 1080p 的 avc1 30080，
   默认排序却给了 720p 的 30064）。`-S res:1080` 明确按分辨率排，才是对的。
2. **为什么锁定 avc1（H.264）**：iPad Safari 播放 HEVC 要看机型和系统版本
   （需要 hardware decoder + 特定 profile），AV1 更晚。同一批里混三种编码没有任何
   好处，统一 avc1 最省心。`vcodec:h264` 是 yt-dlp 的模糊偏好写法，命中 avc1。

`res:1080` 不是「限制到 1080」，而是「偏好最接近 1080 的」——B 站大会员 4K 源存在时
也不会去下 4K，因为参照页只是在 iPad 上看乐句，4K 纯属浪费空间。

用法：
    python tools/fetch_ref.py            # 已有且画质达标的跳过，不达标的重下
    python tools/fetch_ref.py --force    # 全部重下
    python tools/fetch_ref.py --dry-run  # 只打印每个源的目标画质与本地现状
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssm.config import load_project  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "work" / "ref" / "video"

# 排序参数：分辨率优先（目标 1080），其次编码取 h264。
SORT = "res:1080,vcodec:h264"
# 格式表达式：优先「视频+音频」，源只有合流文件时退到 best。
FMT = "bestvideo+bestaudio/best"
# yt-dlp 中间文件的名字形状：<名字>.f<格式号>.<扩展名>
LEFTOVER = re.compile(r"\.f\d+\.")


def probe(path: Path) -> tuple[str, int, int] | None:
    """读取视频文件的编码与分辨率。

    Args:
        path: 视频文件路径。

    Returns:
        ``(codec, width, height)``；文件不存在或 ffprobe 失败时返回 ``None``。
    """
    if not path.exists():
        return None
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    parts = (proc.stdout or "").strip().split(",")
    if len(parts) < 3:
        return None
    codec, w, h = parts[0], parts[1], parts[2]
    try:
        return codec, int(w), int(h)
    except ValueError:
        return None


def target_height(bvid: str) -> int:
    """问 yt-dlp：按当前排序策略，这个源会选到多高。

    Args:
        bvid: B 站视频号。

    Returns:
        目标视频高度（像素）；探测失败返回 0（此时调用方不做「达标跳过」）。
    """
    cmd = ["yt-dlp", "-S", SORT, "-f", FMT, "--no-playlist", "--no-warnings",
           "--simulate", "--print", "%(height)s",
           f"https://www.bilibili.com/video/{bvid}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return 0
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)
    return 0


def clean(pid: str) -> None:
    """清掉该演奏家的全部旧产物与断点残留。

    包括 yt-dlp 在合并失败时留下的 ``<pid>.f30077.mp4`` 这类带 format id 的文件，
    以及 ``.part`` / ``.ytdl`` 临时文件——这些不删会干扰后续 glob 统计。

    Args:
        pid: 演奏家 id。
    """
    for pat in (f"{pid}.mp4", f"{pid}.p*.mp4",
                # `f*` / `p*.f*` 一口气覆盖 yt-dlp 的各种中间产物：合并失败时留下的
                # <pid>.f30077.mp4、<pid>.p1.f30064.mp4、只下到音轨的 <pid>.f30280.m4a、
                # 以及断点 <pid>.f30080.mp4.part
                f"{pid}.f*", f"{pid}.p*.f*", f"{pid}*.part", f"{pid}*.ytdl"):
        for f in OUT.glob(pat):
            f.unlink()
            print(f"    删除 {f.name}", flush=True)


def run(pid: str, bvid: str, parts: list[int], force: bool, dry: bool) -> None:
    """下载一位演奏家的视频。

    Args:
        pid: 演奏家 id。
        bvid: B 站视频号。
        parts: 需要下载的分 P 序号；只有一个且为 1 时按单文件处理。
        force: 是否无条件重下。
        dry: 只打印计划，不实际下载。
    """
    multi = len(parts) > 1
    want = target_height(bvid)
    existing = sorted(OUT.glob(f"{pid}.mp4" if not multi else f"{pid}.p*.mp4"))
    if multi and not existing:
        # 分 P 视频 yt-dlp 也可能直接叫 <pid>.mp4（单 P 时），一并算进来
        existing = sorted(OUT.glob(f"{pid}.mp4"))
    heights = [(f, probe(f)) for f in existing]
    ok = (not force and len(existing) == len(parts) and want > 0
          and all(info and info[2] >= want for _, info in heights))

    now = " ".join(f"{f.name}={info[2]}p" if info else f"{f.name}=?" for f, info in heights) or "无"
    print(f"  {pid}: 目标 {want or '?'}p｜本地 {now}", flush=True)
    if ok:
        print(f"  {pid}: 已达标，跳过", flush=True)
        return
    if dry:
        print(f"  {pid}: 需要重下（dry-run，不执行）", flush=True)
        return

    clean(pid)
    tmpl = (OUT / f"{pid}.p%(playlist_index)s.%(ext)s") if multi else (OUT / f"{pid}.%(ext)s")
    cmd = ["yt-dlp", "-f", FMT, "-S", SORT, "--merge-output-format", "mp4",
           "--newline", "--no-warnings", "-o", str(tmpl)]
    if multi:
        # 注意：`-p` 是 --password 的别名，不是分 P 选择，用它会让 yt-dlp
        # 报 "account username missing"。分 P 必须用 --playlist-items。
        cmd += ["--playlist-items", ",".join(str(x) for x in parts)]
    else:
        cmd += ["--no-playlist"]
    cmd.append(f"https://www.bilibili.com/video/{bvid}")
    print(f"  {pid}: 下载中 …", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  {pid}: 失败\n{(proc.stderr or '')[-400:]}", flush=True)
        return

    got = sorted(OUT.glob(f"{pid}*.mp4"))
    # yt-dlp 合并成功后会自动删掉中间文件；没删说明合并那步失败了。
    # 判据必须是 `.f<格式号>.` 这个精确形状 —— 曾经写成「文件名含不止一个点」，
    # 结果把分 P 的正常产物 bell.p1.mp4 当成残留删了。
    leftovers = [f for f in got if LEFTOVER.search(f.name)]
    for f in leftovers:
        f.unlink()
        print(f"    清理未合并的 {f.name}", flush=True)
    got = sorted(OUT.glob(f"{pid}*.mp4"))
    size = sum(f.stat().st_size for f in got) / 1024 / 1024
    detail = " ".join(
        f"{f.name}={info[1]}x{info[2]}{'' if info[0] == 'h264' else '!' + info[0]}"
        for f in got if (info := probe(f)))
    print(f"  {pid}: {len(got)} 个文件，{size:.0f} MB｜{detail}", flush=True)


def main() -> int:
    """入口。"""
    proj = load_project()
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"下载到 {OUT}", flush=True)
    for p in proj.performers:
        run(p.id, p.bvid, p.parts, force, dry)
    print("\n汇总：", flush=True)
    for p in proj.performers:
        got = sorted(OUT.glob(f"{p.id}*.mp4"))
        info = [probe(f) for f in got]
        hs = [i[2] for i in info if i]
        print(f"  {p.id:<10} {len(got)} 文件  {min(hs) if hs else 0}p"
              f"{'  ⚠ 编码非 h264' if any(i[0] != 'h264' for i in info if i) else ''}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
