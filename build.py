#!/usr/bin/env python3
"""同弦 · 构建入口。

用法：
    python build.py                     # 全流程 S0→S5 + 验收
    python build.py --only s2           # 只跑某一阶段（依赖产物需已存在）
    python build.py --from s2 --to s4   # 跑区间
    python build.py --force s2 s3 s4    # 忽略缓存，强制重跑指定阶段
    python build.py --verify-only       # 只跑验收

设计约束（规格 §9）：
  · 幂等：每阶段用「输入签名」判断是否需要重跑，签名不变则跳过；
  · 可观测：所有参数与实测值写入 build.log；
  · 失败策略：任何验收项不达标 → 非零退出码，不静默降级。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback

from ssm import (config, s0_fetch, s1_norm, s2_align, s2_check, s3_segments,
                 s4_clips, s5_pack, verify)
from ssm.config import BuildError, ensure_dirs, load_project
from ssm.logbook import Log

STAGES = ["s0", "s1", "s2", "s2check", "s3", "s4", "s5"]
TITLES = {
    "s0": "S0 素材登记与抓取",
    "s1": "S1 归一化",
    "s2": "S2 对齐",
    "s2check": "S2.5 自检音频",
    "s3": "S3 自动选段",
    "s4": "S4 切片编码",
    "s5": "S5 打包",
}


def _h(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _file_sig(path) -> str:
    """某份中间产物的内容指纹。

    阶段签名只写「配置」是不够的：s4 真正吃的是 segments.json，s5 吃的是 clips.json。
    踩过的坑 —— 用旧 segments.json 跑过一次 s4，配置签名没变，于是下次就「缓存命中」，
    结果 clips.json 还是旧的 48 片段，而 segments.json 已经是新的 53 段，
    验收里 #12 #15 直接对不上。
    """
    p = __import__("pathlib").Path(path)
    return hashlib.sha1(p.read_bytes()).hexdigest()[:16] if p.exists() else ""


def signatures(proj) -> dict[str, str]:
    """逐级累加的阶段签名：上游一变，下游全部失效。"""
    deltas = {
        "s0": [[p.id, p.bvid, p.parts] for p in proj.performers],
        "s1": [config.TARGET_LUFS, config.TARGET_TP, config.SILENCE_DBFS,
               config.SILENCE_MIN_SEC, config.GAP_MIN_SEC, config.SR_OUTPUT,
               config.SPAN_VERSION],
        "s2": [proj.anchor, config.SR_ANALYSIS, config.HOP, config.ALIGN_FPS,
               config.ALIGN_SMOOTH, config.WARP_STEP, config.ALIGN_VERSION,
               [[p.id, p.nudge] for p in proj.performers]],
        "s2check": [config.WORK.joinpath("check").exists()],
        "s3": [proj.segment_count, proj.seg_duration, proj.seg_min_gap,
               proj.seg_mode, proj.seg_stride, proj.pack_mode,
               config.W_MS_RATIO, config.W_ACTIVITY, config.W_FLATNESS,
               proj.manual_segments],
        "s4": [proj.seg_duration, config.FADE_MS, config.CLIP_CODEC, config.CLIP_BITRATE,
               _file_sig(config.WORK / "segments.json")],
        "s5": [[f.name, f.stat().st_size] for f in sorted(config.WEB.glob("*"))]
              + [_file_sig(config.WORK / "clips.json"), proj.pack_mode],
    }
    out: dict[str, str] = {}
    carry = ""
    for stage in STAGES:
        carry = _h([carry, deltas[stage]])
        out[stage] = carry
    return out


def outputs_of(stage: str, proj) -> list[str]:
    """各阶段的产物（用于判断缓存是否可复用）。"""
    if stage == "s0":
        return [str(config.DIR_SRC / f"{p}.json") for p in proj.ids]
    if stage == "s1":
        return [str(config.DIR_PCM / f"{p}.wav") for p in proj.ids]
    if stage == "s2":
        return [str(config.DIR_WARP / f"{p}.json") for p in proj.ids if p != proj.anchor]
    if stage == "s2check":
        return [str(config.DIR_CHECK / "README.txt")]
    if stage == "s3":
        return [str(config.WORK / "segments.json")]
    if stage == "s4":
        return [str(config.WORK / "clips.json")]
    if stage == "s5":
        if proj.pack_mode == "static":
            return [str(config.DIR_DIST / config.STATIC_DIRNAME / "index.html")]
        return [str(config.DIR_DIST / "同弦.html"), str(config.DIR_DIST / "bank.json")]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同弦构建")
    ap.add_argument("--only", help="只跑某阶段（s0..s5）")
    ap.add_argument("--from", dest="from_stage", help="从某阶段开始")
    ap.add_argument("--to", dest="to_stage", help="到某阶段结束")
    ap.add_argument("--force", nargs="*", default=[], help="强制重跑的阶段，空表示全部")
    ap.add_argument("--verify-only", action="store_true", help="只跑验收")
    args = ap.parse_args(argv)

    ensure_dirs()
    log = Log()
    try:
        proj = load_project()
    except BuildError as exc:
        log.line(f"配置错误: {exc}")
        return 2

    log.line(f"曲目：{proj.title}（{proj.composer}）")
    mode_note = "（上限，实际切到乐曲末尾为止）" if proj.pack_mode == "static" else ""
    log.line(f"演奏家 {proj.n} 位 · 锚点 {proj.anchor} · 段数 {proj.segment_count}{mode_note} · "
             f"片段 {proj.clip_count} · 打包 "
             f"{'静态站点（音频外置）' if proj.pack_mode == 'static' else '单文件内联'}")
    log.kv(参数路径=str(config.ROOT / "project.json"))
    try:
        log.line("  " + proj.check_budget())
    except BuildError as exc:
        log.line(f"!! {exc}")
        return 2

    if args.only:
        todo = [args.only]
    else:
        start = STAGES.index(args.from_stage) if args.from_stage else 0
        end = STAGES.index(args.to_stage) if args.to_stage else len(STAGES) - 1
        todo = STAGES[start:end + 1]
    for stage in todo:
        if stage not in STAGES:
            log.line(f"未知阶段 {stage}")
            return 2

    sigs = signatures(proj)
    manifest = {}
    if config.MANIFEST_PATH.exists():
        try:
            manifest = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}

    force_all = "--force" in sys.argv and not any(a in STAGES for a in args.force)
    t_all = time.time()

    if not args.verify_only:
        for stage in todo:
            outs = outputs_of(stage, proj)
            cached = (manifest.get(stage, {}).get("sig") == sigs[stage]
                      and all(__import__("pathlib").Path(o).exists() for o in outs))
            forced = force_all or stage in args.force
            log.line("")
            log.line(f"---- 阶段 {stage}（{TITLES[stage]}）"
                     f"{' · 缓存命中，跳过' if cached and not forced else ''}")
            if cached and not forced:
                continue
            try:
                if stage == "s0":
                    s0_fetch.run(proj, log, force=forced)
                elif stage == "s1":
                    s1_norm.run(proj, log, force=forced)
                elif stage == "s2":
                    s2_align.run(proj, log, force=forced)
                elif stage == "s2check":
                    s2_check.run(proj, log)
                elif stage == "s3":
                    s3_segments.run(proj, log, force=forced)
                elif stage == "s4":
                    s4_clips.run(proj, log, force=forced)
                elif stage == "s5":
                    s5_pack.run(proj, log, force=forced)
            except BuildError as exc:
                log.line(f"!! {stage} 失败：{exc}")
                log.finish(False)
                return 1
            manifest[stage] = {"sig": sigs[stage], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            config.MANIFEST_PATH.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 验收 ----------------------------------------------------
    try:
        rows, ok = verify.run_checks(proj, log)
    except FileNotFoundError as exc:
        log.line(f"产物不全，跳过验收：{exc}")
        log.finish(False)
        return 1
    verify.print_table(rows, log)
    log.report["acceptance"] = rows
    log.finish(ok)
    log.line(f"总耗时 {(time.time() - t_all) / 60:.1f} 分钟")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
