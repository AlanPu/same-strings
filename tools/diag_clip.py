"""诊断 3：逐段核对对齐是否局部正确。

全局 meanCosSim 只能说明整条路径平均像，掩盖不了「某一段落在错误位置」。
这里对每个 (段落, 演奏家)：拿锚点的 25 秒当探针，在「warp 给出的位置 ±40 秒」范围内
做子序列搜索，报告**最佳局部偏移**与相似度。

偏移接近 0 且相似度高 → 该段对齐正确；
偏移很大或相似度低 → 该段的 warp 错了（或这一段本身是 tutti/静音）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import librosa
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ssm import config, span as spanmod, warp as warpmod  # noqa: E402

SEARCH = 40.0
FPS = config.ALIGN_FPS


def main() -> None:
    proj = config.load_project()
    data = json.loads((config.WORK / "segments.json").read_text(encoding="utf-8"))
    feats = {}
    for pid in proj.ids:
        p = config.DIR_PCM / f"{pid}.wav"
        feats[pid] = spanmod.load_features(pid, p, "pool", None)  # type: ignore[arg-type]
    warps = warpmod.load_all(proj)

    print(f"{'段':<4}{'演奏家':<10}{'warp位置':>9}{'最佳偏移':>9}{'相似度':>8}  判定")
    worst = []
    for seg in data["segments"]:
        t0, dur = seg["anchorStart"], seg["duration"]
        fa = feats[proj.anchor][:, int(t0 * FPS):int((t0 + dur) * FPS)]
        for perf in proj.performers:
            if perf.id == proj.anchor:
                continue
            w = warps[perf.id]
            pos = w.at(t0)
            lo = max(0.0, pos - SEARCH)
            hi = min(len(feats[perf.id][0]) / FPS, pos + dur + SEARCH)
            ft = feats[perf.id][:, int(lo * FPS):int(hi * FPS)]
            _, wp = librosa.sequence.dtw(X=fa, Y=ft, subseq=True, backtrack=True,
                                         metric="cosine")
            best = lo + float(np.min(wp[:, 1])) / FPS
            cos = float(np.mean(np.sum(fa[:, wp[:, 0]] * ft[:, wp[:, 1]], axis=0)))
            off = best - pos
            bad = abs(off) > 1.5 or cos < 0.85
            print(f"{seg['id']:<4}{perf.id:<10}{pos:9.1f}{off:+9.1f}{cos:8.3f}  "
                  f"{'!! 偏移异常' if bad else 'ok'}")
            if bad:
                worst.append((seg["id"], perf.id, round(off, 1), round(cos, 3)))
    print()
    print(f"异常 {len(worst)} / {len(data['segments']) * (proj.n - 1)}：{worst}")


if __name__ == "__main__":
    main()
