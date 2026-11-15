"""S5 — 打包。

两种形态（project.json 的 pack.mode）：

  · inline（默认）：单个自包含 HTML。音频以 base64 放进
    <script type="application/json" id="audio">，运行时 atob → Blob → objectURL。
    不用 data URI 直连（iOS Safari 对长 data URI 不可靠）。所有 CSS/JS 内联，零外部资源。
    受 MAX_HTML_MB 约束，因此段数必须由体积预算反推。
  · static：静态站点。index.html 只装代码与索引（几十 KB），音频外置为 audio/*.m4a，
    由 Web 服务器按需发送。不受体积约束，可以覆盖全曲。
    音频请求走相对 URL，需要服务器支持 Range（iOS 的 <audio> 会发 206 探测）。

两种形态共用同一份 web/app.js：urlOf() 先查内联表，查不到就回退到相对 URL。
"""
from __future__ import annotations

import base64
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import config
from .config import BuildError, Project
from .logbook import Log

EXTERNAL_RE = re.compile(r"""(?:src|href)\s*=\s*["'](?!data:|#|blob:)[^"'>]+["']|@import|https?://""", re.I)
ABSOLUTE_RE = re.compile(r"""https?://""", re.I)


def _mmss(sec: float) -> str:
    """把秒数格式化成 m:ss（选段列表里用，比「段 37」更好定位）。"""
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}"


def build_bank(proj: Project, data: dict, log: Log) -> dict:
    """按规格 §5.3 组装 bank.json。"""
    segments = []
    clips = {}
    for seg in data["segments"]:
        # s3 存的 label 形如「第一乐章 · 段 3」，段号在几十段时没法定位；
        # 这里换成「第一乐章 · 3:30」，并补上 start（前端渲染时间轴/分组用）。
        mov = str(seg["label"]).partition(" · ")[0]
        t0 = float(seg.get("anchorStart", 0.0))
        segments.append({
            "id": seg["id"],
            "label": f"{mov} · {_mmss(t0)}" if mov else _mmss(t0),
            "movement": seg["movement"],
            "start": round(t0, 3),
            "duration": seg["duration"],
            "difficulty": seg["difficulty"],
            "clips": seg["clips"],
        })
    for cid, c in data["clips"].items():
        clips[cid] = {
            "performerId": c["performerId"],
            "segmentId": c["segmentId"],
            "bytes": c["bytes"],
        }
    bank = {
        "version": 1,
        "title": "同弦",
        "work": proj.title,
        "maxDuration": proj.seg_duration,
        "durationPresets": proj.presets,
        "packMode": proj.pack_mode,
        "builtAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "performers": [{"id": p.id, "display": p.display, "fullName": p.fullName}
                       for p in proj.performers],
        "segments": segments,
        "clips": clips,
    }
    log.kv(段落数=len(segments), 片段数=len(clips),
           每段时长=f"{proj.seg_duration:.0f}s", 可选时长=", ".join(f"{x}s" for x in proj.presets))
    return bank


def _render_html(bank: dict, audio_json: str, log: Log, check_external: bool) -> str:
    """把模板渲染成 HTML。

    Args:
        bank: bank 字典。
        audio_json: 内联音频 JSON；static 模式传 "{}"（音频外置）。
        log: 日志。
        check_external: True 时禁止任何外部引用（inline 模式的硬要求）。

    Returns:
        完整 HTML 文本。
    """
    bank_json = json.dumps(bank, ensure_ascii=False, separators=(",", ":"))
    css = (config.WEB / "style.css").read_text(encoding="utf-8")
    logic = (config.WEB / "logic.js").read_text(encoding="utf-8")
    app_js = (config.WEB / "app.js").read_text(encoding="utf-8")
    tpl = (config.WEB / "player.html").read_text(encoding="utf-8")

    for token in ("__CSS__", "__BANK__", "__AUDIO__", "__LOGIC__", "__APP__"):
        if token not in tpl:
            raise BuildError(f"模板缺少占位符 {token}")

    html = (tpl
            .replace("/*__CSS__*/", css)
            .replace("__BANK__", bank_json)
            .replace("__AUDIO__", audio_json)
            .replace("/*__LOGIC__*/", logic)
            .replace("/*__APP__*/", app_js))

    if check_external:
        hits = [m.group(0)[:80] for m in EXTERNAL_RE.finditer(html)]
        if hits:
            raise BuildError(f"产物里出现外部资源引用（规格要求零外部请求）：{hits[:5]}")
    else:
        # 静态站点允许同域相对路径，但仍不许指到别人家的 CDN
        hits = [m.group(0)[:80] for m in ABSOLUTE_RE.finditer(html)]
        if hits:
            raise BuildError(f"产物里出现绝对地址引用（静态站点不得依赖外部域名）：{hits[:5]}")
    return html


