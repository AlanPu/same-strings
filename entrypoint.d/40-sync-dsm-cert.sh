#!/bin/sh
# 同弦 · 启动时同步 DSM 的证书
#
# 官方 nginx 镜像在 `exec nginx` 之前，会按 sort -V 顺序执行
# /docker-entrypoint.d/*.sh —— 但有两个硬条件：
#   ① 只在 $1 是 nginx / nginx-debug 时跑（compose 没覆盖 command，满足）
#   ② 脚本必须有可执行位，否则 entrypoint 直接打 "Ignoring ... not executable"
#      所以 Dockerfile 里有专门的 RUN chmod +x，不能省。
# 命名 40- 是为了排在镜像自带的 10/15/20/30- 之后。
#
# 为什么需要这一步：nginx 只在启动时把 ssl_certificate 读进内存。
# 证书放在宿主机（DSM 的证书库）通过只读挂载推进来，容器不主动拷一遍，
# nginx 就找不到 /etc/nginx/certs/fullchain.pem。
#
# 这个脚本也被宿主机上的 tools/renew_cert_check.sh 复用：续签后直接
# `docker exec ... /docker-entrypoint.d/40-sync-dsm-cert.sh` 再 reload，
# 不用重建容器、不用中断服务。

set -eu

SRC="${DSM_CERT_SRC:-/dsm-certs}"
DST=/etc/nginx/certs

mkdir -p "$DST"

if [ ! -r "$SRC/fullchain.pem" ] || [ ! -r "$SRC/privkey.pem" ]; then
    # 允许「已经在镜像/外部挂载里放好证书」的场景（例如本机用自签证书调试），
    # 但不静默放过：没证书还继续启动，只会在浏览器里变成一个语焉不详的 TLS 错误。
    if [ -r "$DST/fullchain.pem" ] && [ -r "$DST/privkey.pem" ]; then
        echo "[cert] 没有 DSM 证书挂载，沿用 /etc/nginx/certs 里已有的文件" >&2
        exit 0
    fi
    echo "[cert] 证书不可用：$SRC/fullchain.pem 和 privkey.pem 必须都可读" >&2
    echo "[cert] 检查 compose 的 DSM_CERT_DIR 挂载（默认 /usr/syno/etc/certificate/system/default）" >&2
    echo "[cert] DSM 的证书文件是 root:r--------，容器主进程必须以 root 启动（官方镜像默认如此）" >&2
    exit 1
fi

cp "$SRC/fullchain.pem" "$DST/fullchain.pem"
cp "$SRC/privkey.pem"   "$DST/privkey.pem"
chmod 600 "$DST"/*.pem

# 打一行指纹，方便 `docker logs same-strings` 里确认「这次启动同步的是哪一张」
fp=$(sha256sum "$DST/fullchain.pem" | cut -d' ' -f1 | cut -c1-12)
echo "[cert] 已从 DSM 证书库同步 fullchain.pem（sha256 ${fp}…）"
