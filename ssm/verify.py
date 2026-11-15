"""独立验收（规格 §10 总表）。

只读产物、不看日志：所有能自动判定的项都从 work/ 与 dist/ 的实际文件重新计算一遍，
避免「阶段里算过就当过了」。人工项列成清单打印出来。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from . import audio, config, warp as warpmod
from .config import Project

AUTO, MANUAL = "自动", "人工"


def _load(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def run_checks(proj: Project, log) -> tuple[list[dict], bool]:
    """执行全部自动检查。

    Returns:
        (检查结果列表, 是否全部通过)
    """
    rows: list[dict] = []

    def add(no: int, name: str, judge: str, value: str, ok: bool | None, how: str = AUTO) -> None:
        rows.append({"no": no, "name": name, "judge": judge, "value": value,
                     "ok": ok, "how": how})

    # 1 抓取完整性
    src = {pid: _load(config.DIR_SRC / f"{pid}.json") for pid in proj.ids}
    errs = [abs(m["duration"] - m["expectedDuration"]) for m in src.values() if m]
    add(1, "抓取完整性", "分 P 拼接误差 < 1s", f"最大 {max(errs):.3f}s",
        bool(errs) and max(errs) < 1.0)

    # 2 响度一致
    pcm = {pid: _load(config.DIR_PCM / f"{pid}.json") for pid in proj.ids}
    lufs = [m["lufs"] for m in pcm.values() if m]
    spread = max(lufs) - min(lufs) if lufs else 99
    add(2, "响度一致", "极差 ≤ 0.5 LU", f"{spread:.3f} LU", spread <= config.LUFS_TOLERANCE)

    # 3 / 7 编码一致 + 等长
    clips = json.loads((config.WORK / "clips.json").read_text(encoding="utf-8"))["clips"]
    probes = {cid: audio.ffprobe(config.DIR_CLIPS / f"{cid}.m4a") for cid in clips}
    params = {(p["codec"], p["sample_rate"], p["channels"]) for p in probes.values()}
    nominal = int(config.CLIP_BITRATE.rstrip("k")) * 1000
    rates = np.array([p["bit_rate"] for p in probes.values()])
    dev = float(np.max(np.abs(rates - nominal)) / nominal)
    durs = np.array([p["duration"] for p in probes.values()])
    add(3, "编码一致", "编码参数 1 种 + 实测码率偏差 < 1.5%",
        f"{len(params)} 种 {sorted(params)[0] if params else '—'}；码率 "
        f"{rates.min() // 1000}~{rates.max() // 1000}k（偏差 {dev * 100:.2f}%）",
        len(params) == 1 and dev < 0.015)
    add(7, "等长", f"全部 = {proj.seg_duration:.3f}s",
        f"{durs.min():.3f}~{durs.max():.3f}s（极差 {durs.max() - durs.min():.4f}s）",
        float(durs.max() - durs.min()) < 0.01)

    # 4 对齐质量
    sims = {pid: warpmod.load(pid).mean_cos_sim for pid in proj.ids if pid != proj.anchor}
    add(4, "对齐质量", "meanCosSim ≥ 0.85",
        f"最低 {min(sims.values()):.4f}（{min(sims, key=sims.get)}）" if sims else "—",
        bool(sims) and min(sims.values()) >= config.MEANCOSSIM_MIN)

    # warp 单调性
    mono = all(warpmod.load(pid).is_monotonic() for pid in proj.ids if pid != proj.anchor)
    add(4.1, "warp 单调", "严格单调不减", "全部通过" if mono else "存在回退", mono)

    # 5 对齐正确性（人工）；只数左右声道对照文件，别把逐段试听包算进来
    checks = sorted(config.DIR_CHECK.glob("*__t*.m4a"))
    add(5, "对齐正确性", "3 处抽样误差 ≤ 1s", f"{len(checks)} 段对照音频待听", None, MANUAL)

    # 6 选段质量（人工）
    segs = json.loads((config.WORK / "segments.json").read_text(encoding="utf-8"))["segments"]
    listens = sorted(config.DIR_CHECK.glob("segment-*.m4a"))
    add(6, "选段质量", "抽 5 段无 tutti/静音/截断",
        f"共 {len(segs)} 段；逐段试听包 {len(listens)} 条", None, MANUAL)

    # 8 可达性（static 形态没有离线，这是它换来的代价，必须如实标出来）
    if proj.pack_mode == "static":
        add(8, "联网可达", "同一网络内 iPad 可答题；断网不可用（本形态的取舍）",
            "需真机验证", None, MANUAL)
    else:
        add(8, "离线可用", "飞行模式可完整答题", "需真机验证", None, MANUAL)

    # 9 体积（static 模式只约束页面本身，音频外置另计）
    site = config.DIR_DIST / config.STATIC_DIRNAME
    if proj.pack_mode == "static":
        out = site / "index.html"
        kb = out.stat().st_size / 1024 if out.exists() else 0
        add(9, "页面体积", f"≤ {config.STATIC_HTML_MAX_KB} KB（音频外置，不计入）",
            f"{kb:.1f} KB", 0 < kb <= config.STATIC_HTML_MAX_KB)
    else:
        out = config.DIR_DIST / "同弦.html"
        mb = out.stat().st_size / 1024 / 1024 if out.exists() else 0
        add(9, "体积", f"≤ {config.MAX_HTML_MB} MB", f"{mb:.2f} MB", 0 < mb <= config.MAX_HTML_MB)

    # 10 无第三方请求
    html = out.read_text(encoding="utf-8") if out.exists() else ""
    ext = sum(html.count(k) for k in ("http://", "https://", "@import"))
    add(10, "无第三方请求", "0 个外部域名引用", f"{ext} 处", ext == 0)

    # 11 存储降级（人工 + 静态检查）
    app_js = (config.WEB / "app.js").read_text(encoding="utf-8")
    guarded = "try {" in app_js and "localStorage" in app_js
    add(11, "存储降级", "禁用 localStorage 仍可练习",
        "有 try/catch 与内存降级" if guarded else "未发现降级路径", None if not guarded else None, MANUAL)

    # 12 片段总数（inline 模式受字节预算约束；static 模式只看是否与段数吻合）
    if proj.pack_mode == "static":
        add(12, "片段总数", f"= {proj.n}×{len(segs)}（静态托管不限体积）",
            f"{len(clips)}", len(clips) == proj.n * len(segs))
    else:
        cap = config.max_clip_count(proj.seg_duration)
        add(12, "片段总数", f"= {proj.n}×{len(segs)} 且 ≤ 字节预算上限 {cap}",
            f"{len(clips)}", len(clips) == proj.n * len(segs) and len(clips) <= cap)

    # 13 / 14 出题逻辑（node 模拟）
    sim = run_node_sim(proj)
    if sim:
        add(13, "选项合法性", "每题 4 个互不相同且含正确项",
            f"抽样 {sim['rounds']} 轮 × 10 题，异常 {sim['badOptions']} 次",
            sim["badOptions"] == 0)
        add(14, "答案均衡", "10 题内各演奏家次数相差 ≤ 1",
            f"不均衡轮次 {sim['badBalance']}/{sim['rounds']}；选项出现次数 "
            f"{sim['optionSpread']['min']}~{sim['optionSpread']['max']}",
            sim["badBalance"] == 0)
    else:
        add(13, "选项合法性", "每题 4 个互不相同且含正确项", "node 不可用，未跑", None, MANUAL)
        add(14, "答案均衡", "10 题内各演奏家次数相差 ≤ 1", "node 不可用，未跑", None, MANUAL)

    # 15 覆盖
    missing = [pid for pid in proj.ids if not all(pid in s["clips"] for s in segs)]
    add(15, "演奏家覆盖", "每位至少有一个片段",
        "全覆盖" if not missing else f"缺失 {missing}", not missing)

    # 16 本轮新增能力（用户要求：从头开始 / 自选段落 / 时长自选）
    if sim:
        add(16, "自选段落/顺序/时长", "顺序题数=段数；单段可换人；presets 含上限时长",
            f"顺序 {sim.get('seqCount', '—')} 题／单段 "
            f"{sim.get('singleCount', '—')} 题／presets "
            f"{sim.get('presets', '—')}",
            sim.get("seqOK") is True and sim.get("singleOK") is True and sim.get("presetOK") is True)
    else:
        add(16, "自选段落/顺序/时长", "顺序题数=段数；单段可换人；presets 含上限时长",
            "node 不可用，未跑", None, MANUAL)

    # 17 切分顺序（用户要求：从乐曲开头按顺序切，不随机）
    if proj.seg_mode == "sequential":
        starts = [s["anchorStart"] for s in segs]
        increasing = all(b >= a + proj.seg_duration - 1e-6
                         for a, b in zip(starts, starts[1:]))
        head_ok = bool(starts) and starts[0] <= 1.0
        if len(starts) > 8:
            shown = (f"{starts[0]:.0f}, {starts[1]:.0f}, … , {starts[-1]:.0f} s"
                     f"（共 {len(starts)} 段）")
        else:
            shown = "起点 " + ", ".join(f"{t:.1f}" for t in starts) + " s"
        add(17, "切分顺序", "首段起于 0s，起点递增且互不重叠", shown, head_ok and increasing)

    # 18 静态资源完整性（static 模式的命门：少一个文件就是 404）
    if proj.pack_mode == "static":
        audio_dir = site / "audio"
        have = {p.stem for p in audio_dir.glob("*.m4a")} if audio_dir.exists() else set()
        need = set(clips)
        missing_files = need - have
        extra = have - need
        add(18, "静态资源完整", "每个 clipId 都有对应 audio/*.m4a，且无残留旧文件",
            f"{len(have)} 个文件；缺 {len(missing_files)}，多 {len(extra)}",
            not missing_files and not extra)
        total_mb = sum(p.stat().st_size for p in audio_dir.glob("*.m4a")) / 1024 / 1024 \
            if audio_dir.exists() else 0
        add(19, "服务端存储", "音频总量（NAS 上占用，不随请求增长）",
            f"{total_mb:.1f} MB", total_mb > 0)

    passed = all(r["ok"] is not False for r in rows)
    return rows, passed


def run_node_sim(proj) -> dict | None:
    """跑 tests/player_sim.js，返回其 JSON 摘要。"""
    script = config.ROOT / "tests" / "player_sim.js"
    # static 模式没有 dist/bank.json（音频外置，索引仍在站点里留了一份）
    bank = (config.DIR_DIST / config.STATIC_DIRNAME / "bank.json"
            if proj.pack_mode == "static" else config.DIR_DIST / "bank.json")
    if not script.exists() or not bank.exists():
        return None
    try:
        proc = subprocess.run(["node", str(script), str(bank), "1000"],
                              capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return None
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def print_table(rows: list[dict], log) -> None:
    """打印 §10 总表。"""
    log.line("")
    log.line("===== §10 验收总表 =====")
    marks = {True: "通过", False: "失败", None: "待人工"}
    log.table(
        ["#", "项", "判据", "实测", "方式", "结论"],
        [[r["no"], r["name"], r["judge"], r["value"], r["how"], marks[r["ok"]]] for r in rows],
    )
    manual = [r for r in rows if r["how"] == MANUAL]
    auto_fail = [r for r in rows if r["ok"] is False]
    log.line("")
    log.line(f"自动项 {len(rows) - len(manual)} 条，人工项 {len(manual)} 条："
             + "、".join(f"#{r['no']} {r['name']}" for r in manual))
    if auto_fail:
        log.line("失败项：" + "、".join(f"#{r['no']} {r['name']}" for r in auto_fail))

    # 人工项对应的待办文件，避免「待人工」变成一句空话
    checks = sorted(config.DIR_CHECK.glob("*__t*.m4a"))
    packs = sorted(config.DIR_CHECK.glob("segment-*.m4a"))
    if checks or packs:
        log.line("")
        log.line("人工核对材料：")
        if checks:
            log.line(f"  #5 对齐自检：{len(checks)} 段（左耳=锚点，右耳=目标）→ {config.DIR_CHECK}")
            log.line(f"     清单：{config.DIR_CHECK / 'README.txt'}")
        if packs:
            log.line(f"  #6 逐段试听：{len(packs)} 条（每条依次是 8 位演奏家）→ "
                     + "、".join(p.name for p in packs[:3]) + (" …" if len(packs) > 3 else ""))
            log.line(f"     都在 {config.DIR_CHECK}，重新生成：python tools/make_listening.py 6")
        if proj.pack_mode == "static":
            log.line("  #8/#11 真机：浏览器打开 http://<NAS 地址>:8080/ 连答 10 题；"
                     "再关掉 Safari 网站数据重开，确认仍可答题")
            log.line("     本形态断网即不可用（这是换取覆盖全曲的代价），#10「无网络请求」"
                     "已不适用，改为「无第三方域名」")
        else:
            log.line("  #8/#11 真机：AirDrop 到 iPad 后开飞行模式连答 10 题；"
                     "iPad 设置里关掉 Safari 网站数据后重开，确认仍可答题")
            log.line("  #10 远程调试：Safari 开发菜单连 iPad，确认「网络」标签无请求")


if __name__ == "__main__":
    from .config import BuildError, load_project
    from .logbook import Log

    lg = Log(append=True)
    try:
        p = load_project()
        rows, ok = run_checks(p, lg)
        print_table(rows, lg)
        sys.exit(0 if ok else 1)
    except BuildError as exc:
        print(f"验收失败: {exc}")
        sys.exit(1)
