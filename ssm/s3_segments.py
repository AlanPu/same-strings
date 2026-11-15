"""S3 — 自动选段。

两种切分方式（project.json 的 segment.mode）：

  · sequential（默认）：从乐曲开头按时间顺序连续切，第 1 段起点 = 0，
    之后每段紧接上一段（stride 默认 = duration）。完全确定，没有随机、没有跳跃。
    某一刀如果落在静音或乐章边界上，只允许向后微移（≤ MAX_SHIFT 秒）并写进日志，
    移完继续按原步长往下走 —— 顺序与可复现性不变。
  · spread：旧行为。在 anchor 时间轴上生成候选窗口（步长 5 秒），按乐章配额 + 最小间隔
    从高分往低分挑，段与段之间可能隔很远。

两种模式共用同一套质量指标：
  ① 独奏主导度：中/侧能量比（最有效）、谱平坦度、能量包络活跃度；
  ② 内容完整性：避开乐章边界、静音、过于靠首尾；
  ③ 版本间区分度：MFCC + 一阶差分的跨版本距离 → 难度（必须用音色类特征，不能用 chroma）。
"""
from __future__ import annotations

import json
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import find_peaks

from . import audio, config, warp as warpmod
from .config import BuildError, Project
from .logbook import Log

BLOCK = 0.1                       # 包络特征块长（秒）
SILENCE_DB = -45.0                # 判为静音的块电平
EDGE_LEAD = 8.0                   # spread 模式下距首尾至少这么远
BOUNDARY_MARGIN = 1.5             # 距乐章边界至少这么远
MAX_SILENCE_RATIO = 0.25          # 窗口内静音占比上限
PEAK_PROMINENCE = 6.0             # 包络极值的最小突起（dB）
DRIFT_LIMIT = 2.5                 # spread 模式下段内速度一致性的首选门槛（秒）
DRIFT_LIMIT_FALLBACK = 4.0        # 候选不够时的放宽门槛
MAX_SHIFT = 30.0                  # sequential 模式下允许的下刀点后移上限（秒）
SHIFT_REPORT = 0.5                # 记录「后移了多少」的最小值（秒），小于它按未移动处理


def _z(x: np.ndarray) -> np.ndarray:
    """z-score 标准化。"""
    return (x - x.mean()) / (x.std() + 1e-9)


def block_features(pcm: Path) -> tuple[np.ndarray, np.ndarray]:
    """按 0.1 秒块统计中/侧能量比与总电平。

    Args:
        pcm: 立体声 PCM。

    Returns:
        (ms_ratio_db[每块], rms_db[每块])
    """
    with sf.SoundFile(str(pcm)) as fh:
        sr = fh.samplerate
        bs = int(round(BLOCK * sr))
        ms, rms = [], []
        for blk in fh.blocks(blocksize=bs, dtype="float32", always_2d=True):
            left, right = blk[:, 0], blk[:, 1]
            mid = (left + right) * 0.5
            side = (left - right) * 0.5
            e_mid = float(np.sum(mid * mid, dtype=np.float64))
            e_side = float(np.sum(side * side, dtype=np.float64))
            ms.append(10.0 * np.log10((e_mid + 1e-12) / (e_side + 1e-12)))
            rms.append(10.0 * np.log10((e_mid + e_side) / max(1, len(blk)) + 1e-12))
    return np.asarray(ms, dtype=np.float32), np.asarray(rms, dtype=np.float32)


def frame_cumsum(mat: np.ndarray) -> np.ndarray:
    """在时间轴（axis=1）上做前缀和，便于 O(1) 取窗口均值。"""
    return np.cumsum(mat, axis=1, dtype=np.float64)


def window_mean(cs: np.ndarray, f0: int, f1: int) -> np.ndarray:
    """取 [f0, f1) 帧范围的均值向量（cs 为前缀和，需含前置 0 列）。"""
    f0 = max(0, min(f0, cs.shape[1] - 1))
    f1 = max(f0 + 1, min(f1, cs.shape[1]))
    return (cs[:, f1] - cs[:, f0]) / float(f1 - f0)


