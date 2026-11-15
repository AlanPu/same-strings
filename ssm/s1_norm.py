"""S1 — 归一化（硬关卡）。

流程：
  ① 全部解码成 PCM 缓存；
  ② 有效演奏区间定位（去掉掌声/入场/返场，见 span.py）；
  ③ 削首尾静音、去分 P 拼接缝；
  ④ 测量积分响度 → 求全局静态增益 → 施加 → 复测。

全程只在最后编码一次，中间一律 16bit PCM（规格 §S1、§12.5）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import audio, config, span as spanmod
from .config import BuildError, Project
from .logbook import Log


def final_path(pid: str) -> Path:
    """归一化后 PCM 的固定路径。"""
    return config.DIR_PCM / f"{pid}.wav"


def solve_gains(measured: dict[str, dict[str, float]], log: Log) -> tuple[dict[str, float], float]:
    """求解所有演奏家的静态增益，使积分响度极差 ≤ 0.5 LU。

    单人增益受两个约束：目标响度、目标真峰值（TP 会截断正增益）。
    若有轨道被 TP 卡住从而拉大极差，则把全局目标整体下移到「所有人都能到达」的水平
    —— 宁可整体安静 8 dB，也不做动态压缩去凑 -16 LUFS（规格 §S1.2）。

    Args:
        measured: {id: {"i": LUFS, "tp": dBTP, "lra": LU}}
        log: 日志写入器。

    Returns:
        ({id: 增益 dB}, 最终统一目标 LUFS)
    """
    target = config.TARGET_LUFS
    gains: dict[str, float] = {}
    for round_idx in range(1, 4):
        gains = {
            pid: min(target - m["i"], config.TARGET_TP - m["tp"])
            for pid, m in measured.items()
        }
        levels = {pid: measured[pid]["i"] + gains[pid] for pid in measured}
        spread = max(levels.values()) - min(levels.values())
        log.line(f"  增益求解 第 {round_idx} 轮：目标 {target:.2f} LUFS，极差 {spread:.3f} LU")
        for pid, g in gains.items():
            log.line(f"    {pid:<10} 实测 {measured[pid]['i']:7.2f} LUFS · TP {measured[pid]['tp']:6.2f} dB"
                     f" → 增益 {g:+6.2f} dB → {levels[pid]:7.2f} LUFS")
        if spread <= config.LUFS_TOLERANCE:
            return gains, target
        target = min(levels.values()) - 0.05
        log.warn(f"响度极差 {spread:.3f} LU 超标，统一目标下调至 {target:.2f} LUFS 后重算")
    levels = {pid: measured[pid]["i"] + gains[pid] for pid in measured}
    spread = max(levels.values()) - min(levels.values())
    if spread > config.LUFS_TOLERANCE:
        raise BuildError(f"响度极差 {spread:.3f} LU 仍 > {config.LUFS_TOLERANCE} LU，"
                         f"无法在不压动态的前提下统一（规格 S1 硬关卡）")
    return gains, target


def intersect(intervals: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """把区间列表与 [lo, hi] 求交，丢弃过短的碎片。"""
    out = []
    for a, b in intervals:
        a2, b2 = max(a, lo), min(b, hi)
        if b2 - a2 > 0:
            out.append((a2, b2))
    return out


def complement(intervals: list[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """求保留区间的补集（即被删掉的部分）。"""
    removed = []
    cursor = 0
    for a, b in intervals:
        if a > cursor:
            removed.append((cursor, a))
        cursor = b
    if cursor < total:
        removed.append((cursor, total))
    return removed


def run(proj: Project, log: Log, force: bool = False) -> dict[str, dict[str, Any]]:
    """执行 S1，产出 work/pcm/<id>.wav。

    Returns:
        {id: {"duration", "lufs", "tp", "gain", "trimmed", "removed", ...}}
    """
    log.stage("S1 归一化（硬关卡）")

    # ---- ① 解码 --------------------------------------------------
    log.line("-- ① 解码为 44.1kHz/16bit/立体声 PCM")
    raws: dict[str, Path] = {}
    metas: dict[str, dict] = {}
    for perf in proj.performers:
        src_meta_path = config.DIR_SRC / f"{perf.id}.json"
        if not src_meta_path.exists():
            raise BuildError(f"{perf.id}: 缺少 S0 产物 {src_meta_path}")
        meta = json.loads(src_meta_path.read_text(encoding="utf-8"))
        metas[perf.id] = meta
        raw = config.DIR_PCM / f"{perf.id}.raw.wav"
        audio.decode_wav(Path(meta["file"]), raw)
        raws[perf.id] = raw
        log.line(f"  {perf.id}: {audio.duration(raw):.2f}s")

    # ---- ② 有效演奏区间 ------------------------------------------
    log.line("-- ② 有效演奏区间定位（去掌声/入场/返场）")
    feats = {pid: spanmod.load_features(pid, raws[pid], "raw", log) for pid in proj.ids}
    anchor_dur = audio.duration(raws[proj.anchor])
    spans: dict[str, tuple[float, float]] = {proj.anchor: (0.0, anchor_dur)}
    span_infos: dict[str, dict] = {}
    for perf in proj.performers:
        if perf.id == proj.anchor:
            continue
        t_dur = audio.duration(raws[perf.id])
        start, end, info = spanmod.detect_span(
            feats[proj.anchor], feats[perf.id], anchor_dur, t_dur, perf.id, log)
        spans[perf.id] = (start, end)
        span_infos[perf.id] = info

    # ---- ②b 头部对齐（掌声不是静音，必须单独裁）-------------------
    log.line("-- ②b 头部对齐（把各异版本的掌声/静场裁到同一个音乐起点）")
    heads = spanmod.detect_heads(raws, feats, log)
    del feats

    # ---- ③ 剪辑 --------------------------------------------------
    log.line("-- ③ 剪辑（区间裁剪 + 首尾静音 + 分 P 拼接缝）")
    plan: list[dict[str, Any]] = []
    for perf in proj.performers:
        pid = perf.id
        raw = raws[pid]
        trim = config.DIR_PCM / f"{pid}.trim.wav"
        levels, sr, total = audio.block_dbfs(raw, 0.01)

        start, end = spans[pid]
        lo, hi = int(start * sr), int(end * sr)
        head = heads.get(pid, 0.0)
        if head > 0:
            lo = max(lo, int(head * sr))

        boundaries = [int(round(b * sr)) for b in metas[pid].get("partBoundaries") or []]
        keep = audio.keep_intervals(levels, sr, total, 0.01, gap_boundaries=boundaries)
        keep = intersect(keep, lo, hi)
        if not keep:
            raise BuildError(f"{pid}: 区间 {start:.1f}~{end:.1f}s 内没有可用音频")
        removed = complement(keep, total)
        removed_sec = [(a / sr, b / sr) for a, b in removed]

        audio.cut_intervals(raw, trim, keep)
        trim_dur = audio.duration(trim)
        log.line(f"  {pid}: 原 {total / sr:7.2f}s → 保留 {trim_dur:7.2f}s"
                 f"（区间 {start:.1f}~{end:.1f}s，削掉 {sum(b - a for a, b in removed_sec):.2f}s）")
        head_cut = [t for t in removed_sec if t[0] < 0.05]
        if head_cut:
            log.line(f"          开头去掉 {head_cut[0][1]:.2f}s"
                     + (f"（其中头部对齐 {head:.2f}s）" if head > 0 else ""))

        measured = audio.measure_loudness(trim)
        log.line(f"          {measured['i']:.2f} LUFS · TP {measured['tp']:.2f} dB · "
                 f"LRA {measured['lra']:.1f}")
        plan.append({"perf": perf, "trim": trim, "raw": raw, "dur": trim_dur,
                     "raw_dur": total / sr, "measured": measured,
                     "head": round(head, 3),
                     "removed": [[round(a, 3), round(b, 3)] for a, b in removed_sec],
                     "span": [round(start, 2), round(end, 2)]})
        raw.unlink(missing_ok=True)

    # ---- ④ 全局增益 ----------------------------------------------
    log.line("-- ④ 全局增益求解")
    gains, target = solve_gains({p["perf"].id: p["measured"] for p in plan}, log)

    log.line("-- 施加静态增益并复测")
    result: dict[str, dict[str, Any]] = {}
    for item in plan:
        pid = item["perf"].id
        dst = final_path(pid)
        audio.apply_gain(item["trim"], dst, gains[pid])
        item["trim"].unlink(missing_ok=True)
        after = audio.measure_loudness(dst)
        result[pid] = {
            "duration": round(audio.duration(dst), 3),
            "lufs": round(after["i"], 3),
            "tp": round(after["tp"], 3),
            "gain": round(gains[pid], 3),
            "trimmed": round(item["raw_dur"] - item["dur"], 3),
            "headCut": item["head"],
            "srcDuration": round(item["raw_dur"], 3),
            "removed": item["removed"],
            "span": item["span"],
        }
        log.line(f"   {pid:<10} {result[pid]['duration']:8.3f}s  {after['i']:7.2f} LUFS  "
                 f"TP {after['tp']:6.2f}  (目标 {target:.2f})")
        (config.DIR_PCM / f"{pid}.json").write_text(
            json.dumps(result[pid], ensure_ascii=False, indent=2), encoding="utf-8")

    spreads = max(r["lufs"] for r in result.values()) - min(r["lufs"] for r in result.values())
    log.table(["id", "时长(s)", "LUFS", "TP(dB)", "增益(dB)", "削掉(s)", "区间(s)"],
              [[p, f"{r['duration']:.3f}", f"{r['lufs']:.2f}", f"{r['tp']:.2f}",
                f"{r['gain']:+.2f}", f"{r['trimmed']:.2f}",
                f"{r['span'][0]:.1f}-{r['span'][1]:.1f}"] for p, r in result.items()])
    log.kv(统一目标=f"{target:.2f} LUFS（受真峰值约束，见上表 TP 列）")
    log.metric("2 响度一致", f"极差 {spreads:.3f} LU", "≤ 0.5 LU", spreads <= config.LUFS_TOLERANCE)
    if spreads > config.LUFS_TOLERANCE:
        raise BuildError(f"响度极差 {spreads:.3f} LU 超标")
    return result
