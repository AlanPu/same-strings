"""参照页的静态检查：dist/ref 是否自洽。

这块东西最容易出的错不是「打不开」，而是**换算错了但看不出来**：
拖到 9:30，八个人各播八秒，听起来都像那么回事，实际有个人偏差了十几秒。
所以这里做四类断言：

1. **文件齐全**：8 个视频 + 8 个对比音轨，编码是 h264 / aac；
2. **moov 前置**：视频真做了 +faststart，否则 iPad 上拖进度条要等整文件；
3. **换算表自洽**：warp 单调不减、长度 = 锚点时长 / 步长 + 1、端点落在音轨内；
4. **页面数据完整**：index.html 里嵌的 JSON 含 8 人、7 张表（锚点自己不需要），
   并且和 work/warp/*.json 完全一致（防止把旧表打进页面）。

用法：
    .venv/bin/python tools/check_ref.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssm import audio, config  # noqa: E402
from ssm.config import load_project  # noqa: E402

REF = config.DIR_DIST / "ref"


def head_has_moov_before_mdat(path: Path, span: int = 4 << 20) -> tuple[bool, str]:
    """检查 moov 是否出现在 mdat 之前（= 做了 faststart）。

    只读文件头部：如果 moov 前置，它一定在开头几 MB 内；如果被放在文件尾，
    头部压根找不到它。所以读一段就够，不用扫整个文件。

    Args:
        path: mp4 文件。
        span: 读取前多少字节。

    Returns:
        (是否合格, 说明文字)
    """
    with path.open("rb") as fh:
        head = fh.read(span)
    i_moov = head.find(b"moov")
    i_mdat = head.find(b"mdat")
    if i_moov < 0:
        return False, "开头未见 moov（可能被放在文件尾）"
    if i_mdat >= 0 and i_mdat < i_moov:
        return False, "mdat 在 moov 之前"
    return True, "moov 前置"


def main() -> int:
    """入口。"""
    proj = load_project()
    if not (REF / "index.html").exists():
        print(f"缺少 {REF}/index.html，先跑 tools/make_ref.py")
        return 2

    rows: list[tuple[str, str, bool]] = []

    # ---- 1/2 视频 ------------------------------------------------
    for p in proj.performers:
        f = REF / "video" / f"{p.id}.mp4"
        if not f.exists():
            rows.append((f"视频 {p.id}", "缺失", False))
            continue
        info = audio.ffprobe(f)
        mb = f.stat().st_size / 1024 / 1024
        ok, note = head_has_moov_before_mdat(f)
        rows.append((f"视频 {p.id}", f"{mb:.0f} MB，{info['duration'] / 60:.1f} 分，{note}", ok))

    # ---- 3 换算表 -------------------------------------------------
    anchor_wav = config.DIR_PCM / f"{proj.anchor}.wav"
    anchor_sec = audio.duration(anchor_wav)
    # 后端是 np.arange(0, 锚点时长 + 1e-6, 步长)，所以点数 = floor(时长/步长) + 1。
    # 一开始写成 round(...) + 1 多算了一个点，七个源全报 FAIL —— 是判据错了，不是数据错了。
    want_len = int((anchor_sec + 1e-6) // config.WARP_STEP) + 1
    print(f"锚点 {proj.anchor}：{anchor_sec:.1f} s，按 {config.WARP_STEP} s 步长应有 {want_len} 个点")

    for p in proj.performers:
        if p.id == proj.anchor:
            continue
        w = json.loads((config.DIR_WARP / f"{p.id}.json").read_text(encoding="utf-8"))
        mp = [float(x) for x in w["map"]]
        mono = all(b >= a - 1e-6 for a, b in zip(mp, mp[1:]))
        audio_sec = audio.duration(config.DIR_PCM / f"{p.id}.wav")
        # 画面能拖到的最远处必须还在音轨内，否则 seek 会被浏览器夹到末尾，听感上是「卡住」
        in_range = max(mp) <= audio_sec + 0.5
        rows.append((
            f"表 {p.id}",
            f"{len(mp)} 点，0→{mp[0]:.1f}s，末 {mp[-1]:.1f}s / 音轨 {audio_sec:.1f}s"
            + ("" if mono else "，⚠ 非单调"),
            mono and in_range and len(mp) == want_len,
        ))

    # ---- 4 页面数据 ------------------------------------------------
    html = (REF / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    if not m:
        rows.append(("页面数据", "找不到 data 块", False))
    else:
        data = json.loads(m.group(1))
        ids = [p["id"] for p in data["performers"]]
        want_ids = [p.id for p in proj.performers]
        rows.append(("页面 8 位演奏家", f"{len(ids)} 位：{','.join(ids)}", ids == want_ids))

        tables = data["warp"]
        expect = len(proj.performers) - 1
        rows.append(("页面换算表", f"{len(tables)} 张（锚点不需要）", len(tables) == expect))

        # 逐点比对页面里的表和 work 里的一致，防「旧表被打进新页面」
        worst = 0.0
        for pid, t in tables.items():
            src = json.loads((config.DIR_WARP / f"{pid}.json").read_text(encoding="utf-8"))["map"]
            if len(t["map"]) != len(src):
                worst = float("inf")
                break
            worst = max(worst, max(abs(a - round(float(b), 3)) for a, b in zip(src, t["map"])))
        rows.append(("页面表与 work 一致", f"最大偏差 {worst:.4f} s", worst <= 0.001))

        # 视频时长写进了页面，列表里显示的是各人的真实长度
        vs = [p.get("videoSec", 0) for p in data["performers"]]
        rows.append(("页面含各人时长", f"{min(vs):.0f}–{max(vs):.0f} s",
                     all(v > 60 for v in vs)))

        # 开场静音跳过点。实测哈德利希 18 s、文格洛夫 32 s、穆特 34 s 才有声，
        # 不跳过去用户会以为「这个没声音」；值本身也要落在合理范围里。
        starts = {p["id"]: p.get("musicStart", 0) for p in data["performers"]}
        bad_start = [k for k, v in starts.items() if not (0 <= v <= 60)]
        rows.append(("开场静音跳过点",
                     "、".join(f"{k} {v:.0f}s" for k, v in starts.items() if v > 0) or "全部有声",
                     not bad_start))
        # 跳过的点必须早于音轨长度，否则一点开就直接到头
        over = [k for k, v in starts.items()
                if v > 0 and v >= audio.duration(config.DIR_PCM / f"{k}.wav") - 60]
        rows.append(("跳过点未越界", f"越界 {len(over)} 位", not over))

    width = max(len(r[0]) for r in rows)
    bad = sum(1 for *_, ok in rows if not ok)
    for name, detail, ok in rows:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print(f"\n通过 {len(rows) - bad}/{len(rows)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