def timbre_matrix(pid: str, pcm: Path, log: Log) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """计算某演奏家的 MFCC + Δ 前缀和与时序。

    Returns:
        (前缀和矩阵 (38, T+1), 帧时长, 总时长)
    """
    cache = config.WORK / "feat" / f"{pid}.mfcc.npz"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and cache.stat().st_mtime >= pcm.stat().st_mtime:
        z = np.load(cache)
        return z["cs"], z["frame_sec"], z["dur"]
    y, _ = librosa.load(str(pcm), sr=config.SR_ANALYSIS, mono=True)
    mfcc = librosa.feature.mfcc(y=y, sr=config.SR_ANALYSIS, n_mfcc=20, hop_length=config.HOP)
    delta = librosa.feature.delta(mfcc, width=9)
    # 丢掉 c0（整体电平），只保留音色类信息
    mat = np.vstack([mfcc[1:], delta[1:]]).astype(np.float32)
    frame_sec = config.HOP / config.SR_ANALYSIS
    cs = np.concatenate([np.zeros((mat.shape[0], 1)), frame_cumsum(mat)], axis=1)
    dur = len(y) / config.SR_ANALYSIS
    np.savez(cache, cs=cs, frame_sec=frame_sec, dur=dur)
    del y
    return cs, frame_sec, dur


def window_timbre(cs: np.ndarray, frame_sec: float, start: float, dur: float) -> np.ndarray:
    """取某时间窗的音色特征向量（L2 归一化后的 38 维均值）。"""
    f0 = int(round(start / frame_sec))
    f1 = int(round((start + dur) / frame_sec))
    vec = window_mean(cs, f0, f1)
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-9)


def pick_by_movements(
    cand: list[dict],
    movements: list[dict],
    need: int,
    min_gap: float,
    seg_dur: float,
    log: Log,
) -> list[dict]:
    """按乐章配额 + 最小间隔贪心选段。

    Args:
        cand: 已按 soloScore 降序排列的候选窗口。
        movements: 乐章边界（anchor 秒）。
        need: 需要的段数。
        min_gap: 相邻选中间隔下限（秒）。
        seg_dur: 窗口时长。
        log: 日志。

    Returns:
        选中的候选列表。
    """
    total_span = sum(m["end"] - m["start"] for m in movements) or 1.0
    quotas = []
    remainders = []
    for m in movements:
        exact = need * (m["end"] - m["start"]) / total_span
        quotas.append(int(np.floor(exact)))
        remainders.append(exact - np.floor(exact))
    # 最大余数法补齐
    while sum(quotas) < need:
        idx = int(np.argmax(remainders))
        quotas[idx] += 1
        remainders[idx] = -1

    chosen: list[dict] = []
    used: list[float] = []

    def far_enough(t0: float) -> bool:
        return all(abs(t0 - u) >= min_gap for u in used)

    for m, quota in zip(movements, quotas):
        if quota <= 0:
            continue
        pool = [c for c in cand
                if c["t0"] >= m["start"] + EDGE_LEAD
                and c["t0"] + seg_dur <= m["end"] - EDGE_LEAD / 2]
        picked = 0
        for c in pool:
            if picked >= quota:
                break
            if far_enough(c["t0"]):
                chosen.append(c)
                used.append(c["t0"])
                picked += 1
        if picked < quota:
            log.warn(f"{m['label']} 配额 {quota} 段，实际只选出 {picked} 段（受最小间隔约束）")

    # 不足则全局补齐
    if len(chosen) < need:
        for c in cand:
            if len(chosen) >= need:
                break
            if c in chosen:
                continue
            if far_enough(c["t0"]):
                chosen.append(c)
                used.append(c["t0"])
    if len(chosen) < need:
        log.warn(f"仅选出 {len(chosen)} 段（目标 {need}），放宽最小间隔后仍然不够")
        for c in cand:
            if len(chosen) >= need:
                break
            if c not in chosen:
                chosen.append(c)
                used.append(c["t0"])
    return chosen


