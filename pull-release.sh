#!/bin/bash
# pull-release.sh — 从 GitHub Release 取回 drive-relay 的产物。
#
# 设计目标：macOS 自带 /bin/bash 3.2、curl、tar(bsdtar)、python3 即可运行；
# 不需要 pip install，不需要代理。国内默认走 ghfast.top 镜像，失败自动回退
# github.com 直连。
#
# 用法：
#   ./pull-release.sh -r owner/repo -t relay -o ~/data/xxx
#
# 选项：
#   -r  owner/repo        必填
#   -t  Release 标签       默认 relay
#   -o  输出目录           默认 ./data/<tag>
#   -n  不走镜像，直连 github.com
#   -k  保留分片（默认解包成功后删除）
#   -f  强制重新下载已有分片
#   -h  帮助
#
# 环境变量：
#   RELAY_MIRROR  镜像前缀，默认 https://ghfast.top/
#   RELAY_API     API 地址，默认 https://api.github.com（测试用）
#   GITHUB_TOKEN  可选；私有仓库或提高 API 限额

set -euo pipefail
export LC_ALL=C

REPO=""
TAG="relay"
OUT=""
MIRROR="${RELAY_MIRROR-https://ghfast.top/}"
API="${RELAY_API-https://api.github.com}"
NO_MIRROR=0
KEEP=0
FORCE=0
TMP=""
PARTS=""

usage() {
  sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "" >&2
  echo "错误：$*" >&2
  if [ -n "${PARTS:-}" ]; then
    echo "分片目录：$PARTS（直接重跑会续传；加 -f 强制重下）" >&2
  fi
  exit 1
}

cleanup() {
  if [ -n "${TMP:-}" ] && [ -d "$TMP" ]; then
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

while getopts "r:t:o:nkfh" opt; do
  case "$opt" in
    r) REPO="$OPTARG" ;;
    t) TAG="$OPTARG" ;;
    o) OUT="$OPTARG" ;;
    n) NO_MIRROR=1 ;;
    k) KEEP=1 ;;
    f) FORCE=1 ;;
    h) usage; exit 0 ;;
    \?) usage; exit 2 ;;
  esac
done

