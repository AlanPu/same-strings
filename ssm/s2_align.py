"""S2 — 对齐（锚点时间轴 + 色度特征 + 全局单调 DTW）。

要点（规格 §S2）：
  · 单声道 22050Hz 分析；
  · chroma_cens，逐帧 L2 归一化 —— 只保留和声内容，忽略音色/音量/调音差异；
  · 降采样到 ~3.5fps 并做 1 秒平滑，抗速度差；
  · 全局单调 DTW（subseq=False），严禁子序列搜索，否则再现部会匹配到错误位置。
"""
from __future__ import annotations

import json
from pathlib import Path

import librosa
import numpy as np

from . import audio, config, span as spanmod, warp as warpmod
from .config import BuildError, Project
from .logbook import Log

# 头部局部校正：全局 DTW 的两端被硬约束成「0 对 0、末尾对末尾」，两端各版本
# 总时长差几秒（本曲目最多差 5 秒）时，这个差值会被挤到端点附近，
# 表现为开头的局部速度比掉到 2~3 倍（实测穆特前 10 秒只走了 4.4 秒音乐）。
# 既然现在段子是从 0 秒开始切的，头部就必须准。做法：拿锚点开头 60 秒
# 做一次子序列匹配（这一段是纯音乐，没有端点约束），用它的映射替换头部，
# 再在 60~90 秒之间线性过渡回全局 warp。
HEAD_FIX_SEC = 60.0
HEAD_BLEND_SEC = 30.0
HEAD_SEARCH_SEC = 180.0


def _smooth_repath(path: np.ndarray, t_from: float, t_to: float) -> tuple[np.ndarray, np.ndarray]:
    """把 DTW 路径（起点→终点）重采样到 0.5 秒网格，窗口取中位抹平竖直段。

    Args:
        path: (N, 2) 的 (锚点帧, 目标帧)。
        t_from: 起始时间（秒）。
        t_to: 结束时间（秒）。

    Returns:
        (网格时间, 对应目标时间)。
    """
    fps = config.ALIGN_FPS
    fx = path[:, 0] / fps
    fy = path[:, 1] / fps
    order = np.argsort(fx, kind="stable")
    fx, fy = fx[order], fy[order]
    grid = np.arange(t_from, t_to + 1e-6, config.WARP_STEP)
    lo = np.searchsorted(fx, grid - 1.0, side="left")
    hi = np.searchsorted(fx, grid + 1.0, side="right")
    out = np.empty_like(grid)
    for k in range(len(grid)):
        a, b = lo[k], hi[k]
        out[k] = float(np.median(fy[a:b])) if b > a else float(np.interp(grid[k], fx, fy))
    return grid, out


def _map_cos(feat_a: np.ndarray, feat_p: np.ndarray,
             grid: np.ndarray, mapped: np.ndarray) -> float:
    """按 warp 表逐点抽样，复算平均余弦相似度。

    Args:
        feat_a: 锚点特征。
        feat_p: 目标特征。
        grid: 锚点时间网格（秒）。
        mapped: 对应目标时间（秒）。

    Returns:
        平均余弦相似度。
    """
    fps = config.ALIGN_FPS
    ia = np.clip(np.round(grid * fps).astype(int), 0, feat_a.shape[1] - 1)
    ip = np.clip(np.round(mapped * fps).astype(int), 0, feat_p.shape[1] - 1)
    a, b = feat_a[:, ia], feat_p[:, ip]
    denom = np.maximum(np.linalg.norm(a, axis=0) * np.linalg.norm(b, axis=0), 1e-9)
    return float(np.mean(np.sum(a * b, axis=0) / denom))


