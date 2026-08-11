#!/bin/sh
# Docker 字幕运行时的端到端自检。
#
# 不能只跑 ``seconv --version``：PGS 解析会延迟加载 SkiaSharp/HarfBuzz，
# Tesseract 也是到真正 OCR 时才作为子进程启动。这里先用一段合成 SRT 生成
# 无版权的 PGS，封装进 MKV 后再通过 FFmpeg 抽出，最后 OCR 回 SRT，同时
# 覆盖字体、动态库、语言包、影片内封字幕抽取和进程调用链。
set -eu

SECONV="${MOVIECLAW_SECONV_PATH:-/opt/movieclaw/seconv/seconv}"
EXPECTED_LANGUAGES="eng chi_sim chi_tra jpn kor fra deu spa ita por rus"

fail() {
    echo "字幕运行时自检失败：$*" >&2
    exit 1
}

[ -x "$SECONV" ] || fail "未找到可执行的 seconv：$SECONV"
for command_name in ffmpeg ffprobe tesseract fc-match ldd; do
    command -v "$command_name" >/dev/null 2>&1 \
        || fail "镜像缺少命令：$command_name"
done

case "$(uname -m)" in
    x86_64) runtime_arch="amd64" ;;
    aarch64|arm64) runtime_arch="arm64" ;;
    *) fail "不支持的容器架构：$(uname -m)（仅支持 amd64/arm64）" ;;
esac

"$SECONV" --version >/dev/null 2>&1 || fail "seconv 无法启动"
ffmpeg -version >/dev/null 2>&1 || fail "ffmpeg 无法启动"
ffprobe -version >/dev/null 2>&1 || fail "ffprobe 无法启动"
fc-match "DejaVu Sans" >/dev/null 2>&1 || fail "DejaVu Sans 字体不可用"

# seconv 的本机库不会都在进程启动时加载，显式检查可把缺库问题提前到构建期。
for native_library in "$(dirname "$SECONV")"/*.so; do
    [ -f "$native_library" ] || continue
    if ! ldd_output="$(ldd "$native_library" 2>&1)"; then
        printf '%s\n' "$ldd_output" >&2
        fail "无法检查动态库：$native_library"
    fi
    if printf '%s\n' "$ldd_output" | grep -F "not found" >&2; then
        fail "动态库依赖不完整：$native_library"
    fi
done

installed_languages="$(tesseract --list-langs 2>&1)" \
    || fail "Tesseract 无法读取语言包"
for language in $EXPECTED_LANGUAGES; do
    printf '%s\n' "$installed_languages" | grep -Fxq "$language" \
        || fail "Tesseract 缺少语言包：$language"
done

probe_dir="$(mktemp -d /tmp/movieclaw-subtitle-smoke.XXXXXX)" \
    || fail "无法创建临时目录"
trap 'rm -rf "$probe_dir"' EXIT INT TERM

printf '%s\n' \
    '1' \
    '00:00:01,000 --> 00:00:04,000' \
    'MOVIECLAW OCR TEST 2026' \
    > "$probe_dir/source.srt"
mkdir -p "$probe_dir/pgs" "$probe_dir/ocr"

if ! "$SECONV" "$probe_dir/source.srt" bluraysup \
    --resolution:1920x1080 \
    "--font-name:DejaVu Sans" \
    --font-size:64 \
    "--output-folder:$probe_dir/pgs" \
    --overwrite > "$probe_dir/render.log" 2>&1; then
    sed -n '1,120p' "$probe_dir/render.log" >&2
    fail "seconv 无法生成测试 PGS"
fi
[ -s "$probe_dir/pgs/source.sup" ] || fail "seconv 未生成测试 PGS"

# 不能只把刚生成的 SUP 直接送回 seconv：真实任务会先从 MKV 等影片容器抽轨。
# 这里封装再抽出，并让 ffprobe 确认轨道类型，覆盖 MovieClaw 的完整底层链路。
if ! ffmpeg -loglevel error \
    -i "$probe_dir/pgs/source.sup" \
    -map 0:s:0 -c:s copy \
    "$probe_dir/embedded.mkv" > "$probe_dir/mux.log" 2>&1; then
    sed -n '1,120p' "$probe_dir/mux.log" >&2
    fail "FFmpeg 无法把测试 PGS 封装进 MKV"
fi
[ -s "$probe_dir/embedded.mkv" ] || fail "FFmpeg 未生成带 PGS 的测试 MKV"

probe_codec="$(ffprobe -v error -select_streams s:0 \
    -show_entries stream=codec_name -of default=nw=1:nk=1 \
    "$probe_dir/embedded.mkv")" || fail "ffprobe 无法读取测试 PGS 轨道"
[ "$probe_codec" = "hdmv_pgs_subtitle" ] \
    || fail "测试 MKV 字幕轨道格式异常：$probe_codec"

if ! ffmpeg -loglevel error \
    -i "$probe_dir/embedded.mkv" \
    -map 0:s:0 -c:s copy \
    "$probe_dir/extracted.sup" > "$probe_dir/extract.log" 2>&1; then
    sed -n '1,120p' "$probe_dir/extract.log" >&2
    fail "FFmpeg 无法从测试 MKV 抽取 PGS"
fi
[ -s "$probe_dir/extracted.sup" ] || fail "FFmpeg 未抽取出测试 PGS"

if ! "$SECONV" "$probe_dir/extracted.sup" subrip \
    --ocr-engine:tesseract \
    --ocr-language:eng \
    "--output-folder:$probe_dir/ocr" \
    --overwrite > "$probe_dir/ocr.log" 2>&1; then
    sed -n '1,160p' "$probe_dir/ocr.log" >&2
    fail "PGS → SRT 实际转换失败"
fi
[ -s "$probe_dir/ocr/extracted.srt" ] || fail "PGS OCR 未生成 SRT"
grep -Fq "MOVIECLAW OCR TEST 2026" "$probe_dir/ocr/extracted.srt" \
    || fail "PGS OCR 结果内容不符合预期"

echo "字幕运行时自检通过：linux/$runtime_arch · seconv · ffmpeg · Tesseract（11 种语言）"