[ -n "$REPO" ] || { usage; die "必须用 -r 指定 owner/repo"; }
case "$REPO" in
  */*) ;;
  *) die "-r 的格式是 owner/repo" ;;
esac
case "$REPO" in
  *[!A-Za-z0-9_./-]*) die "仓库名里有非法字符：$REPO" ;;
esac
case "$TAG" in
  ""|*[!A-Za-z0-9._-]*|[-._]*) die "tag 只能包含字母、数字、点、下划线、短横线：$TAG" ;;
esac
[ -n "$OUT" ] || OUT="./data/$TAG"

command -v curl >/dev/null 2>&1 || die "找不到 curl"
command -v tar >/dev/null 2>&1 || die "找不到 tar"
command -v python3 >/dev/null 2>&1 || die "找不到 python3（macOS 可执行 xcode-select --install）"

if command -v shasum >/dev/null 2>&1; then
  SHA_KIND="shasum"
elif command -v sha256sum >/dev/null 2>&1; then
  SHA_KIND="sha256sum"
else
  die "找不到 shasum / sha256sum"
fi

if [ "$NO_MIRROR" -eq 1 ]; then
  MIRROR=""
elif [ -n "$MIRROR" ]; then
  MIRROR="${MIRROR%/}/"
fi
API="${API%/}"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/relay.XXXXXX") || die "mktemp 失败"

CURL_OPTS=(-fL --retry 5 --retry-delay 3 --connect-timeout 20)
if [ -t 2 ]; then
  CURL_VERBOSE=(--progress-bar)
else
  CURL_VERBOSE=(-sS)
fi

mirror_curl() {
  curl "${CURL_OPTS[@]}" "${CURL_VERBOSE[@]}" "$@"
}

api_curl() {
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl "${CURL_OPTS[@]}" "${CURL_VERBOSE[@]}" \
      -H "Authorization: Bearer $GITHUB_TOKEN" "$@"
  else
    curl "${CURL_OPTS[@]}" "${CURL_VERBOSE[@]}" "$@"
  fi
}

file_size() {
  wc -c < "$1" | tr -d ' '
}

sha_of() {
  if [ "$SHA_KIND" = "shasum" ]; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

normalize_manifest() {
  local f="$1"
  awk '{sub(/\r$/, "")} {print}' "$f" > "$f.tmp"
  mv "$f.tmp" "$f"
}

valid_manifest() {
  [ -s "$1" ] || return 1
  awk 'NF >= 2 && length($1) == 64 && $1 ~ /^[0-9a-f]+$/ { ok++; next }
       { bad = 1 }
       END { exit (ok > 0 && !bad) ? 0 : 1 }' "$1"
}

echo "查询 $REPO 的 Release: $TAG"
api_curl -o "$TMP/release.json" \
  "$API/repos/$REPO/releases/tags/$TAG" \
  || die "取不到 Release。检查仓库/标签是否存在、仓库是否公开（公开仓库无需 token）"

REL_ID=$(python3 - "$TMP/release.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d["id"])
PY
) || die "Release JSON 解析失败"

ASSETS_TSV="$TMP/assets.tsv"
N=$(python3 - "$TMP/release.json" "$ASSETS_TSV" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
a = d.get("assets", [])
with open(sys.argv[2], "w", encoding="utf-8") as f:
    for x in a:
        f.write("%s\t%s\t%s\n" % (x["name"], x["size"], x["browser_download_url"]))
print(len(a))
PY
) || die "Release 资产解析失败"

# Release 对象的 assets 数组最多 30 条；超过时改查 assets 分页接口。
if [ "$N" -ge 30 ]; then
  : > "$ASSETS_TSV"
  page=1
  while :; do
    api_curl -o "$TMP/assets.json" \
      "$API/repos/$REPO/releases/$REL_ID/assets?per_page=100&page=$page" \
      || die "取资产列表失败（第 $page 页）"
    M=$(python3 - "$TMP/assets.json" "$ASSETS_TSV" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
with open(sys.argv[2], "a", encoding="utf-8") as f:
    for x in d:
        f.write("%s\t%s\t%s\n" % (x["name"], x["size"], x["browser_download_url"]))
print(len(d))
PY
    ) || die "资产列表解析失败"
    [ "$M" -lt 100 ] && break
    page=$((page + 1))
  done
fi

if ! awk -F'\t' '$1 == "SHA256SUMS" { found = 1 } END { exit !found }' "$ASSETS_TSV"; then
  die "Release 里没有 SHA256SUMS：workflow 可能还没跑完（它是最后上传的完成标记）。请等日志结束再试"
fi
if ! awk -F'\t' '$1 == "relay.parts.sha256" { found = 1 } END { exit !found }' "$ASSETS_TSV"; then
  die "Release 里没有 relay.parts.sha256：产物不完整"
fi

awk -F'\t' '/^relay[.]tar[.]part[.]/ { print $1 }' "$ASSETS_TSV" | sort > "$TMP/partnames.txt"
PART_COUNT=$(wc -l < "$TMP/partnames.txt" | tr -d ' ')
[ "$PART_COUNT" -gt 0 ] || die "Release 里没有 relay.tar.part.* 分片"

TOTAL_BYTES=$(awk -F'\t' '/^relay[.]tar[.]part[.]/ { s += $2 } END { print s + 0 }' "$ASSETS_TSV")
PART_HUMAN=$(awk -v b="$TOTAL_BYTES" 'BEGIN {
  split("B KiB MiB GiB TiB", u, " "); i = 1
  while (b >= 1024 && i < 5) { b /= 1024; i++ }
  printf "%.1f %s", b, u[i]
}')

echo "共 $PART_COUNT 个分片，$PART_HUMAN"

mkdir -p "$OUT" || die "无法创建输出目录：$OUT"
PARTS="$OUT/.relay_parts"
mkdir -p "$PARTS" || die "无法创建分片目录：$PARTS"

AVAIL_KB=$(df -Pk "$OUT" | awk 'NR == 2 { print $4 }')
NEED_KB=$(( TOTAL_BYTES / 1024 * 2 + 2048 ))
if [ -n "${AVAIL_KB:-}" ] && [ "$AVAIL_KB" -lt "$NEED_KB" ]; then
  die "磁盘可能不够：解包前后大约需要 $(( NEED_KB / 1024 )) MiB，当前可用 $(( AVAIL_KB / 1024 )) MiB。换一个磁盘或用 --storage hf"
fi

fetch_manifest() {
  local name="$1" dst="$2" url
  url=$(awk -F'\t' -v n="$name" '$1 == n { print $3; exit }' "$ASSETS_TSV")
  [ -n "$url" ] || die "Release 缺资产：$name"

  if [ "$NO_MIRROR" -eq 0 ] && [ -n "$MIRROR" ]; then
    if mirror_curl -o "$dst" "${MIRROR}${url}" && valid_manifest "$dst"; then
      normalize_manifest "$dst"
      return 0
    fi
    echo "  镜像返回的 $name 不合法，改直连" >&2
    rm -f "$dst"
  fi

  api_curl -o "$dst" "$url" || die "下载 $name 失败"
  valid_manifest "$dst" || die "$name 内容不合法（可能是镜像错误页）"
  normalize_manifest "$dst"
}

echo "下载校验清单..."
fetch_manifest "relay.parts.sha256" "$TMP/relay.parts.sha256"
fetch_manifest "SHA256SUMS" "$TMP/SHA256SUMS"

MANIFEST_COUNT=$(wc -l < "$TMP/relay.parts.sha256" | tr -d ' ')
if [ "$MANIFEST_COUNT" -ne "$PART_COUNT" ]; then
  die "分片数与清单不一致（Release $PART_COUNT，清单 $MANIFEST_COUNT）。可能正在上传，请稍后重试"
fi

if [ "$FORCE" -eq 1 ]; then
  rm -f "$PARTS"/relay.tar.part.*
fi

paths=()
while IFS= read -r name; do
  size=$(awk -F'\t' -v n="$name" '$1 == n { print $2; exit }' "$ASSETS_TSV")
  url=$(awk -F'\t' -v n="$name" '$1 == n { print $3; exit }' "$ASSETS_TSV")
  sha=$(awk -v n="$name" '$2 == n { print $1; exit }' "$TMP/relay.parts.sha256")
  [ -n "$size" ] || die "资产列表里没有 $name"
  [ -n "$url" ] || die "资产列表里没有 $name 的下载地址"
  [ -n "$sha" ] || die "分片清单里没有 $name"

  dst="$PARTS/$name"
  if [ -f "$dst" ] && [ "$(file_size "$dst")" = "$size" ]; then
    if [ "$(sha_of "$dst")" = "$sha" ]; then
      echo "  已完整：$name"
      paths+=("$dst")
      continue
    fi
    rm -f "$dst"
  fi

  if [ -f "$dst" ]; then
    echo "  续传：$name（已有 $(file_size "$dst") / $size 字节）"
  else
    echo "  下载：$name（$size 字节）"
  fi

  ok=0
  if [ "$NO_MIRROR" -eq 0 ] && [ -n "$MIRROR" ]; then
    if mirror_curl -C - -o "$dst" "${MIRROR}${url}" \
       && [ "$(file_size "$dst")" = "$size" ] \
       && [ "$(sha_of "$dst")" = "$sha" ]; then
      ok=1
    else
      echo "  镜像失败或校验不符，改直连" >&2
      rm -f "$dst"
    fi
  fi

  if [ "$ok" -eq 0 ]; then
    if ! api_curl -C - -o "$dst" "$url"; then
      rm -f "$dst"
      api_curl -o "$dst" "$url" || die "下载 $name 失败"
    fi
    [ "$(file_size "$dst")" = "$size" ] || die "$name 大小不符（期望 $size，实际 $(file_size "$dst")）"
    [ "$(sha_of "$dst")" = "$sha" ] || { rm -f "$dst"; die "$name 的 sha256 不符"; }
  fi

  paths+=("$dst")
done < "$TMP/partnames.txt"

echo "解包 -> $OUT"
cat "${paths[@]}" | tar -xf - -C "$OUT" || die "解包失败。分片保留在 $PARTS，重跑可续传"
cp "$TMP/SHA256SUMS" "$OUT/SHA256SUMS"

echo "校验文件..."
if [ "$SHA_KIND" = "shasum" ]; then
  ( cd "$OUT" && shasum -a 256 -c SHA256SUMS ) || die "文件 sha256 校验失败"
else
  ( cd "$OUT" && sha256sum -c SHA256SUMS ) || die "文件 sha256 校验失败"
fi

if [ "$KEEP" -eq 0 ]; then
  rm -rf "$PARTS"
else
  echo "分片保留在：$PARTS"
fi

echo ""
echo "完成：$OUT"
