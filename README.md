# 同弦 / Same Strings

盲听辨人的小提琴听力训练器。8 位演奏家演奏同一首门德尔松 e 小调小提琴协奏曲，
每题播放一段（默认 30 秒）的某个版本，四选一判断是谁在拉。

同一段乐句在不同人手里，揉弦的密度、换弓的棱角、句尾的收法都不一样。这个工具让你
在「不知道是谁」的前提下反复听同一段，靠耳朵建立区分度，而不是靠记忆叙事。

## 两种打包形态

同一套源码、同一套管线，产物有两种形态。差异只在**音频怎么送到耳朵**。

| | `inline` | `static` |
|---|---|---|
| 产物 | 单个 `同弦.html`（约 23 MB） | `dist/site/`（index.html 89 KB + `audio/*.m4a` 148 MB） |
| 音频 | base64 内联进 HTML | 外置，按需 HTTP 取用（单次约 358 KB） |
| 分段数 | 受 25 MB 体积预算约束，8 位演奏家只能放 6 段 | 不受体积约束，可覆盖全曲（53 段 × 8 人 = 424 片段） |
| 分发 | AirDrop 一个文件到 iPad，双击即用 | 需要 Web 服务器（见下方 Docker） |
| 离线 | 可以，飞行模式照样答题 | 不行，断网就是用不了 |
| 首屏 | 解析 23 MB base64（约 46 MB 内存峰值） | 取一个几百 KB 的片段 |

改 `project.json` 的 `pack.mode` 切换。默认 `static`。

**为什么内联模式要有体积上限**：`file://` 协议下 Safari 不允许 fetch 同目录文件，
所以离线单文件只能把音频 base64 塞进 HTML，而 base64 膨胀 4/3。
25 MB 这条线的实质是「iPad 上页面不会被 iOS 因内存压力丢弃」，
不是磁盘配额。详见 `SPEC.md` S3.5。

## 快速开始

先装两个可执行文件：`ffmpeg`（切片/归一化）和 `yt-dlp`（抓音轨）。

```bash
brew install ffmpeg yt-dlp                 # macOS
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python build.py                  # 全流程 S0→S5，首次约 40 分钟（含下载）
.venv/bin/python build.py --verify-only    # 只跑验收
```

单阶段重跑：

```bash
.venv/bin/python build.py --only s3         # 只重算选段
.venv/bin/python build.py --from s3 --to s5 # 选段 → 切片 → 打包
.venv/bin/python build.py --force s4        # 忽略缓存强制重跑
```

每阶段用输入签名判断缓存，`project.json` 里改段数/时长会自动让下游失效。

## Docker 部署（NAS）

构建仍在本机跑（要 2.2 GB PCM、几十分钟 CPU 和网络），镜像只负责托管产物：

```bash
.venv/bin/python build.py                 # 先产出 dist/site/
docker compose up -d --build              # 再在 NAS 上起容器
```

默认映射 `8080:80`，打开 `http://<NAS 地址>:8080/`。

`nginx.conf` 里有三件事是必须的，改的时候别动：

1. **`.m4a` 必须是 `audio/mp4`**。nginx 自带的 mime.types 不一定收录 m4a，
   漏了就退回 `application/octet-stream`，iOS Safari 直接拒绝播放。
2. **支持 Range / 206**。Safari 打开音频会先发 Range 探测再续取，
   只回 200 全量会表现为「点了播放没反应」。
3. **不要 gzip m4a**。AAC 已经压过，二次压缩会破坏 Range 分段。

内网 http 通常可以直接播（Safari 不受 ATS 约束）。若被企业策略拦，再配 HTTPS。

## 管线

| 阶段 | 做什么 | 关键点 |
|---|---|---|
| S0 | 抓取 B 站音轨，分 P 拼接 | 拼接缝对齐误差 < 1 s |
| S1 | 响度归一化 + 头部对齐 | `loudnorm` 两遍法；裁掉各版本长度不一的掌声/静场 |
| S2 | 对齐到锚点时间轴 | 全局单调 DTW（chroma_cens），头部 60 s 用子序列匹配局部校正 |
| S2.5 | 生成左右声道对照音频 | 左耳锚点、右耳目标，人工核验对齐 |
| S3 | 选段 | `sequential` 从乐曲开头连续切，确定性、无随机 |
| S4 | 切片与编码 | 先切再压；段内做响度对齐，否则响度差会成为答题捷径 |
| S5 | 打包 | 按 `pack.mode` 输出内联 HTML 或静态站点 |

**不用子序列 DTW 做正式对齐**：再现部会匹配到错误位置，必须是全局单调。
**不降码率省体积**：64 kbps 会削掉高频泛音，而泛音正是分辨演奏家的关键信息。

## 验收

自动 16 项 + 人工 4 项，构建期打印，不达标非零退出。

自动项覆盖：抓取完整性、响度一致、编码一致、等长、对齐质量、warp 单调、
页面体积、无第三方请求、片段总数、选项合法性、答案均衡、演奏家覆盖、
顺序切分、静态资源完整、服务端存储。

协议层另有 `tools/smoke_site.py`：起一个支持 Range 的本地服务器，
验证 200 / 206 / `Content-Range` / `Accept-Ranges` / ftyp 头都对。

人工项（自动验收覆盖不到）：
- **#5 对齐正确性**：`work/check/*.m4a` 左右声道对照，听 3 处
- **#6 选段质量**：`work/check/segment-s0*.m4a` 逐段试听（`python tools/make_listening.py 6`）
- **#8 联网可达**：真 iPad 上连续答 10 题
- **#11 存储降级**：关掉 Safari 网站数据后仍能答题

## 目录

```
SPEC.md          需求规格（唯一来源，先改它再改代码）
project.json     唯一人工编辑的配置：演奏家、切片参数、打包形态
build.py         构建入口，负责签名缓存与验收
ssm/             S0–S5 各阶段 + verify
web/             前端（logic.js 是纯函数，可被 node 直接测试）
tests/           出题逻辑的 1000 轮模拟
tools/           诊断与冒烟测试脚本
work/            中间产物（不入库：pcm 2.2 GB、feat 90 MB、src 原始音轨）
dist/            产物（不入库）
```

## 已知取舍

- **切到点就停，不做淡出**。iOS 上 `HTMLMediaElement.volume` 是只读的，做不了。
- **考试模式不出提示音**。对/错是两种音高，等于把答案提前告诉耳朵。
- **static 形态没有离线**。这是换取「覆盖全曲」付出的代价，不是疏漏。
- **顺序模式一次 53 题**。全曲切了 53 段，一轮走完确实长；中途可以「退出」，
  也可以到「自己选段」里只练某几段。
- **音频不入库**。片段来自 B 站，版权属原演奏者与唱片公司；仓库只保留
  `project.json` 里的 bvid 引用，构建时现抓。