def head_local_map(feat_a: np.ndarray, feat_p: np.ndarray, log: Log, pid: str) -> tuple[np.ndarray, np.ndarray] | None:
    """局部子序列匹配出锚点开头一段的映射（不受全局端点约束影响）。

    Args:
        feat_a: 锚点特征。
        feat_p: 目标特征。
        log: 日志。
        pid: 演奏家 id。

    Returns:
        (网格时间, 目标时间)；匹配不可信时返回 None。
    """
    fps = config.ALIGN_FPS
    probe = feat_a[:, : int(HEAD_FIX_SEC * fps)]
    y_head = feat_p[:, : int(HEAD_SEARCH_SEC * fps)]
    if probe.shape[1] < 20 or y_head.shape[1] < 20:
        return None
    _, wp = librosa.sequence.dtw(X=probe, Y=y_head, subseq=True, backtrack=True,
                                 metric="cosine")
    wp = wp[::-1]
    cos = float(np.mean(np.sum(probe[:, wp[:, 0]] * y_head[:, wp[:, 1]], axis=0)))
    if cos < 0.80:
        log.warn(f"  {pid}: 头部局部匹配可信度低（cos {cos:.3f}），沿用全局 warp")
        return None
    grid, mapped = _smooth_repath(wp, 0.0, HEAD_FIX_SEC)
    log.line(f"  {pid}: 头部局部匹配 cos {cos:.3f}，warp(0) {mapped[0]:.2f}s")
    return grid, mapped



def load_feat(pid: str, pcm: Path, log: Log) -> np.ndarray:
    """带缓存地读取某位演奏家的对齐特征（与 S1 区间定位共用同一套特征代码）。"""
    return spanmod.load_features(pid, pcm, "pool", log)


def build_warp(
    pid: str,
    feat_a: np.ndarray,
    feat_p: np.ndarray,
    anchor_dur: float,
    perf_dur: float,
    nudge: float,
    log: Log,
) -> dict:
    """跑全局单调 DTW 并生成 0.5 秒精度的 warp 表。

    Args:
        pid: 演奏家 id。
        feat_a: anchor 特征 (12, Ta)。
        feat_p: 目标演奏家特征 (12, Tp)。
        anchor_dur: anchor 时长（秒）。
        perf_dur: 目标时长（秒）。
        nudge: 手工微调偏移（秒）。
        log: 日志。

    Returns:
        warp 字典（规格 §5.2 的字段）。
    """
    log.line(f"   全局 DTW {pid}: {feat_a.shape[1]} × {feat_p.shape[1]} 帧 …")
    _, wp = librosa.sequence.dtw(
        X=feat_a, Y=feat_p, subseq=False, backtrack=True, metric="cosine"
    )
    wp = wp[::-1]  # 回溯得到的是终点→起点，翻转成起点→终点
    xi = wp[:, 0].astype(np.float64)   # anchor 帧索引
    yi = wp[:, 1].astype(np.float64)   # 目标帧索引

    # ---- 平均余弦相似度（沿路径独立复算一遍，与 DTW 内部代价交叉验证）
    a_vecs = feat_a[:, wp[:, 0]]
    p_vecs = feat_p[:, wp[:, 1]]
    cos = np.sum(a_vecs * p_vecs, axis=0) / np.maximum(
        np.linalg.norm(a_vecs, axis=0) * np.linalg.norm(p_vecs, axis=0), 1e-9
    )
    mean_cos = float(np.mean(cos))

    # ---- 生成 0.5 秒网格 -----------------------------------------
    # DTW 路径里常有「同一 anchor 帧对应多个目标帧」的竖直段（局部让步）。
    # 直接取组内最后一个 y 会把这个让步放大成一次跳跃（实测能让 25 秒内漂移 5 秒）。
    # 这里改用在 anchor 时间 ±SMOOTH_HALF 秒的窗口内取 y 的中位数重采样，
    # 既保留全局漂移趋势，又把局部突跳抹平。
    fps = config.ALIGN_FPS
    SMOOTH_HALF = 1.0
    fx = xi / fps
    fy = yi / fps
    order = np.argsort(fx, kind="stable")
    fx, fy = fx[order], fy[order]

    grid = np.arange(0.0, anchor_dur + 1e-6, config.WARP_STEP)
    lo_idx = np.searchsorted(fx, grid - SMOOTH_HALF, side="left")
    hi_idx = np.searchsorted(fx, grid + SMOOTH_HALF, side="right")
    mapped = np.empty_like(grid)
    for k in range(len(grid)):
        a, b = lo_idx[k], hi_idx[k]
        mapped[k] = float(np.median(fy[a:b])) if b > a else float(np.interp(grid[k], fx, fy))

    mapped = np.maximum.accumulate(mapped) + nudge
    mapped = np.clip(mapped, 0.0, perf_dur)
    mapped = np.maximum.accumulate(mapped)          # 钳制后再次保证单调不减

    # ---- 头部局部校正 + 过渡 -------------------------------------
    head = head_local_map(feat_a, feat_p, log, pid)
    if head is not None:
        g2, m2 = head
        blend_end = HEAD_FIX_SEC + HEAD_BLEND_SEC
        for k, t in enumerate(grid):
            if t <= HEAD_FIX_SEC:
                mapped[k] = float(np.interp(t, g2, m2))
            elif t < blend_end:
                w = (t - HEAD_FIX_SEC) / HEAD_BLEND_SEC
                mapped[k] = (1.0 - w) * float(np.interp(t, g2, m2)) + w * mapped[k]
        mapped = np.clip(mapped, 0.0, perf_dur)
        mapped = np.maximum.accumulate(mapped)
        mean_cos = _map_cos(feat_a, feat_p, grid, mapped)

    data = {
        "performerId": pid,
        "anchorDuration": round(anchor_dur, 3),
        "duration": round(perf_dur, 3),
        "fps": config.ALIGN_FPS,
        "meanCosSim": round(mean_cos, 4),
        "grid": [round(float(g), 3) for g in grid],
        "map": [round(float(m), 3) for m in mapped],
    }
    data["map"] = [max(a, b) for a, b in zip(data["map"], [0.0] + data["map"][:-1])]
    return data


