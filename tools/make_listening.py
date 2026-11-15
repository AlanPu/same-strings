"""生成「逐段横向试听包」：每个段落把 8 位演奏家的片段按顺序拼成一条，中间留 1 秒静音。

用途：规格 §10 第 6 项「抽 5 段人工试听，不得出现纯乐团 tutti / 静音 / 半句截断」。
一条文件就能横着听完一段的 8 个版本，比逐个点开快得多。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssm import audio, config  # noqa: E402
from ssm.config import load_project  # noqa: E402


def main() -> None:
    proj = load_project()
    data = json.loads((config.WORK / "clips.json").read_text(encoding="utf-8"))
    out_dir = config.DIR_CHECK
    # 段数会随体积预算变化，先清掉上一轮的试听包，免得残留旧段误导人工核对
    for stale in out_dir.glob("segment-*.m4a"):
        stale.unlink()
    silence = out_dir / "_silence.m4a"
    audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
              "-i", "anullsrc=r=44100:cl=stereo", "-t", "1.0", "-c:a", "aac",
              "-b:a", "96k", str(silence)], "生成静音")

    made = []
    for seg in data["segments"]:
        order = [p.id for p in proj.performers]
        parts = []
        for pid in order:
            cid = seg["clips"][pid]
            parts.append(config.DIR_CLIPS / f"{cid}.m4a")
            parts.append(silence)
        listfile = out_dir / f"_{seg['id']}.txt"
        listfile.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
                            encoding="utf-8")
        dst = out_dir / f"segment-{seg['id']}.m4a"
        audio.sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                  "-safe", "0", "-i", str(listfile), "-c", "copy", str(dst)], "拼接试听包")
        listfile.unlink()
        made.append(dst)
        print(f"  {dst.name}  顺序：" + " → ".join(proj.by_id(p).display for p in order))

    silence.unlink(missing_ok=True)
    print(f"共 {len(made)} 条 → {out_dir}")


if __name__ == "__main__":
    main()
