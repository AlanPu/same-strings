"""S5 — 打包成单个自包含 HTML。

音频以 base64 放进 <script type="application/json" id="audio">，运行时 atob → Blob → objectURL。
不用 data URI 直连（iOS Safari 对长 data URI 不可靠）。所有 CSS/JS 内联，零外部资源。
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path

from . import config
from .config import BuildError, Project
from .logbook import Log

EXTERNAL_RE = re.compile(r"""(?:src|href)\s*=\s*["'](?!data:|#|blob:)[^"'>]+["']|@import|https?://""", re.I)


def build_bank(proj: Project, data: dict, log: Log) -> dict:
    """按规格 §5.3 组装 bank.json。"""
    segments = []
    clips = {}
    for seg in data["segments"]:
        segments.append({
            "id": seg["id"],
            "label": seg["label"],
            "movement": seg["movement"],
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
        "builtAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "performers": [{"id": p.id, "display": p.display, "fullName": p.fullName}
                       for p in proj.performers],
        "segments": segments,
        "clips": clips,
    }
    log.kv(段落数=len(segments), 片段数=len(clips),
           每段时长=f"{proj.seg_duration:.0f}s", 可选时长=", ".join(f"{x}s" for x in proj.presets))
    return bank


def run(proj: Project, log: Log, force: bool = False) -> Path:
    """执行 S5，产出 dist/同弦.html。

    Returns:
        产物路径。
    """
    log.stage("S5 打包")
    data = json.loads((config.WORK / "clips.json").read_text(encoding="utf-8"))
    bank = build_bank(proj, data, log)

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

    bank_json = json.dumps(bank, ensure_ascii=False, separators=(",", ":"))
    audio_json = json.dumps(audio_b64, separators=(",", ":"))
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

    # ---- 外部资源扫描 -------------------------------------------
    hits = [m.group(0)[:80] for m in EXTERNAL_RE.finditer(html)]
    if hits:
        raise BuildError(f"产物里出现外部资源引用（规格要求零外部请求）：{hits[:5]}")
    log.metric("10 无外部请求", "0 处外部引用", "0", True)

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