def window_usable(c: dict, seg_dur: float, boundaries: list[float],
                  anchor_dur: float) -> tuple[bool, str]:
    """判断一个窗口能不能直接用来出题。

    Args:
        c: 候选窗口（含 t0 / sil_ratio）。
        seg_dur: 窗口时长（秒）。
        boundaries: 乐章边界时刻（anchor 秒）。
        anchor_dur: anchor 总时长（秒）。

    Returns:
        (是否可用, 不可用原因)
    """
    if c["t0"] + seg_dur > anchor_dur - 1.0:
        return False, "越过乐曲末尾"
    if any(c["t0"] - BOUNDARY_MARGIN < b < c["t0"] + seg_dur + BOUNDARY_MARGIN
           for b in boundaries):
        return False, "跨乐章边界"
    if c["sil_ratio"] > MAX_SILENCE_RATIO:
        return False, f"静音占 {c['sil_ratio'] * 100:.0f}%"
    return True, ""


def pick_sequential(
    cands: list[dict],
    movements: list[dict],
    need: int,
    seg_dur: float,
    anchor_dur: float,
    stride: float,
    log: Log,
) -> list[dict]:
    """从乐曲开头按时间顺序连续切段（确定性，无随机、无跳跃）。

    第 1 刀固定在 t=0；之后每刀落在上一刀起点 + stride（stride 缺省 = seg_dur，即首尾相接）。
    某一刀如果落在静音或乐章边界上，只向后微移（步长 SEG_HOP，最多 MAX_SHIFT 秒），
    移完把后续刀口一起带上，仍然严格单调、可复现。

    Args:
        cands: 全部候选窗口（含 t0 及质量指标）。
        movements: 乐章划分。
        need: 需要的段数。
        seg_dur: 每段时长（秒）。
        anchor_dur: anchor 总时长（秒）。
        stride: 相邻两段起点的间隔（秒）；0 表示紧接上一段。
        log: 日志。

    Returns:
        选中的候选列表（按时间升序）。
    """
    by_t0 = {round(c["t0"], 3): c for c in cands}
    step = float(stride) if stride and stride > 0 else float(seg_dur)
    boundaries = [m["start"] for m in movements[1:]] + [m["end"] for m in movements[:-1]]

    chosen: list[dict] = []
    t = 0.0
    guard = 0
    while len(chosen) < need and t + seg_dur <= anchor_dur + 1e-6 and guard < 10_000:
        guard += 1
        picked = None
        why_last = "无候选"
        shift = 0.0
        while shift <= MAX_SHIFT:
            cand = by_t0.get(round(t + shift, 3))
            if cand is not None:
                ok, why = window_usable(cand, seg_dur, boundaries, anchor_dur)
                if ok:
                    picked = cand
                    break
                why_last = why
            shift += config.SEG_HOP
        if picked is None:
            picked = by_t0.get(round(t, 3))
            if picked is None:
                log.warn(f"t={t:.0f}s 处没有候选窗口，停止顺序切分（已切 {len(chosen)} 段）")
                break
            log.warn(f"t={t:.0f}s 起 {seg_dur:.0f}s 后移 {MAX_SHIFT:.0f}s 内都不可用"
                     f"（{why_last}），仍按原位切")
        elif shift >= SHIFT_REPORT:
            log.warn(f"t={t:.0f}s 起的窗口不可用（{why_last}），后移 {shift:.0f}s 到 {picked['t0']:.0f}s")
        chosen.append(picked)
        t = picked["t0"] + step
    return chosen