def _run_static(proj: Project, data: dict, bank: dict, log: Log) -> Path:
    """产出静态站点：index.html + audio/*.m4a。

    音频不入 HTML，由 Web 服务器按需发送（需支持 Range/206），
    因此不受 MAX_HTML_MB 约束，段数只受乐曲长度限制。

    Returns:
        index.html 路径。
    """
    site = config.DIR_DIST / config.STATIC_DIRNAME
    audio_dir = site / "audio"
    site.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    # 重跑时先清掉旧片段：否则上一次的 48 个会和这次的 432 个混在一起，
    # 人工清点时数目对不上（inline 模式的 s2_check 就踩过这个坑）
    stale = list(audio_dir.glob("*.m4a"))
    for f in stale:
        f.unlink()
    if stale:
        log.line(f"  清理旧片段 {len(stale)} 个")

    total = 0
    for cid in data["clips"]:
        src = config.DIR_CLIPS / f"{cid}.m4a"
        if not src.exists():
            raise BuildError(f"缺少片段文件 {src}")
        dst = audio_dir / f"{cid}.m4a"
        shutil.copyfile(src, dst)
        total += dst.stat().st_size

    html = _render_html(bank, "{}", log, check_external=False)
    out = site / "index.html"
    out.write_text(html, encoding="utf-8")
    # 索引另存一份：出题逻辑的自动验收（tests/player_sim.js）和排查都要读它
    (site / "bank.json").write_text(
        json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8")

    n = len(data["clips"])
    page_kb = out.stat().st_size / 1024
    avg_kb = total / max(1, n) / 1024
    log.metric("10 无外部域名", "0 处绝对地址引用", "0", True)
    log.table(["分项", "数值"], [
        ["index.html", f"{page_kb:.1f} KB（代码 + 索引，无音频）"],
        ["音频片段", f"{n} 个，{total / 1024 / 1024:.1f} MB，平均 {avg_kb:.0f} KB"],
        ["单次播放加载", f"约 {avg_kb:.0f} KB（只取当前这一段，支持 Range 拖放）"],
        ["全曲覆盖", f"{len(data['segments'])} 段 × {proj.n} 人"],
    ])
    ok = page_kb <= config.STATIC_HTML_MAX_KB
    log.metric("9 页面体积", f"{page_kb:.1f} KB", f"≤ {config.STATIC_HTML_MAX_KB} KB（音频外置）", ok)
    if not ok:
        raise BuildError(f"index.html {page_kb:.1f} KB 超过 {config.STATIC_HTML_MAX_KB} KB，"
                         f"说明有音频被误内联进页面了")
    log.line(f"  → {out}")
    log.line(f"  → {audio_dir}（{n} 个片段）")
    return out


def run(proj: Project, log: Log, force: bool = False) -> Path:
    """执行 S5，产出 dist/同弦.html 或 dist/site/index.html。

    Returns:
        产物路径。
    """
    log.stage("S5 打包")
    data = json.loads((config.WORK / "clips.json").read_text(encoding="utf-8"))
    bank = build_bank(proj, data, log)

    if proj.pack_mode == "static":
        return _run_static(proj, data, bank, log)

    # ---- 音频 base64 ---------------------------------------------
    log.line("  编码音频为 base64 …")
    audio_b64: dict[str, str] = {}
    raw_bytes = 0
    for cid in data["clips"]:
        path = config.DIR_CLIPS / f"{cid}.m4a"
        if not path.exists():
            raise BuildError(f"缺少片段文件 {path}")
        blob = path.read_bytes()
        raw_bytes += len(blob)
        audio_b64[cid] = base64.b64encode(blob).decode("ascii")

    audio_json = json.dumps(audio_b64, separators=(",", ":"))
    html = _render_html(bank, audio_json, log, check_external=True)
    log.metric("10 无外部请求", "0 处外部引用", "0", True)

    bank_json = json.dumps(bank, ensure_ascii=False, separators=(",", ":"))
    css = (config.WEB / "style.css").read_text(encoding="utf-8")
    logic = (config.WEB / "logic.js").read_text(encoding="utf-8")
    app_js = (config.WEB / "app.js").read_text(encoding="utf-8")

    out = config.DIR_DIST / "同弦.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    (config.DIR_DIST / "bank.json").write_text(
        json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8")
    size = out.stat().st_size
    mb = size / 1024 / 1024

    log.table(["分项", "字节", "占比"], [
        ["CSS", f"{len(css.encode()):,}", f"{len(css.encode()) / size * 100:.1f}%"],
        ["JS（logic+app）", f"{len((logic + app_js).encode()):,}",
         f"{len((logic + app_js).encode()) / size * 100:.1f}%"],
        ["bank JSON", f"{len(bank_json.encode()):,}", f"{len(bank_json.encode()) / size * 100:.1f}%"],
        ["音频 base64", f"{len(audio_json.encode()):,}", f"{len(audio_json.encode()) / size * 100:.1f}%"],
        ["音频原始", f"{raw_bytes:,}", f"{raw_bytes / size * 100:.1f}%"],
        ["合计", f"{size:,}", f"{mb:.2f} MB"],
    ])
    log.metric("9 体积", f"{mb:.2f} MB", f"≤ {config.MAX_HTML_MB} MB", mb <= config.MAX_HTML_MB)
    if mb > config.MAX_HTML_MB:
        raise BuildError(f"产物 {mb:.2f} MB 超过 {config.MAX_HTML_MB} MB，"
                         f"请减少段落数或截短片段（不得降码率）")
    log.line(f"  → {out}")
    return out
