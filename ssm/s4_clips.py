"""S4 — 切片与编码。

从 S1 的 PCM 直接切（先切再压，绝不「压一次→切→再压」）。
每段内部做一次响度对齐：同一题的多个片段必须听不出电平差，否则响度就成了答题捷径。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import audio, config, warp as warpmod
from .config import BuildError, Project
from .logbook import Log

GAIN_MIN = -15.0        # 单片段最大衰减
GAIN_MAX = 9.0          # 单片段最大提升（再大就会把源噪声抬起来）


def _write_both(clips: dict, segments: list[dict]) -> None:
    """同时写 clips.json 与 segments.json：后者补上每个演奏家的 clipId。

    两个文件内容一致（clips.json 多索引维度），避免下游各读一份而出现「某个文件里
    clips 是空的」这类不一致（验收第 15 项就踩过这个坑）。
    """
    payload = json.dumps({"clips": clips, "segments": segments}, ensure_ascii=False, indent=2)
    (config.WORK / "clips.json").write_text(payload, encoding="utf-8")
    seg_path = config.WORK / "segments.json"
    if seg_path.exists():
        old = json.loads(seg_path.read_text(encoding="utf-8"))
        old["segments"] = segments
        seg_path.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")


def run(proj: Project, log: Log, force: bool = False) -> dict:
    """执行 S4，产出 work/clips/*.m4a 与 work/clips.json。

    Returns:
        {"clips": {clipId: {...}}, "segments": [...]}
    """
    log.stage("S4 切片与编码")
    data = json.loads((config.WORK / "segments.json").read_text(encoding="utf-8"))
    segments = data["segments"]
    warps = warpmod.load_all(proj)
    durations = {p.id: audio.duration(config.DIR_PCM / f"{p.id}.wav") for p in proj.performers}
    log.line("  PCM 时长：" + ", ".join(f"{k}={v:.2f}s" for k, v in durations.items()))

    clips: dict[str, dict] = {}
    counter = 0
    drift_rows = []

    for seg in segments:
        t0 = seg["anchorStart"]
        dur = seg["duration"]
        starts: dict[str, float] = {}
        for perf in proj.performers:
            w = warps.get(perf.id)
            s = t0 if w is None else w.at(t0)
            e = t0 + dur if w is None else w.at(t0 + dur)
            drift = (e - s) - dur
            starts[perf.id] = s
            drift_rows.append([seg["id"], perf.id, f"{s:.2f}", f"{drift:+.3f}"])
            if s < -0.01 or s + dur > durations[perf.id] + 0.05:
                raise BuildError(
                    f"{seg['id']}/{perf.id}: 切片起点 {s:.2f}s + {dur}s 超出音轨 "
                    f"{durations[perf.id]:.2f}s —— 规格要求报错而非补零")

        # 段内响度对齐：以中位数为目标，双向调整但受真峰值约束
        # —— 只修正「录音/母带」层面的电平差，不动 25 秒内的动态起伏
        measures = {pid: audio.measure_region(config.DIR_PCM / f"{pid}.wav", s, dur)
                    for pid, s in starts.items()}
        levels = {pid: m["i"] for pid, m in measures.items()}
        cap = {pid: config.TARGET_TP - m["tp"] for pid, m in measures.items()}  # 不削顶的最大正增益
        target = min(float(np.median(list(levels.values()))),
                     max(v for v in levels.values()))          # 上限不超过最响的一条
        target = min(target, min(levels[pid] + cap[pid] for pid in levels))
        gains = {pid: float(np.clip(min(target - levels[pid], cap[pid]), GAIN_MIN, GAIN_MAX))
                 for pid in levels}
        residual = max(levels[p] + gains[p] for p in levels) - min(levels[p] + gains[p] for p in levels)
        log.line(f"  {seg['id']} anchor {t0:7.1f}s · 段内响度 "
                 + " ".join(f"{pid[0:4]}={levels[pid]:.1f}" for pid in levels)
                 + f" → 目标 {target:.1f} · 增益 "
                 + " ".join(f"{g:+.1f}" for g in gains.values())
                 + (f" · 残余极差 {residual:.1f}" if residual > 0.5 else ""))
        if residual > 3.0:
            log.warn(f"{seg['id']}: 段内响度残余极差 {residual:.1f} LU，受真峰值/增益上限约束")
        seg["gainResidual"] = round(residual, 2)

        for perf in proj.performers:
            counter += 1
            clip_id = f"c{counter:04d}"
            dst = config.DIR_CLIPS / f"{clip_id}.m4a"
            audio.encode_clip(config.DIR_PCM / f"{perf.id}.wav", dst, starts[perf.id], dur,
                              gain_db=gains[perf.id])
            probe = audio.ffprobe(dst)
            clips[clip_id] = {
                "performerId": perf.id,
                "segmentId": seg["id"],
                "bytes": dst.stat().st_size,
                "start": round(starts[perf.id], 3),
                "drift": round((warps[perf.id].at(t0 + dur) - starts[perf.id] - dur)
                               if warps.get(perf.id) else 0.0, 3),
                "gain": round(gains[perf.id], 2),
                "lufsSrc": round(measures[perf.id]["i"], 2),
                "duration": round(probe["duration"], 3),
                "codec": probe["codec"],
                "sampleRate": probe["sample_rate"],
                "channels": probe["channels"],
                "bitRate": probe["bit_rate"],
            }
            seg["clips"][perf.id] = clip_id

    (_write_both(clips, segments))

    # ---- 验收：等长 / 参数一致 ----------------------------------
    # 说明：ffprobe 的 bit_rate 是「文件字节数 / 时长」的实测均值，AAC 是内容自适应编码，
    # 均值不可能逐字节相同（实测 95% 的片段落在 96k ± 0.3%）。所以一致性的判据取
    # 编码参数本身（codec / 采样率 / 声道 / 目标码率），并要求实测均值偏差 < 1.5%。
    durs = np.array([c["duration"] for c in clips.values()])
    nominal = int(config.CLIP_BITRATE.rstrip("k")) * 1000
    params = {(c["codec"], c["sampleRate"], c["channels"]) for c in clips.values()}
    rates = np.array([c["bitRate"] for c in clips.values()])
    dev = float(np.max(np.abs(rates - nominal)) / nominal)
    log.table(["指标", "实测"], [
        ["片段数", str(len(clips))],
        ["时长 min/max", f"{durs.min():.3f} / {durs.max():.3f} s"],
        ["时长极差", f"{durs.max() - durs.min():.4f} s"],
        ["编码参数组合", f"{len(params)} 种 {sorted(params)}"],
        ["目标码率 / 实测均值", f"{nominal // 1000}k / {rates.mean() / 1000:.1f}k"
                              f"（{rates.min() // 1000}~{rates.max() // 1000}，最大偏差 {dev * 100:.2f}%）"],
        ["单片段体积 mean", f"{np.mean([c['bytes'] for c in clips.values()]) / 1024:.0f} KB"],
    ])
    log.metric("7 等长", f"{durs.min():.3f}~{durs.max():.3f}s（极差 {durs.max() - durs.min():.4f}s）",
               f"全部 = {proj.seg_duration:.3f}s", float(durs.max() - durs.min()) < 0.01)
    log.metric("3 编码一致", f"{len(params)} 种编码参数；实测码率最大偏差 {dev * 100:.2f}%",
               f"参数 1 种 + 码率偏差 < 1.5%", len(params) == 1 and dev < 0.015)
    if len(params) != 1:
        raise BuildError(f"片段编码参数不一致：{params}")
    if dev >= 0.015:
        raise BuildError(f"片段实测码率偏差 {dev * 100:.2f}% 过大，疑似编码参数不同")
    if durs.max() - durs.min() >= 0.01:
        raise BuildError(f"片段时长不一致：{durs.min():.3f}~{durs.max():.3f}")

    # ---- 验收：段内响度极差 --------------------------------------
    worst = 0.0
    for seg in segments:
        vals = [audio.measure_loudness(config.DIR_CLIPS / f"{cid}.m4a")["i"]
                for cid in seg["clips"].values()]
        spread = max(vals) - min(vals)
        worst = max(worst, spread)
        seg["clipLufs"] = [round(v, 2) for v in vals]
        seg["clipLufsSpread"] = round(spread, 3)
    log.metric("2b 段内响度一致", f"最大极差 {worst:.3f} LU", "≤ 0.5 LU", worst <= config.LUFS_TOLERANCE)
    if worst > config.LUFS_TOLERANCE:
        log.warn(f"有段落内响度极差 {worst:.3f} LU > 0.5，受真峰值/增益上下限约束（详见各段残留极差）")

    # ---- drift 汇总 ---------------------------------------------
    drifts = np.array([abs(c["drift"]) for c in clips.values()])
    log.metric("段内速度漂移", f"最大 |drift| {drifts.max():.3f}s（中位 {np.median(drifts):.3f}s）",
               "S3 已约束 ≤ 2.5s", drifts.max() <= 2.5)
    if drifts.max() > 2.5:
        log.warn(f"最大段内漂移 {drifts.max():.3f}s > 2.5s：该段首尾速度差较大，末尾可能略有错位")

    _write_both(clips, segments)
    log.line(f"  → {config.DIR_CLIPS}（{len(clips)} 个片段）")
    return {"clips": clips, "segments": segments}
