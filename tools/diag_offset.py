"""诊断：锚点开头 30 秒在各演奏家音轨里最匹配的位置。

用子序列 DTW（这里只做诊断，不参与正式对齐）快速回答两个问题：
  1) 每条音轨是不是从协奏曲开头开始（有没有前奏曲/致辞/掌声）；
  2) 时长差是分布在开头、结尾还是整体速度。
"""
import json
import sys
from pathlib import Path

import librosa
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ssm import config  # noqa: E402

PROBE_SEC = 40.0


def feat(y):
    c = librosa.feature.chroma_cens(y=y, sr=22050, hop_length=1024)
    n = np.linalg.norm(c, axis=0, keepdims=True)
    return (c / np.maximum(n, 1e-9)).astype(np.float32)


proj = json.loads((config.ROOT / "project.json").read_text())
anchor = proj["anchor"]
ids = [p["id"] for p in proj["performers"]]

ya, _ = librosa.load(str(config.DIR_PCM / f"{anchor}.wav"), sr=22050, mono=True,
                     offset=0, duration=PROBE_SEC)
fa = feat(ya)

for pid in ids:
    y, _ = librosa.load(str(config.DIR_PCM / f"{pid}.wav"), sr=22050, mono=True)
    fp = feat(y)
    D, wp = librosa.sequence.dtw(X=fa, Y=fp, subseq=True, backtrack=True, metric="cosine")
    end = wp[-1]
    start_frame = end[1] - (fa.shape[1] - 1)
    t_start = start_frame * config.HOP / 22050
    # 对齐质量：路径上的平均余弦
    wp_r = wp[::-1]
    sim = np.mean(np.sum(fa[:, wp_r[:, 0]] * fp[:, wp_r[:, 1]], axis=0))
    print(f"{pid:<9} 时长 {len(y)/22050:7.1f}s  锚点开头落在 {t_start:7.1f}s  "
          f"余弦 {sim:.3f}")
    del y, fp
