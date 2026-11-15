"""有效演奏区间定位（S1 的内部步骤）。

同一个视频里经常混着掌声、入场、返场甚至别的曲目（实测弗朗多出 326 秒）。
不切掉会有两个后果：掌声参与响度积分；全局 DTW 被迫吞下与乐曲无关的几分钟。

做法：拿锚点在 5 个位置各取 35 秒当探针，用子序列 DTW 在目标音轨上定位，
对 (锚点时间, 目标时间) 做最小二乘直线拟合，外推出乐曲的起点与终点。
这里用子序列搜索只是为了「找边界」，正式对齐仍是全局单调 DTW（规格 §S2）。
"""
from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np
from scipy.ndimage import gaussian_filter1d

from . import config
from .logbook import Log

DIR_FEAT = config.WORK / "feat"
PROBE_FRACS = (0.10, 0.28, 0.46, 0.64, 0.82)
PROBE_LEN = 35.0        # 探针长度（秒）
MIN_COS = 0.80          # 探针可信下限
SPEED_RANGE = (0.80, 1.30)
PAD = 1.0               # 边界外留 1 秒，避免切掉第一个音头


def pooled_features(y: np.ndarray, sr: int = config.SR_ANALYSIS) -> np.ndarray:
    """chroma_cens → 聚合到 ALIGN_FPS → 1 秒平滑 → 逐帧 L2 归一化。

    Args:
        y: 单声道波形。
        sr: 采样率。

    Returns:
        (12, T) 特征矩阵。
    """
    cens = librosa.feature.chroma_cens(y=y, sr=sr, hop_length=config.HOP)
    frame_rate = sr / config.HOP
    times = np.arange(cens.shape[1]) / frame_rate
    bins = np.floor(times * config.ALIGN_FPS).astype(np.int64)
    n_bins = int(bins[-1]) + 1
    counts = np.bincount(bins, minlength=n_bins).astype(np.float64)
    sums = np.zeros((cens.shape[0], n_bins), dtype=np.float64)
    for k in range(cens.shape[0]):
        sums[k] = np.bincount(bins, weights=cens[k], minlength=n_bins)
    pooled = (sums / np.maximum(counts, 1)).astype(np.float32)
    pooled = gaussian_filter1d(pooled, sigma=config.ALIGN_SMOOTH * config.ALIGN_FPS / 2,
                               axis=1, mode="nearest")
    norm = np.linalg.norm(pooled, axis=0, keepdims=True)
    return (pooled / np.maximum(norm, 1e-9)).astype(np.float32)


def load_features(pid: str, wav: Path, tag: str, log: Log) -> np.ndarray:
    """带缓存地取某条音轨的对齐特征。

    Args:
        pid: 演奏家 id。
        wav: PCM 路径。
        tag: 缓存后缀，区分原始轨（poolraw）与最终轨（pool）。
    """
    DIR_FEAT.mkdir(parents=True, exist_ok=True)
    cache = DIR_FEAT / f"{pid}.{tag}.npy"
    if cache.exists() and cache.stat().st_mtime >= wav.stat().st_mtime:
        return np.load(cache)
    y, _ = librosa.load(str(wav), sr=config.SR_ANALYSIS, mono=True)
    log.line(f"   色度特征 {pid}({tag})：{len(y) / config.SR_ANALYSIS:.1f}s 音频")
    feat = pooled_features(y)
    np.save(cache, feat)
    del y
    return feat


