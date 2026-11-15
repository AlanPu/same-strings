"""诊断 2：定位锚点的「开头 40 秒」和「结尾 40 秒」各自落在每条音轨的什么位置。

(start, end) 的跨度与锚点跨度的比值就是两版的速度比；
若跨度接近锚点长度但 end 远小于音轨长度，说明多余时长堆在尾部（掌声/返场）。
"""
import json
import sys
from pathlib import Path

import librosa
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ssm import config  # noqa: E402

PROBE = 40.0
HOP = config.HOP
SR = 22050


def feat(y):
    c = librosa.feature.chroma_cens(y=y, sr=SR, hop_length=HOP)
    n = np.linalg.norm(c, axis=0, keepdims=True)
    return (c / np.maximum(n, 1e-9)).astype(np.float32)


def locate(probe: np.ndarray, target: np.ndarray) -> tuple[float, float]:
    """返回 (起点秒, 路径平均余弦)。"""
    _, wp = librosa.sequence.dtw(X=probe, Y=target, subseq=True, backtrack=True, metric="cosine")
    start = float(np.min(wp[:, 1])) * HOP / SR
    sim = float(np.mean(np.sum(probe[:, wp[:, 0]] * target[:, wp[:, 1]], axis=0)))
    return start, sim


proj = json.loads((config.ROOT / "project.json").read_text())
anchor = proj["anchor"]
ids = [p["id"] for p in proj["performers"]]
apath = config.DIR_PCM / f"{anchor}.wav"
adur = librosa.get_duration(path=str(apath))

head, _ = librosa.load(str(apath), sr=SR, mono=True, offset=0, duration=PROBE)
tail, _ = librosa.load(str(apath), sr=SR, mono=True, offset=adur - PROBE, duration=PROBE)
fh, ft = feat(head), feat(tail)

print(f"锚点 {anchor} 时长 {adur:.1f}s")
for pid in ids:
    p = config.DIR_PCM / f"{pid}.wav"
    dur = librosa.get_duration(path=str(p))
    y, _ = librosa.load(str(p), sr=SR, mono=True)
    f = feat(y)
    s_head, c1 = locate(fh, f)
    s_tail, c2 = locate(ft, f)
    span = (s_tail + PROBE) - s_head
    print(f"{pid:<9} 时长 {dur:7.1f}s | 开头在 {s_head:7.1f}s (cos {c1:.3f}) | "
          f"结尾在 {s_tail:7.1f}s (cos {c2:.3f}) | 跨度 {span:7.1f}s "
          f"= 锚点 {span / adur * 100:5.1f}% | 尾部多余 {dur - (s_tail + PROBE):7.1f}s")
    del y, f