def run(proj: Project, log: Log, force: bool = False) -> dict[str, dict]:
    """执行 S2，产出 work/warp/<id>.json。

    Returns:
        {id: warp 字典}
    """
    log.stage("S2 对齐（chroma_cens + 全局单调 DTW）")
    anchor_pcm = config.DIR_PCM / f"{proj.anchor}.wav"
    if not anchor_pcm.exists():
        raise BuildError(f"缺少 anchor PCM: {anchor_pcm}")
    anchor_dur = audio.duration(anchor_pcm)
    log.line(f"  锚点 = {proj.anchor}（{anchor_dur:.2f}s）")

    feat_a = load_feat(proj.anchor, anchor_pcm, log)
    log.line(f"  anchor 特征 {feat_a.shape}（{feat_a.shape[1] / config.ALIGN_FPS:.1f}s @ "
             f"{config.ALIGN_FPS}fps）")

    out: dict[str, dict] = {}
    rows = []
    for perf in proj.performers:
        if perf.id == proj.anchor:
            continue
        pcm = config.DIR_PCM / f"{perf.id}.wav"
        if not pcm.exists():
            raise BuildError(f"缺少 PCM: {pcm}")
        perf_dur = audio.duration(pcm)
        cand = warpmod.path_of(perf.id)
        if cand.exists() and not force:
            data = json.loads(cand.read_text(encoding="utf-8"))
            if abs(data.get("anchorDuration", 0) - anchor_dur) < 0.05:
                log.line(f"  {perf.id}: 已有对齐结果，跳过（meanCosSim={data['meanCosSim']}）")
                out[perf.id] = data
                rows.append([perf.id, f"{data['meanCosSim']:.4f}", f"{data['duration']:.2f}", "缓存"])
                continue

        feat_p = load_feat(perf.id, pcm, log)
        data = build_warp(perf.id, feat_a, feat_p, anchor_dur, perf_dur, perf.nudge, log)
        warpmod.path_of(perf.id).parent.mkdir(parents=True, exist_ok=True)
        warpmod.path_of(perf.id).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out[perf.id] = data
        drift = data["map"][-1] - data["duration"]
        rows.append([perf.id, f"{data['meanCosSim']:.4f}",
                     f"{data['duration']:.2f}", f"端点差 {drift:+.2f}s"])
        log.line(f"  {perf.id}: meanCosSim={data['meanCosSim']:.4f} · 端点映射 "
                 f"{data['map'][0]:.2f}→{data['map'][-1]:.2f}s（源 {perf_dur:.2f}s）")
        del feat_p

    log.table(["id", "meanCosSim", "时长(s)", "备注"], rows)

    # ---- 验收 ---------------------------------------------------
    sims = [v["meanCosSim"] for v in out.values()]
    worst = min(sims) if sims else 0.0
    ok = all(s >= config.MEANCOSSIM_MIN for s in sims)
    log.metric("4 对齐质量", f"最低 meanCosSim {worst:.4f}", "≥ 0.85", ok)
    if worst < config.MEANCOSSIM_FAIL:
        raise BuildError(f"meanCosSim {worst:.4f} < 0.75，对齐失败（规格 S2 验收），拒绝静默产出")
    if not ok:
        log.warn(f"有演奏家 meanCosSim < {config.MEANCOSSIM_MIN}，需人工试听自检音频确认")

    for pid, d in out.items():
        w = warpmod.Warp(d)
        if not w.is_monotonic():
            raise BuildError(f"{pid}: warp 表非单调不减")
    log.metric("warp 单调性", "全部单调不减", "严格单调", True)

    # ---- 反推乐章边界到 anchor 时间轴 ---------------------------
    movements = derive_movements(proj, out, anchor_dur, log)
    (config.WORK / "movements.json").write_text(
        json.dumps(movements, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def derive_movements(proj: Project, warps: dict[str, dict], anchor_dur: float, log: Log) -> list[dict]:
    """把「分 P 即乐章」的信息经 warp 反推到 anchor 时间轴。

    本曲目实测所有视频都没有 chapters，但贝尔那条的分 P 标题就是三个乐章，
    把它当作乐章边界的外部真值，反查 anchor 时间轴，其它演奏家共用这套边界。

    Args:
        proj: 项目。
        warps: {id: warp 字典}
        anchor_dur: anchor 时长。
        log: 日志。

    Returns:
        [{"index", "label", "start", "end"}]，单位为 anchor 秒；无信息时返回单一乐章。
    """
    votes: list[list[float]] = []
    for perf in proj.performers:
        meta_path = config.DIR_SRC / f"{perf.id}.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        movs = meta.get("movements") or []
        if len(movs) < 2:
            continue
        # 分 P 标题给出的乐章边界是「源时间」，S1 裁剪过（静音缝/区间）必须换算
        pcm_info = json.loads((config.DIR_PCM / f"{perf.id}.json").read_text(encoding="utf-8"))
        removed = pcm_info.get("removed") or []
        starts = [spanmod.source_to_trim(m["start"], removed) for m in movs[1:]]
        if perf.id == proj.anchor:
            votes.append(starts)
            continue
        w = warpmod.Warp(warps[perf.id]) if perf.id in warps else None
        if w is None:
            continue
        votes.append([w.inverse_at(s) for s in starts])

    if not votes:
        log.line("  未发现乐章边界信息，退化为单一乐章")
        return [{"index": 1, "label": "", "start": 0.0, "end": round(anchor_dur, 3)}]

    n = min(len(v) for v in votes)
    bounds = [float(np.median([v[i] for v in votes])) for i in range(n)]
    bounds = sorted(bounds)
    log.line(f"  乐章边界（anchor 秒，{len(votes)} 条来源取中位）："
             + ", ".join(f"{b:.1f}" for b in bounds))

    edges = [0.0] + bounds + [anchor_dur]
    labels = ["第一乐章", "第二乐章", "第三乐章", "第四乐章"]
    return [
        {"index": i + 1, "label": labels[i] if i < len(labels) else f"第{i + 1}段",
         "start": round(edges[i], 3), "end": round(edges[i + 1], 3)}
        for i in range(len(edges) - 1)
        if edges[i + 1] - edges[i] > 5.0
    ]