def detect_span(
    feat_a: np.ndarray,
    feat_p: np.ndarray,
    anchor_dur: float,
    target_dur: float,
    pid: str,
    log: Log,
) -> tuple[float, float, dict]:
    """定位目标音轨里「与锚点对应的乐曲」的起止时间。

    Args:
        feat_a: 锚点特征。
        feat_p: 目标特征。
        anchor_dur: 锚点时长（秒）。
        target_dur: 目标时长（秒）。
        pid: 演奏家 id。
        log: 日志。

    Returns:
        (start, end, info)；无法可靠定位时退化为 (0, target_dur)。
    """
    fps = config.ALIGN_FPS
    anchor_frames = feat_a.shape[1]
    points: list[tuple[float, float, float]] = []

    for frac in PROBE_FRACS:
        t0 = min(max(0.0, frac * anchor_dur), max(0.0, anchor_dur - PROBE_LEN))
        f0 = int(t0 * fps)
        f1 = min(anchor_frames, f0 + int(PROBE_LEN * fps))
        if f1 - f0 < 20:
            continue
        probe = feat_a[:, f0:f1]
        _, wp = librosa.sequence.dtw(X=probe, Y=feat_p, subseq=True,
                                     backtrack=True, metric="cosine")
        start = float(np.min(wp[:, 1])) / fps
        cos = float(np.mean(np.sum(probe[:, wp[:, 0]] * feat_p[:, wp[:, 1]], axis=0)))
        points.append((t0 + (f1 - f0) / fps / 2, start + (f1 - f0) / fps / 2, cos))

    good = [p for p in points if p[2] >= MIN_COS]
    info = {"pid": pid, "probes": [(round(a, 1), round(b, 1), round(c, 3)) for a, b, c in good],
            "anchorDur": round(anchor_dur, 2), "targetDur": round(target_dur, 2)}

    if len(good) < 3:
        log.warn(f"{pid}: 有效探针只有 {len(good)} 个，跳过区间裁剪（不冒险切掉乐曲）")
        return 0.0, target_dur, {**info, "fallback": True}

    a_t = np.array([p[0] for p in good], dtype=np.float64)
    a_p = np.array([p[1] for p in good], dtype=np.float64)
    slope, intercept = np.polyfit(a_t, a_p, 1)
    head = float(intercept - PAD)
    tail = float(intercept + slope * anchor_dur + PAD)
    speed = float(slope)
    info.update({"slope": round(speed, 4), "head": round(head, 2), "tail": round(tail, 2)})

    if not (SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]):
        log.warn(f"{pid}: 拟合速度比 {speed:.3f} 不合理，放弃裁剪")
        return 0.0, target_dur, {**info, "fallback": True}
    if head < -3 or tail > target_dur + 3 or tail - head < 0.8 * anchor_dur:
        log.warn(f"{pid}: 区间 {head:.1f}~{tail:.1f}s 越界或过短，放弃裁剪")
        return 0.0, target_dur, {**info, "fallback": True}

    head = max(0.0, head)
    tail = min(target_dur, tail)
    return head, tail, info


def source_to_trim(t_source: float, removed: list[list[float]]) -> float:
    """把原始时间换算到被裁剪后的时间轴。

    Args:
        t_source: 原始（解码后）时间。
        removed: [[start, end], ...] 按时间升序的被删区间（原始时间轴）。

    Returns:
        裁剪后的时间。
    """
    shift = 0.0
    for a, b in removed:
        if t_source <= a:
            break
        shift += min(b, t_source) - a
    return t_source - shift


# ---------------------------------------------------------------- 头部对齐
# 掌声不是静音，削静电平抓不到它；而各版本前面残留的长度差得很远
# （实测从 1 秒到 80 多秒都有，混着掌声、静场、甚至上台前的调音）。
# 不裁掉这段，全局 DTW 的端点会被非音乐内容硬拽，头部速度比能到 4 倍，
# 于是「从 0 秒开始切」切出来的第一段就是掌声。
#
# 这里不去判断「哪段是掌声」（谱平坦度对低电平的调音/空调声分不出来），
# 而是问「同一条音轨的开头，在别的音轨里出现在哪个时刻」：
#   ① 以锚点开头 30 秒为探针，定位到其余各条 → 得到相对偏移；
#   ② 偏移最小的那条，就是「前面废料最少」的一条，把它当基准；
#   ③ 用基准的开头再量一遍，各条按同一音乐时刻对齐；基准自己裁 0。
# 这样各条新时间轴的 0 秒都落在同一个音乐位置上。
HEAD_PROBE = 30.0            # 探针长度（秒）
HEAD_SEARCH = 240.0          # 在目标开头多少秒以内找起点
HEAD_MAX = 120.0             # 允许裁掉的头部上限，超过就认为定位失败
HEAD_MIN_COS = 0.80          # 探针可信下限