def run(proj: Project, log: Log, force: bool = False) -> list[dict]:
    """执行 S3，产出 work/segments.json。

    Returns:
        segments 列表。
    """
    log.stage("S3 自动选段")
    anchor_pcm = config.DIR_PCM / f"{proj.anchor}.wav"
    anchor_dur = audio.duration(anchor_pcm)
    need = proj.segment_count
    seg_dur = proj.seg_duration
    log.kv(演奏家数=proj.n, 段数=need, 片段总数=proj.clip_count,
           片段时长=f"{seg_dur}s",
           切分方式="顺序（从乐曲开头连续切）" if proj.seg_mode == "sequential"
                    else f"分散（乐章配额 + 最小间隔 {proj.seg_min_gap:.0f}s）")
    if proj.seg_mode == "sequential":
        step = proj.seg_stride if proj.seg_stride > 0 else seg_dur
        log.kv(切分起点="0.0s（乐曲开头）", 相邻间隔=f"{step:.1f}s"
               + ("（首尾相接）" if abs(step - seg_dur) < 1e-6 else ""))
    log.line("  " + proj.check_budget())
    log.kv(可选时长预设=", ".join(f"{x}s" for x in proj.presets))

    warps = warpmod.load_all(proj)
    movements = json.loads((config.WORK / "movements.json").read_text(encoding="utf-8"))
    log.line("  乐章（anchor 秒）：" + " / ".join(
        f"{m['label'] or '全曲'} {m['start']:.1f}-{m['end']:.1f}" for m in movements))

    # ---- 人工指定兜底（project.json 的 segment.manual）----------
    if proj.manual_segments:
        log.warn(f"使用 project.json 的 manual 时间列表，跳过自动选段（{len(proj.manual_segments)} 段）")
        chosen = [{"t0": float(t), "solo": 0.0, "score": 0.0} for t in proj.manual_segments]
        for c in chosen:
            c["movement"] = next((m["index"] for m in movements
                                  if m["start"] <= c["t0"] < m["end"]), 1)
        return _finalize(proj, chosen, movements, anchor_dur, log)

    # ---- ① 独奏主导度 -------------------------------------------
    log.line("-- 计算 anchor 块特征（中/侧比、电平包络）")
    ms_db, rms_db = block_features(anchor_pcm)
    y_mono, _ = librosa.load(str(anchor_pcm), sr=config.SR_ANALYSIS, mono=True)
    flatness = librosa.feature.spectral_flatness(y=y_mono, n_fft=2048, hop_length=config.HOP)[0]
    frame_sec = config.HOP / config.SR_ANALYSIS
    flat_cs = np.cumsum(np.concatenate([[0.0], flatness.astype(np.float64)]))
    del y_mono

    n_cand = int((anchor_dur - seg_dur) // config.SEG_HOP) + 1
    cands: list[dict] = []
    for i in range(n_cand):
        t0 = i * config.SEG_HOP
        b0, b1 = int(t0 / BLOCK), int((t0 + seg_dur) / BLOCK)
        f0, f1 = int(t0 / frame_sec), int((t0 + seg_dur) / frame_sec)
        blocks = rms_db[b0:b1]
        if len(blocks) == 0:
            continue
        peak_idx, _ = find_peaks(blocks, prominence=PEAK_PROMINENCE, distance=5)
        flat = float((flat_cs[min(f1, len(flat_cs) - 1)] - flat_cs[min(f0, len(flat_cs) - 1)])
                     / max(1, min(f1, len(flat_cs) - 1) - min(f0, len(flat_cs) - 1)))
        cands.append({
            "t0": t0,
            "ms": float(np.mean(ms_db[b0:b1])),
            "flat": flat,
            "sil_ratio": float(np.mean(blocks < SILENCE_DB)),
            "peak_rate": float(len(peak_idx) / seg_dur),
            "driftMax": max((abs(w.at(t0 + seg_dur) - w.at(t0) - seg_dur)
                             for w in warps.values() if w is not None), default=0.0),
        })
    if not cands:
        raise BuildError("候选窗口为空：anchor 太短")

    ms_z = _z(np.array([c["ms"] for c in cands]))
    flat_z = _z(np.array([c["flat"] for c in cands]))
    act_z = _z(1.0 - np.array([c["sil_ratio"] for c in cands])) \
        + _z(np.array([c["peak_rate"] for c in cands]))
    for i, c in enumerate(cands):
        c["solo"] = (config.W_MS_RATIO * ms_z[i]
                     + config.W_FLATNESS * (-flat_z[i])
                     + config.W_ACTIVITY * act_z[i] / 2)
    log.kv(S3权重=f"ms_ratio={config.W_MS_RATIO} activity={config.W_ACTIVITY} flatness={config.W_FLATNESS}",
           候选窗口数=len(cands),
           中侧比范围=f"{np.min([c['ms'] for c in cands]):.1f} ~ {np.max([c['ms'] for c in cands]):.1f} dB")

    # ---- ② 选段 -------------------------------------------------
    if proj.seg_mode == "sequential":
        chosen = pick_sequential(cands, movements, need, seg_dur, anchor_dur,
                                 proj.seg_stride, log)
        gaps = np.diff([c["t0"] for c in chosen]) if len(chosen) > 1 else np.array([])
        log.kv(顺序切分=f"起点 {chosen[0]['t0']:.1f}s，共 {len(chosen)} 段，"
                        f"相邻间隔 {gaps.min():.1f}~{gaps.max():.1f}s" if len(gaps)
               else f"起点 {chosen[0]['t0']:.1f}s，共 {len(chosen)} 段")
    else:
        # ---- ②' 内容完整性（分散模式：先过滤出可用池，再按分数挑）----
        boundaries = [m["start"] for m in movements[1:]] + [m["end"] for m in movements[:-1]]
        clean = []
        for c in cands:
            t0 = c["t0"]
            if t0 < EDGE_LEAD or t0 + seg_dur > anchor_dur - EDGE_LEAD:
                continue
            if any(t0 - BOUNDARY_MARGIN < b < t0 + seg_dur + BOUNDARY_MARGIN for b in boundaries):
                continue
            if c["sil_ratio"] > MAX_SILENCE_RATIO:
                continue
            clean.append(c)
        log.kv(完整性过滤后候选=len(clean), 被剔除=len(cands) - len(clean))

        # 速度一致性：同一段乐谱里的相对时长必须接近，否则两段「同样时长」
        # 覆盖的乐句量会差出一大截（实测有演奏家只走了 18.6 秒的音乐）。
        drift_all = np.array([c["driftMax"] for c in clean])
        limit = DRIFT_LIMIT
        if int((drift_all <= limit).sum()) < need * 3:
            limit = DRIFT_LIMIT_FALLBACK
        if int((drift_all <= limit).sum()) < need * 2:
            limit = float(drift_all.max())
            log.warn("速度一致的候选太少，放弃该约束")
        clean = [c for c in clean if c["driftMax"] <= limit]
        log.kv(速度一致性门槛=f"max|drift| ≤ {limit:.1f}s，通过 {len(clean)}/{len(drift_all)} 个")
        if len(clean) < need:
            log.warn(f"候选仅 {len(clean)} 个 < 需要 {need} 个，放宽边界约束")
            clean = [c for c in cands if c["driftMax"] <= limit] or cands

        clean.sort(key=lambda x: -x["solo"])
        chosen = pick_by_movements(clean, movements, need, proj.seg_min_gap, seg_dur, log)
        chosen.sort(key=lambda x: x["t0"])

    for c in chosen:
        c["movement"] = next((m["index"] for m in movements
                              if m["start"] <= c["t0"] < m["end"]), 1)

    # ---- ③ 版本间区分度 → 难度 ----------------------------------
    log.line("-- 计算跨版本音色区分度（MFCC+Δ）")
    timbre: dict[str, tuple[np.ndarray, float, float]] = {}
    for perf in proj.performers:
        pcm = config.DIR_PCM / f"{perf.id}.wav"
        timbre[perf.id] = timbre_matrix(perf.id, pcm, log)
        log.line(f"   {perf.id}: {timbre[perf.id][0].shape[1] - 1} 帧 MFCC+Δ")

    for c in chosen:
        vecs = []
        for perf in proj.performers:
            cs, fsec, dur = timbre[perf.id]
            w = warps.get(perf.id)
            t_p = c["t0"] if w is None else w.at(c["t0"])
            t_p = min(max(0.0, t_p), max(0.0, dur - seg_dur - 0.05))
            vecs.append(window_timbre(cs, fsec, t_p, seg_dur))
        vecs = np.vstack(vecs)
        sims = vecs @ vecs.T
        iu = np.triu_indices(len(vecs), k=1)
        c["distance"] = float(1.0 - np.mean(sims[iu]))

    # ---- 难度归一化 ---------------------------------------------
    dists = np.array([c["distance"] for c in chosen])
    if dists.max() - dists.min() < 1e-6:
        diff = np.full(len(chosen), 0.5)
    else:
        diff = 1.0 - (dists - dists.min()) / (dists.max() - dists.min())
    for c, d in zip(chosen, diff):
        c["difficulty"] = round(float(d), 3)

    return _finalize(proj, chosen, movements, anchor_dur, log)


def _finalize(proj: Project, chosen: list[dict], movements: list[dict],
              anchor_dur: float, log: Log) -> list[dict]:
    """生成 segments.json 的最终结构。"""
    per_movement_count: dict[int, int] = {}
    segments: list[dict] = []
    for i, c in enumerate(sorted(chosen, key=lambda x: x["t0"]), 1):
        mid = c.get("movement") or 1
        per_movement_count[mid] = per_movement_count.get(mid, 0) + 1
        mov_label = next((m["label"] for m in movements if m["index"] == mid), "")
        seg = {
            "id": f"s{i:02d}",
            "label": f"{mov_label + ' · ' if mov_label else ''}段 {per_movement_count[mid]}",
            "movement": mid,
            "duration": proj.seg_duration,
            "difficulty": c.get("difficulty", 0.5),
            "anchorStart": round(float(c["t0"]), 3),
            "soloScore": round(float(c.get("solo", 0.0)), 3),
            "distance": round(float(c.get("distance", 0.0)), 4),
            "driftMax": round(float(c.get("driftMax", 0.0)), 2),
            "clips": {},
        }
        segments.append(seg)

    total = len(segments) * proj.n
    log.table(["id", "乐章", "anchor起(s)", "难度", "solo", "区分度", "max|drift|"],
              [[s["id"], s["movement"], f"{s['anchorStart']:.1f}",
                f"{s['difficulty']:.2f}", f"{s['soloScore']:+.2f}", f"{s['distance']:.4f}",
                f"{s.get('driftMax', 0):.2f}"]
               for s in segments])
    log.kv(选中段数=len(segments), 片段总数=total)

    cap = config.max_clip_count(proj.seg_duration)
    log.metric("12 片段总数", total, f"≤ {cap}（体积预算）且 = {proj.n}×{len(segments)}",
               total <= cap and total == proj.n * len(segments))
    if total > cap:
        raise BuildError(f"片段总数 {total} 超过体积预算上限 {cap}（{proj.seg_duration}s 片段）")
    if len(segments) < config.SEG_COUNT_MIN:
        raise BuildError(f"仅选出 {len(segments)} 段，少于下限 {config.SEG_COUNT_MIN}")

    path = config.WORK / "segments.json"
    path.write_text(json.dumps({"anchor": proj.anchor, "anchorDuration": round(anchor_dur, 3),
                                "movements": movements, "segments": segments},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    log.line(f"  → {path}")
    return segments
