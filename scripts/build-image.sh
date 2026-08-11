#!/usr/bin/env bash
# =============================================================================
# movieclaw Docker 镜像构建脚本
#
# 用法：
#   ./scripts/build-image.sh                  # 构建 movieclaw:latest（本机架构）
#   TAG=v0.1.0 ./scripts/build-image.sh       # 指定标签
#   PLATFORM=linux/amd64 ./scripts/build-image.sh   # 交叉构建 NAS 常见的 x86_64
#   CN_MIRROR=1 ./scripts/build-image.sh      # 国内镜像源加速（pip 清华源 + npmmirror）
#
# TMDB Key 读取顺序：环境变量 TMDB_API_KEY > 仓库根目录 .env 中的 TMDB_API_KEY。
# Key 只通过 --build-arg 传入，不进入构建上下文（.env 已被 .dockerignore 排除）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TAG="${TAG:-latest}"
IMAGE="movieclaw:${TAG}"

# 读取 TMDB Key
TMDB_KEY="${TMDB_API_KEY:-}"
if [[ -z "$TMDB_KEY" && -f .env ]]; then
    TMDB_KEY="$(grep -E '^TMDB_API_KEY=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]"'"'" || true)"
fi
if [[ -z "$TMDB_KEY" ]]; then
    echo "错误：未找到 TMDB_API_KEY（设置环境变量，或写入仓库根目录 .env）" >&2
    exit 1
fi

# --shm-size：Docker 默认只给 /dev/shm 64MB，Next.js 构建的并行 worker
# 靠共享内存通信，页面数量上来后会静默死锁（日志停在 "Creating an optimized
# production build"、CPU 掉到空闲）。给足 2G 是这一现象的根治手段。
BUILD_ARGS=(--build-arg "TMDB_API_KEY=$TMDB_KEY" --shm-size=2g)

# 国内网络加速
if [[ "${CN_MIRROR:-0}" == "1" ]]; then
    BUILD_ARGS+=(
        --build-arg "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
        --build-arg "NPM_REGISTRY=https://registry.npmmirror.com"
        # apt 也要换源：ffmpeg 的依赖链很长，直连 deb.debian.org 经代理时
        # 常在半路 400/超时，整个构建就废在最后一层
        --build-arg "APT_MIRROR=mirrors.tuna.tsinghua.edu.cn"
    )
fi

# 交叉构建（如在 Apple Silicon 上给 x86_64 NAS 出镜像）
if [[ -n "${PLATFORM:-}" ]]; then
    case "$PLATFORM" in
        linux/amd64|linux/arm64) ;;
        *)
            echo "错误：PLATFORM 只支持 linux/amd64 或 linux/arm64，当前为 $PLATFORM" >&2
            exit 1
            ;;
    esac

    # 目标架构不是 Docker 守护进程原生架构时，必须先有 binfmt/QEMU。
    # 提前检测可以避免跑完整个前端构建后，才在 seconv 自检层报 exec format error。
    docker_arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || true)"
    case "$docker_arch" in
        x86_64) docker_arch="amd64" ;;
        aarch64) docker_arch="arm64" ;;
    esac
    target_arch="${PLATFORM#linux/}"
    if [[ "$docker_arch" != "$target_arch" ]]; then
        builder_platforms="$(docker buildx inspect --bootstrap 2>/dev/null \
            | sed -n 's/^Platforms:[[:space:]]*//p' \
            | tr ',' '\n' \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        if ! grep -Fxq "$PLATFORM" <<<"$builder_platforms"; then
            echo "错误：当前 Docker builder 不能执行 $PLATFORM。" >&2
            echo "请先安装/启用 binfmt(QEMU)，或在对应架构设备上原生构建。" >&2
            exit 1
        fi
    fi
    BUILD_ARGS+=(--platform "$PLATFORM")
fi

# 离线构建：只用本地已有的基础镜像，不去 registry 查元数据。
# 适用于 Docker 守护进程出网不畅的环境（如 fake-ip 代理模式下，dockerd 自身
# 的请求不经宿主代理，会卡在解析出的 198.18.x 地址上）。
# 前提：node/python/debian 三个基础镜像本地已存在。
if [[ "${OFFLINE_BASE:-0}" == "1" ]]; then
    BUILD_ARGS+=(--pull=false)
fi

# 交叉构建时分两步：先单独把前端阶段跑完，再跑完整构建。
#
# 为什么：交叉构建下 BuildKit 会并行执行「前端阶段（跑在构建机原生架构）」与
# 「后端/运行镜像阶段（跑在 QEMU 模拟的目标架构）」。两者并行时 next build 会
# 间歇性挂死——CPU 掉到 0、构建缓存零增长，能挂十几个小时；而单独构建前端阶段
# 从未复现过。拆成两步后前端独占执行，第二步直接命中它的缓存。
if [[ -n "${PLATFORM:-}" ]]; then
    echo "预构建前端阶段（与 QEMU 模拟阶段错开，规避并行挂死）……"
    docker build "${BUILD_ARGS[@]}" --target web-builder -t "${IMAGE}-webstage" .
fi

echo "构建 $IMAGE ……"
docker build "${BUILD_ARGS[@]}" -t "$IMAGE" .
echo "验证 $IMAGE 的字幕运行时 ……"
docker run --rm --entrypoint /usr/local/bin/movieclaw-subtitle-smoke-test "$IMAGE"
echo "完成：${IMAGE}（字幕运行时已通过 PGS → SRT 端到端自检）"
