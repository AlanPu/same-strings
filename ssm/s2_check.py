"""S2.5 — 自检音频（必须实现）。

把 anchor 的 25 秒和某演奏家映射到的 25 秒分左右声道同步播放：
左耳 = anchor，右耳 = 目标。听同一处乐句就说明对齐正确。
这是把「看不见的对齐错误」变成「听得见的错误」的唯一低成本手段（规格 §S2.5）。
"""
from __future__ import annotations

from pathlib import Path

from . import audio, config, warp as warpmod
from .config import Project
from .logbook import Log

FRACTIONS = (0.15, 0.50, 0.85)   # 早 / 中 / 晚 三个抽查位置
CHECK_LEN = 25.0


def run(proj: Project, log: Log) -> list[Path]:
    """生成全部对照音频。

    Returns:
        生成的文件路径列表。
    """
    log.stage("S2.5 自检音频")
    anchor_pcm = config.DIR_PCM / f"{proj.anchor}.wav"
    anchor_dur = audio.duration(anchor_pcm)
    made: list[Path] = []
    # 上一轮的对照片先清掉，否则重跑会累积出两份，人工清点时数不清
    for stale in config.DIR_CHECK.glob("*__t*.m4a"):
        stale.unlink()
    lines = [
        "同弦 · 对齐自检",
        "===================",
        "每段音频：左耳 = 锚点（%s），右耳 = 目标演奏家。" % proj.anchor,
        "判断标准：两耳应听到同一处乐句（允许细微错位）；若旋律错开半句以上 → 对齐失败。",
        "",
    ]

    for perf in proj.performers:
        if perf.id == proj.anchor:
            continue
        w = warpmod.load(perf.id)
        perf_pcm = config.DIR_PCM / f"{perf.id}.wav"
        perf_dur = audio.duration(perf_pcm)
        for frac in FRACTIONS:
            t_a = min(max(0.0, anchor_dur * frac), anchor_dur - CHECK_LEN - 0.1)
            t_p = min(max(0.0, w.at(t_a)), max(0.0, perf_dur - CHECK_LEN - 0.1))
            dst = config.DIR_CHECK / f"{perf.id}__t{t_a:06.1f}.m4a"
            audio.stereo_ab(anchor_pcm, perf_pcm, dst, t_a, t_p, CHECK_LEN)
            made.append(dst)
            lines.append(f"{dst.name}   anchor {t_a:7.1f}s  ↔  {perf.id} {t_p:7.1f}s"
                         f"   （映射差 {t_p - t_a:+.2f}s）")
        log.line(f"  {perf.id}: 3 段对照已生成")

    lines += [
        "",
        "人工核对记录（填完后即为 §10 第 5 项）",
        "----------------------------------------",
    ]
    for p in made:
        lines.append(f"[ ] {p.name}   结论: ______")
    (config.DIR_CHECK / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.line(f"  共 {len(made)} 段 → {config.DIR_CHECK}（含 README.txt 核对清单）")
    log.metric("5 对齐正确性", f"{len(made)} 段待听", "人工抽听误差 ≤1s", True)
    return made
