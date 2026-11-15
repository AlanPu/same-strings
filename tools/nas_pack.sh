#!/usr/bin/env bash
# 打一个「NAS 上原生构建」用的传输包。
#
# 为什么不在本机构建镜像：这台 Mac 上没有任何容器运行时（docker / podman / colima 都没装），
# 而 tools/docker_pack.sh 依赖 buildx。与其为了打一个 50 MB 的镜像去装一套虚拟机，
# 不如把「运行镜像真正需要的四样东西」传上去，让 NAS 自己 build —— NAS 是 x86_64，
# 原生构建，连交叉都不需要。
#
# 产物：dist/nas/same-strings-nas.tar.gz（约 150 MB，大头是 dist/site/audio 的 m4a）
#
# 用法：
#   tools/nas_pack.sh
#   OUT=/tmp/x.tar.gz tools/nas_pack.sh
set -euo pipefail

cd "$(dirname "$0")/.."

OUT="${OUT:-dist/nas/same-strings-nas.tar.gz}"
NAME="same-strings"

die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# 构建上下文里必须有 dist/site，否则 Dockerfile 的 COPY 会失败。
# 在这里先拦住，比在 NAS 上看到 "COPY failed" 好排查。
[ -f dist/site/index.html ] || die "dist/site/index.html 不存在，先跑 .venv/bin/python build.py"
[ -f dist/site/bank.json ] || die "dist/site/bank.json 不存在，构建不完整"

n_audio=$(find dist/site/audio -name '*.m4a' 2>/dev/null | wc -l | tr -d ' ')
[ "$n_audio" -gt 0 ] || die "dist/site/audio 里没有 m4a，练不了任何一题"
say "dist/site：${n_audio} 个音频片段"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PKG="$TMP/$NAME"
mkdir -p "$PKG/dist"

# 只有这四样进包。build.py、ssm/、web/、tools/ 都不参与运行镜像 ——
# 传上去只会让 150 MB 变成几百 MB，还得等。
cp Dockerfile nginx.conf docker-compose.yml "$PKG/"
cp -R dist/site "$PKG/dist/site"

# 参照页 3 GB 不进包（走共享目录），但留一个空目录，省得用户在 NAS 上还要 mkdir，
# 也避免 compose 把「目录不存在」当成 bug 去查。
mkdir -p "$PKG/ref"

# ⚠️ 包内必须自带 .dockerignore。仓库那份写的是 `dist/ref`（本机布局），
# 而包里的参照页在顶层 `ref/` —— 不排除的话，等用户把 3 GB 视频拷进去，
# `docker compose build` 会先把 3 GB 全发给守护进程，表现是「卡在
# Sending build context 不动」，看起来像构建卡死，其实是上下文太大。
cat > "$PKG/.dockerignore" <<'IGNORE'
# 参照页走 volumes 挂载，不进构建上下文
ref/
# 环境变量只在宿主机读，不进镜像
.env
IGNORE

# 默认值写进 .env：解压目录自己就是项目根，ref 就在旁边，不用改任何绝对路径。
# NAS 上常见的 /volume1/docker/... 那种写法只是习惯，用相对路径同样能起。
cat > "$PKG/.env" <<'ENV'
# NAS 上暴露的端口（宿主机侧）。被占用就改这里。
PORT=8080
# 参照页目录（宿主机侧），相对 compose 文件所在目录。
# 8 个原视频 + 对比音轨约 3 GB，拷到 ./ref/ 下即可；不打算用就留空目录，/ref/ 返回 404。
REF_DIR=./ref
ENV

# 一键起容器。存在的理由是两个 DSM 特有的坑：
#   ① 非交互 shell 的 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，没有 /usr/local/bin，
#      所以直接 `docker` 会 command not found（而用户明明在 UI 里装了 Container Manager）；
#   ② docker.sock 是 root:root 660，administrators 组也得 sudo。
# 脚本自己把 PATH 补齐，用户只需要 `sudo ./start.sh`。
cat > "$PKG/start.sh" <<'SH'
#!/bin/sh
# DSM 上起「同弦」。用法：sudo ./start.sh
# 加 -d 前台看日志：sudo ./start.sh -f
PATH="/var/packages/ContainerManager/target/usr/bin:/usr/local/bin:$PATH"
export PATH
cd "$(dirname "$0")" || exit 1
case "$1" in
  -f) shift; exec docker compose up --build "$@" ;;    # 前台，Ctrl-C 停
  *)  exec docker compose up -d --build "$@" ;;
esac
SH
chmod +x "$PKG/start.sh"

mkdir -p "$(dirname "$OUT")"
# 两个 macOS 专属开关：
#   COPYFILE_DISABLE=1 —— 不加这个，bsdtar 会给每个带 xattr 的文件多塞一个 ._xxx
#     元数据文件，NAS 上解压出来是一堆垃圾，看着像打包出错。
#   --no-xattrs —— 不加这个，包里会带 LIBARCHIVE.xattr.com.apple.provenance
#     这类 pax 扩展头。GNU tar 不认识，解压时每个文件刷一行
#     "Ignoring unknown extended header keyword"，几十行红字，用户以为失败了。
#     ⚠️ 别用 --no-mac-metadata：它只挡 ._ 文件和自定义 Apple 头，
#     实测挡不住 com.apple.provenance 这类普通 xattr（同一份文件仍能数出 1 处）。
#     老系统没有 --no-xattrs 就退回原样，只是有噪音，不影响正确性。
TAR_OPTS="-czf"
if tar --no-xattrs -cf /dev/null --files-from /dev/null 2>/dev/null; then
    TAR_OPTS="--no-xattrs -czf"
fi
COPYFILE_DISABLE=1 tar $TAR_OPTS "$OUT" -C "$TMP" "$NAME"

# 自检：包里不能有 AppleDouble（._*）或 .DS_Store，且入口文件齐全
BAD=$(COPYFILE_DISABLE=1 tar -tzf "$OUT" | grep -cE '(^|/)\._|\.DS_Store$' || true)
[ "$BAD" -eq 0 ] || die "包里有 $BAD 个 macOS 元数据文件（._* 或 .DS_Store）"

for f in Dockerfile nginx.conf docker-compose.yml .env start.sh dist/site/index.html dist/site/bank.json; do
    COPYFILE_DISABLE=1 tar -tzf "$OUT" | grep -qx "$NAME/$f" || die "包里缺 $f"
done

SIZE=$(du -h "$OUT" | cut -f1 | tr -d ' ')
SHA=$(shasum -a 256 "$OUT" | cut -d' ' -f1)

say ""
say "✓ ${OUT}（${SIZE}）"
say "  sha256 ${SHA}"
say ""
say "── 拷到 NAS（任选一种）─────────────────────────────"
say "  共享目录挂载：  cp \"$OUT\" /Volumes/<共享名>/docker/"
say "  SSH + rsync：  rsync -av \"$OUT\" <用户>@<NAS地址>:/volume1/docker/"
say ""
say "── 在 NAS 上执行 ──────────────────────────────────"
say "  cd /volume1/docker && tar xzf $(basename "$OUT")"
say "  cd $NAME && sudo ./start.sh          # 等价于 sudo docker compose up -d --build"
say ""
say "  然后 http://<NAS地址>:8080/        练习器"
say "       http://<NAS地址>:8080/ref/    参照页（把 3 GB 拷进 ./ref/ 之后才有）"
