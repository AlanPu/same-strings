"""S0 — 素材登记与抓取。

产出 work/src/<id>.m4a（或 .webm，取决于源格式）+ work/src/<id>.json（元数据）。
分 P 视频按 project.json 的 parts 顺序逐 P 下载，再用 concat demuxer 无损拼接；
拼接参数不一致时退化为 PCM 拼接再编码一次，并在日志里留 warn（不静默降级）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import certifi

from . import audio, config
from .config import BuildError, Performer, Project
from .logbook import Log

YTDLP = "yt-dlp"
MOVEMENT_RE = re.compile(r"第\s*([一二三四1-4])\s*乐章")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _env() -> dict[str, str]:
    """带上 certifi 的 CA 路径，规避 macOS 上的 CERTIFICATE_VERIFY_FAILED。"""
    env = dict(os.environ)
    env["SSL_CERT_FILE"] = certifi.where()
    env.pop("LC_ALL", None)
    return env


def ytdlp_json(url: str, what: str) -> dict[str, Any]:
    """取单条视频/P 的完整元数据（不下载）。"""
    proc = subprocess.run(
        [YTDLP, "-J", "--skip-download", "--no-warnings", url],
        capture_output=True, text=True, env=_env(), timeout=180,
    )
    if proc.returncode != 0:
        raise BuildError(f"{what}: yt-dlp 取元数据失败\n{proc.stderr[-500:]}")
    return json.loads(proc.stdout)


def ytdlp_download(url: str, template: Path, what: str, retries: int = 3) -> Path:
    """下载 bestaudio 到指定路径模板，失败重试。

    Args:
        url: 视频地址（含 ?p=N）。
        template: yt-dlp 输出模板，含 %(ext)s。
        what: 用于日志的描述。
        retries: 重试次数（规格 §S0 要求 3 次）。

    Returns:
        实际落盘的文件路径。
    """
    last = ""
    for attempt in range(1, retries + 1):
        proc = subprocess.run(
            [YTDLP, "-f", "bestaudio/best", "--no-playlist", "--no-warnings",
             "--newline", "-o", str(template), url],
            capture_output=True, text=True, env=_env(), timeout=1800,
        )
        if proc.returncode == 0:
            # 从输出里找落盘路径
            for line in reversed(proc.stdout.splitlines()):
                m = re.search(r"(?:Destination|Merging formats into):\s*(.+)$", line)
                if m:
                    path = Path(m.group(1).strip().strip('"'))
                    if path.exists():
                        return path
            matches = sorted(template.parent.glob(template.name.replace("%(ext)s", "*")))
            if matches:
                return matches[0]
            last = "命令成功但找不到输出文件"
        else:
            last = proc.stderr[-400:]
            time.sleep(2 * attempt)
        print(f"    下载重试 {attempt}/{retries}: {what}")
    raise BuildError(f"{what}: 下载失败（已重试 {retries} 次）\n{last}")


def parse_movements(part_titles: list[tuple[str, float, float]], total: float) -> list[dict[str, Any]]:
    """从分 P 标题推导乐章边界。

    Args:
        part_titles: [(标题, 起始秒, 结束秒), ...]
        total: 总时长（秒）。

    Returns:
        [{"index": 1, "label": "第一乐章", "start": 0.0, "end": 834.2}, ...]；
        标题里没有「第 N 乐章」时返回空列表。
    """
    found: list[tuple[int, float, float]] = []
    for title, start, end in part_titles:
        m = MOVEMENT_RE.search(title)
        if not m:
            continue
        idx = CN_NUM.get(m.group(1))
        if idx:
            found.append((idx, start, end))
    if len(found) < 2:
        return []
    found.sort(key=lambda x: x[0])
    return [
        {"index": idx, "label": f"第{'一二三四'[idx - 1]}乐章", "start": round(start, 3), "end": round(end, 3)}
        for idx, start, end in found
    ]


def fetch_one(proj: Project, perf: Performer, log: Log, force: bool) -> dict[str, Any]:
    """抓取一位演奏家的全部分 P 并拼接成一条音轨。

    Returns:
        写入 work/src/<id>.json 的元数据字典。
    """
    meta_path = config.DIR_SRC / f"{perf.id}.json"
    if meta_path.exists() and not force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if Path(meta.get("file", "")).exists():
            log.line(f"  {perf.id}: 已有素材，跳过（{meta['duration']:.2f}s）")
            return meta

    part_files: list[Path] = []
    part_meta: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []

    for part in perf.parts:
        url = f"https://www.bilibili.com/video/{perf.bvid}?p={part}"
        info = ytdlp_json(url, f"{perf.id} p{part}")
        target = config.DIR_SRC / f"{perf.id}.p{part}.%(ext)s"
        path = ytdlp_download(url, target, f"{perf.id} p{part}")
        probe = audio.ffprobe(path)
        if part == perf.parts[0] and info.get("chapters"):
            chapters = [
                {"title": c.get("title", ""), "start": float(c.get("start_time", 0)),
                 "end": float(c.get("end_time", 0))}
                for c in info["chapters"]
            ]
        part_files.append(path)
        part_meta.append({
            "part": part, "file": str(path), "duration": probe["duration"],
            "codec": probe["codec"], "sample_rate": probe["sample_rate"],
            "channels": probe["channels"], "title": info.get("title", ""),
        })
        log.line(f"  {perf.id} p{part}: {probe['duration']:.3f}s · {probe['codec']} "
                 f"{probe['sample_rate']}Hz {probe['channels']}ch {probe['bit_rate'] // 1000}kbps")

    # ---- 拼接 ----------------------------------------------------
    expected = sum(p["duration"] for p in part_meta)
    if len(part_files) == 1:
        final = part_files[0]
        log.line(f"  {perf.id}: 单轨，无需拼接")
    else:
        exts = {p.suffix.lower() for p in part_files}
        codecs = {p["codec"] for p in part_meta}
        can_copy = len(exts) == 1 and len(codecs) == 1
        ext = part_files[0].suffix.lower()
        final = config.DIR_SRC / f"{perf.id}.spliced{ext}"
        if can_copy:
            listfile = config.DIR_SRC / f"{perf.id}.concat.txt"
            listfile.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in part_files),
                                encoding="utf-8")
            audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                      "-safe", "0", "-i", str(listfile), "-c", "copy",
                      "-map_metadata", "-1", str(final)], f"{perf.id} 无损拼接")
        else:
            log.warn(f"{perf.id}: 各 P 编码不一致（{codecs}/{exts}），退化为 PCM 拼接后重编码")
            tmp = config.DIR_PCM / f"{perf.id}.spjoin.wav"
            listfile = config.DIR_SRC / f"{perf.id}.concat.txt"
            listfile.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in part_files),
                                encoding="utf-8")
            audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                      "-safe", "0", "-i", str(listfile), "-ac", "2",
                      "-ar", str(config.SR_OUTPUT), "-c:a", "pcm_s16le", str(tmp)],
                     f"{perf.id} PCM 拼接")
            final = config.DIR_SRC / f"{perf.id}.m4a"
            audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp),
                      "-c:a", "aac", "-b:a", "256k", "-ar", str(config.SR_OUTPUT), "-ac", "2",
                      str(final)], f"{perf.id} 重编码")
            tmp.unlink(missing_ok=True)

    total = audio.duration(final)
    if abs(total - expected) > 1.0:
        raise BuildError(f"{perf.id}: 拼接后 {total:.2f}s 与分 P 之和 {expected:.2f}s 相差 "
                         f"{abs(total - expected):.2f}s > 1s（规格 S0 验收）")

    # ---- 乐章边界 -------------------------------------------------
    cursor = 0.0
    timeline = []
    for pm in part_meta:
        timeline.append((pm["title"], cursor, cursor + pm["duration"]))
        cursor += pm["duration"]
    movements = parse_movements(timeline, cursor)
    if not movements and chapters:
        movements = [
            {"index": i + 1, "label": c["title"][:16] or f"第{i + 1}段",
             "start": c["start"], "end": c["end"]}
            for i, c in enumerate(chapters)
        ]

    meta = {
        "performerId": perf.id,
        "file": str(final),
        "duration": total,
        "expectedDuration": round(expected, 3),
        "parts": part_meta,
        "partBoundaries": [round(t[1], 3) for t in timeline[1:]],
        "chapters": chapters,
        "movements": movements,
        "sourceTitle": part_meta[0]["title"],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.line(f"  {perf.id}: 完成 {total:.3f}s（期望 {expected:.3f}s）"
             f"{' · 乐章边界 ' + str(len(movements)) + ' 处' if movements else ''}")
    return meta


def run(proj: Project, log: Log, force: bool = False) -> dict[str, dict[str, Any]]:
    """抓取全部演奏家。

    Returns:
        {performerId: meta}
    """
    log.stage(f"S0 素材登记与抓取（{proj.n} 位演奏家）")
    metas: dict[str, dict[str, Any]] = {}
    for perf in proj.performers:
        log.line(f"-- {perf.id} / {perf.display} / {perf.bvid} / parts={perf.parts}")
        metas[perf.id] = fetch_one(proj, perf, log, force)

    rows = [[pid, f"{m['duration']:.2f}", f"{m['expectedDuration']:.2f}",
             "OK" if abs(m["duration"] - m["expectedDuration"]) <= 1.0 else "FAIL"] for pid, m in metas.items()]
    log.table(["id", "实测(s)", "分P之和(s)", "差值判据<1s"], rows)
    log.metric("1 抓取完整性", f"最大误差 {max(abs(m['duration'] - m['expectedDuration']) for m in metas.values()):.3f}s",
               "< 1s", all(abs(m["duration"] - m["expectedDuration"]) <= 1.0 for m in metas.values()))
    return metas