def locate_start(feat_ref: np.ndarray, feat_p: np.ndarray) -> tuple[float, float]:
    """用某条音轨开头 30 秒作探针，定位它在目标轨里出现的位置（子序列 DTW）。

    Args:
        feat_ref: 参考轨特征（12, T）。
        feat_p: 目标轨特征（12, T'）。

    Returns:
        (起点秒数, 平均余弦相似度)；无法定位时返回 (0.0, 0.0)。
    """
    fps = config.ALIGN_FPS
    n = min(feat_ref.shape[1], int(HEAD_PROBE * fps))
    probe = feat_ref[:, :n]
    y_head = feat_p[:, : int(HEAD_SEARCH * fps)]
    if probe.shape[1] < 10 or y_head.shape[1] < 10:
        return 0.0, 0.0
    _, wp = librosa.sequence.dtw(X=probe, Y=y_head, subseq=True,
                                 backtrack=True, metric="cosine")
    ii, jj = wp[:, 0], wp[:, 1]
    # 取路径最前面几帧对应位置的低分位，避开单帧跳变
    start = float(np.percentile(jj[ii <= ii.min() + 3], 10)) / fps
    cos = float(np.mean(np.sum(probe[:, ii] * y_head[:, jj], axis=0)))
    return start, cos


def _matrix(feats: dict[str, np.ndarray], log: Log) -> dict[tuple[str, str], tuple[float, float]]:
    """两两定位矩阵：M[(i, j)] = i 的开头在 j 里出现在第几秒。

    Args:
        feats: {id: 特征}。
        log: 日志。

    Returns:
        {(i, j): (秒数, 余弦)}，不含 i == j。
    """
    out: dict[tuple[str, str], tuple[float, float]] = {}
    ids = list(feats)
    for i in ids:
        for j in ids:
            if i != j:
                out[(i, j)] = locate_start(feats[i], feats[j])
    return out


def detect_heads(raws: dict[str, Path], feats: dict[str, np.ndarray], log: Log) -> dict[str, float]:
    """确定每条音轨应从头部裁掉多少秒，使所有版本的新 0 秒落在同一音乐时刻。

    判据来自「位移模型」：设第 p 条在音乐开始前有 junk_p 秒废料，
    则 i 的开头会出现在 j 的第 (junk_j - junk_i) 秒。于是
      · 前面没有废料的轨（junk=0），它那一行的值就等于别人的 junk_j，全是非负数；
      · 废料越多的轨，它那一行越容易被截到 0、行和越小。
    所以「行和最大」的那条就是废料最少（开头即音乐）的一条，用它那一行当裁切量。

    Args:
        raws: {id: 解码后的 PCM 路径}（只用于保持顺序与日志）。
        feats: {id: 该音轨的对齐特征}。
        log: 日志。

    Returns:
        {id: 头部应裁掉的秒数}；全部为 0 表示不做头部对齐。
    """
    ids = list(feats)
    if len(ids) < 3:
        return {pid: 0.0 for pid in ids}

    mat = _matrix(feats, log)
    capped = 100.0
    scores = {
        i: sum(min(mat[(i, j)][0], capped) for j in ids if j != i)
        for i in ids
    }
    base = max(ids, key=lambda i: scores[i])
    log.line("  两两定位行和：" + "  ".join(f"{i}={scores[i]:.0f}" for i in ids))
    log.line(f"  基准轨 {base}（行和最大 → 开头即音乐，废料最少）")

    heads: dict[str, float] = {}
    for pid in ids:
        if pid == base:
            heads[pid] = 0.0
            continue
        start, cos = mat[(base, pid)]
        if cos < HEAD_MIN_COS or start > HEAD_MAX:
            log.warn(f"  {pid}: 起点定位不可靠（{start:.2f}s，cos {cos:.3f}），保守取 0")
            heads[pid] = 0.0
        else:
            heads[pid] = max(0.0, start)
    log.line("  头部裁切：" + "  ".join(f"{pid}={heads[pid]:.1f}s" for pid in ids))
    return heads

