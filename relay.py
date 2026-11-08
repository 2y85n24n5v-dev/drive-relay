#!/usr/bin/env python3
"""Drive Relay — 在能访问 Google 的机器上把 Drive 内容搬到墙内可达的存储。

两种落地方式：

  --storage gh  GitHub Release（公开仓库；整包 tar 分片 + SHA256 清单）
  --storage hf  Hugging Face 私有仓库（逐文件上传，适合大文件/可续传）

这个脚本只在 GitHub Actions runner（或任何能访问 Google 的 Linux 机器）上跑；
本机用 pull-release.sh 从 ghfast.top / github.com 取回，全程不需要代理。

设计要点：
  * 先列清单，再逐个下载；下载完立即算 sha256。
  * gh 模式用 Python 的 tarfile 流式打包，边打包边删除源文件，峰值磁盘
    约等于「总大小 + 最大单文件」，而不是 2 倍总大小。
  * 分片按确定性顺序（文件名排序、mtime/uid/gid 归一化）生成，重跑时
    分片边界稳定，可安全跳过已上传的分片。
  * 只有所有分片都上传成功，最后才上传 SHA256SUMS，作为「Release 完整」
    的标记；pull 端缺这个文件就拒绝解包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path

# GitHub Release 单资产上限 2 GiB（2,147,483,648）。留约 150 MiB 余量。
PART_SIZE_DEFAULT = 1_992_294_400  # 1900 MiB
MIN_FREE_BYTES = 1 << 30  # 低于 1 GiB 就停止
PACK_SLACK_BYTES = 1 << 29  # 打包时额外预留 512 MiB

FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
FILE_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
OPEN_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
DOCS_RE = re.compile(
    r"docs\.google\.com/(?:document|spreadsheets|presentation|forms|drawings)"
    r"/d/([A-Za-z0-9_-]+)"
)
BARE_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# --------------------------------------------------------------------------- 基础


def log(msg: str, err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(rel: str) -> str:
    """把 gdown 给出的相对路径变成安全的、用 / 分隔的相对路径。"""
    rel = rel.replace("\\", "/").lstrip("/")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in rel):
        raise ValueError(f"文件名里含有控制字符，无法安全打包: {rel!r}")
    parts: list[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"路径里含有 ..，拒绝打包: {rel!r}")
        parts.append(part)
    if not parts:
        raise ValueError("空文件名")
    return "/".join(parts)


def unique_rel(rel: str, used: set[str]) -> str:
    """Drive 允许同名文件；这里加 (1)/(2) 后缀避免互相覆盖。"""
    if rel not in used:
        used.add(rel)
        return rel
    p = Path(rel)
    parent = "" if str(p.parent) == "." else str(p.parent) + "/"
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = f"{parent}{stem} ({i}){suffix}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


def parse_url(url: str) -> tuple[str, str]:
    """返回 ('folder'|'file', id)。支持文件夹、/file/d/、open?id=、docs 和裸 id。"""
    s = url.strip()
    if not s:
        raise SystemExit("Drive 链接为空")

    m = FOLDER_RE.search(s)
    if m:
        return "folder", m.group(1)
    m = FILE_RE.search(s) or OPEN_RE.search(s) or DOCS_RE.search(s)
    if m:
        return "file", m.group(1)
    if BARE_RE.fullmatch(s):
        return "file", s
    raise SystemExit(f"无法识别的 Google Drive 链接：{url}")


# --------------------------------------------------------------------------- 计划


def plan_folder(folder_id: str, timeout: float) -> list[tuple[str, str]]:
    import gdown

    url = f"https://drive.google.com/drive/folders/{folder_id}"
    log("读取文件夹清单（不下载）...")
    items = gdown.download_folder(
        url=url,
        skip_download=True,
        quiet=True,
        use_cookies=False,
        timeout=timeout,
    )
    plan: list[tuple[str, str]] = []
    for item in items or []:
        fid = getattr(item, "id", None)
        rel = getattr(item, "path", None)
        if not fid or not rel:
            continue
        plan.append((str(fid), safe_rel(str(rel))))
    if not plan:
        raise SystemExit(
            "文件夹里没有列出任何文件。请确认链接是「Anyone with the link」可读，"
            "且没有被 Drive 的访问配额限制。"
        )
    return plan


def plan_file(file_id: str, timeout: float) -> list[tuple[str, str]]:
    """单文件链接：先问 Drive 真实文件名，避免落地成 drive_file。"""
    import gdown

    log("解析文件名（不下载）...")
    item = gdown.download(
        id=file_id,
        skip_download=True,
        quiet=True,
        use_cookies=False,
        timeout=timeout,
    )
    rel = getattr(item, "path", None)
    if not rel and isinstance(item, str):
        rel = os.path.basename(item)
    if not rel:
        raise SystemExit("无法解析该文件的文件名（可能需要登录或没有权限）")
    return [(file_id, safe_rel(str(rel)))]


def build_plan(kind: str, ident: str, timeout: float) -> list[tuple[str, str]]:
    if kind == "folder":
        plan = plan_folder(ident, timeout)
    else:
        plan = plan_file(ident, timeout)
    used: set[str] = set()
    plan = [(fid, unique_rel(rel, used)) for fid, rel in plan]
    plan.sort(key=lambda x: x[1])
    return plan


# --------------------------------------------------------------------------- 下载


def check_free(root: Path) -> int:
    free = shutil.disk_usage(root).free
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"runner 磁盘只剩 {human(free)}，已停止。"
            "请把文件夹拆小，或改用 --storage hf（HF 支持更大的总容量）。"
        )
    return free


def download_one(fid: str, rel: str, root: Path, timeout: float, retries: int) -> int:
    import gdown

    local = root / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    out = gdown.download(
        id=fid,
        output=str(local),
        quiet=False,
        use_cookies=False,
        resume=True,
        timeout=timeout,
        retries=retries,
    )
    if not out:
        raise RuntimeError("gdown 没有返回文件路径")
    produced = Path(out)
    if not produced.exists():
        raise RuntimeError(f"gdown 报告成功但文件不存在: {produced}")
    if produced.resolve() != local.resolve():
        if local.exists():
            local.unlink()
        produced.rename(local)
    return local.stat().st_size


def write_sha256sums(root: Path, rels: list[str], out: Path) -> None:
    lines: list[str] = []
    for rel in rels:
        p = root / rel
        if not p.exists():
            raise RuntimeError(f"缺少文件，无法生成校验清单: {rel}")
        lines.append(f"{sha256_file(p)}  ./{rel}\n")
    out.write_text("".join(lines), encoding="utf-8")


def pack_and_split(
    root: Path,
    parts_dir: Path,
    rels: list[str],
    part_size: int,
) -> list[Path]:
    """流式 tar -> split；每写完一个文件就删除源文件，控制峰值磁盘。"""
    parts_dir.mkdir(parents=True, exist_ok=True)
    for old in parts_dir.glob("relay.tar.part.*"):
        old.unlink()

    target = str(parts_dir / "relay.tar.part.")
    cmd = ["split", "-b", str(part_size), "-d", "-a", "3", "-", target]
    log(f"打包并分片（每片 {human(part_size)}）...")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        with tarfile.open(
            fileobj=proc.stdin,
            mode="w|",
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
        ) as tf:
            for i, rel in enumerate(rels, 1):
                full = root / rel
                info = tf.gettarinfo(str(full), arcname=rel)
                # 归一化元数据，保证重跑时分片边界稳定。
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o644
                with open(full, "rb") as fh:
                    tf.addfile(info, fh)
                full.unlink()
                if i % 25 == 0 or i == len(rels):
                    log(f"  已打包 {i}/{len(rels)}")
        proc.stdin.close()
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"split 失败（退出码 {rc}）；通常表示磁盘空间不足")
    except BaseException:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise

    parts = sorted(parts_dir.glob("relay.tar.part.*"))
    if not parts:
        raise RuntimeError("没有生成任何分片")
    return parts


def write_part_hashes(parts: list[Path], out: Path) -> None:
    lines = [f"{sha256_file(p)}  {p.name}\n" for p in parts]
    out.write_text("".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- GitHub


def _gh_env_ok() -> None:
    if not shutil.which("gh"):
        raise SystemExit(
            "找不到 gh 命令。gh 模式需要 GitHub CLI；"
            "Actions runner 自带，本机请先安装 gh。"
        )
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        raise SystemExit(
            "gh 模式需要 GH_TOKEN（或 GITHUB_TOKEN）。"
            "在 Actions 里由 workflow 注入；本机可先 gh auth login。"
        )


def gh_release_exists(repo: str, tag: str) -> bool:
    p = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        capture_output=True,
        text=True,
    )
    if p.returncode == 0:
        return True
    msg = f"{p.stderr}\n{p.stdout}".lower()
    if "not found" in msg or "release not found" in msg:
        return False
    raise RuntimeError(
        "gh release view 失败："
        + (p.stderr.strip() or p.stdout.strip() or "未知错误")
        + "\n请检查仓库是否存在、token 是否有 contents: write 权限。"
    )


def gh_assets(repo: str, tag: str) -> dict[str, int]:
    p = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(
            "读取 Release 资产失败："
            + (p.stderr.strip() or p.stdout.strip() or "未知错误")
        )
    data = json.loads(p.stdout)
    return {a["name"]: int(a["size"]) for a in data.get("assets", [])}


def gh_download_text(repo: str, tag: str, name: str) -> str | None:
    tmp = Path(tempfile.mkdtemp(prefix="relay-gh-"))
    try:
        p = subprocess.run(
            [
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repo,
                "--pattern",
                name,
                "--dir",
                str(tmp),
                "--clobber",
            ],
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            return None
        f = tmp / name
        return f.read_text(encoding="utf-8") if f.exists() else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def gh_upload(repo: str, tag: str, path: Path) -> None:
    log(f"  上传 {path.name} ({human(path.stat().st_size)})")
    subprocess.run(
        ["gh", "release", "upload", tag, str(path), "--repo", repo, "--clobber"],
        check=True,
    )


def gh_delete_asset(repo: str, tag: str, name: str) -> None:
    log(f"  删除旧资产 {name}")
    subprocess.run(
        ["gh", "release", "delete-asset", tag, name, "--repo", repo, "--yes"],
        check=True,
    )


def relay_gh(args: argparse.Namespace, plan: list[tuple[str, str]]) -> int:
    _gh_env_ok()
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("gh 模式需要 --repo owner/name 或 GITHUB_REPOSITORY")
    if not REPO_RE.fullmatch(repo):
        raise SystemExit(f"仓库名格式不对（应为 owner/name）：{repo}")

    src = Path("_relay_src")
    parts_dir = Path("_relay_parts")
    for d in (src, parts_dir):
        if d.exists():
            shutil.rmtree(d)
    src.mkdir()

    sizes: dict[str, int] = {}
    failed: list[tuple[str, str]] = []
    for idx, (fid, rel) in enumerate(plan, 1):
        free = check_free(src)
        log(f"[{idx}/{len(plan)}] 下载 {rel}（磁盘余 {human(free)}）")
        try:
            size = download_one(fid, rel, src, args.timeout, args.retries)
        except Exception as exc:  # noqa: BLE001
            if args.fail_fast:
                raise RuntimeError(f"下载失败: {rel}\n{exc}") from exc
            # 单个文件没权限/被删除时，不让整批任务白跑。
            print(f"[跳过] {rel}: {exc}", file=sys.stderr, flush=True)
            failed.append((rel, str(exc)))
            continue
        sizes[rel] = size
        log(f"    完成 {human(size)}")

    if not sizes:
        raise RuntimeError(
            "所有文件都下载失败（常见原因：链接不是 Anyone with the link、"
            "文件被删除，或 Drive 配额用尽）。"
        )

    total = sum(sizes.values())
    max_file = max(sizes.values())
    free = shutil.disk_usage(src).free
    if free < max_file + PACK_SLACK_BYTES:
        raise RuntimeError(
            f"打包需要额外约 {human(max_file)}，当前只剩 {human(free)}。\n"
            "gh 模式建议总量 ≤ 20 GiB、单个文件 ≤ 10 GiB；更大的数据请用 "
            "--storage hf，或把 Drive 文件夹拆成几个子文件夹分别搬运。"
        )
    log(
        f"共 {len(sizes)}/{len(plan)} 个文件成功，{human(total)}；"
        f"最大单文件 {human(max_file)}"
    )

    rels = list(sizes)
    log("计算 sha256 清单...")
    write_sha256sums(src, rels, Path("SHA256SUMS"))

    parts = pack_and_split(src, parts_dir, rels, args.part_size)
    shutil.rmtree(src, ignore_errors=True)
    parts_bytes = sum(p.stat().st_size for p in parts)
    log(f"共 {len(parts)} 个分片，{human(parts_bytes)}")

    log("计算分片校验清单...")
    write_part_hashes(parts, Path("relay.parts.sha256"))

    if not gh_release_exists(repo, args.tag):
        log(f"创建 Release: {args.tag}")
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                args.tag,
                "--repo",
                repo,
                "--title",
                f"drive-relay {args.tag}",
                "--notes",
                "Google Drive relay artifacts. 下载请用 pull-release.sh。",
                "--latest=false",
            ],
            check=True,
        )

    existing = gh_assets(repo, args.tag)
    new_sums = Path("SHA256SUMS").read_text(encoding="utf-8")
    new_parts = Path("relay.parts.sha256").read_text(encoding="utf-8")

    old_sums = gh_download_text(repo, args.tag, "SHA256SUMS") if "SHA256SUMS" in existing else None
    old_parts = (
        gh_download_text(repo, args.tag, "relay.parts.sha256")
        if "relay.parts.sha256" in existing
        else None
    )
    sums_changed = old_sums is not None and old_sums.strip() != new_sums.strip()
    layout_changed = old_parts is not None and old_parts.strip() != new_parts.strip()

    if sums_changed and not args.force:
        raise SystemExit(
            "这个 tag 下已经有另一份内容（SHA256SUMS 不一致）。\n"
            "Drive 里的文件可能被改过。请换一个新 tag，或勾选 force 覆盖旧分片。"
        )
    if layout_changed and not args.force:
        raise SystemExit(
            "这个 tag 下已有分片，但分片布局和这次不同（可能改过 --part-size）。\n"
            "请换一个新 tag，或勾选 force 清掉旧分片重传。"
        )

    has_parts = any(n.startswith("relay.tar.part.") for n in existing)
    # 旧分片但没有布局清单，说明是旧版本/半途中断，无法确认内容，清掉重来。
    reset = args.force or layout_changed or sums_changed or (has_parts and old_parts is None)
    if reset:
        for name in sorted(existing):
            if name.startswith("relay.tar.part.") or name in ("relay.parts.sha256", "SHA256SUMS"):
                gh_delete_asset(repo, args.tag, name)
        existing = {}

    # 先上传分片布局清单：即使中途失败，下一次也能据此判断能否续传。
    gh_upload(repo, args.tag, Path("relay.parts.sha256"))

    for p in parts:
        size = p.stat().st_size
        if p.name in existing and existing[p.name] == size and not args.force:
            log(f"  跳过已存在: {p.name}")
            continue
        gh_upload(repo, args.tag, p)

    gh_upload(repo, args.tag, Path("SHA256SUMS"))  # 最后上传：完成标记

    log("")
    log(f"完成：{len(parts)} 个分片，{human(parts_bytes)}")
    log(f"Release: https://github.com/{repo}/releases/tag/{args.tag}")
    log(
        "本机下载："
        f"./pull-release.sh -r {repo} -t {args.tag} -o ~/data/{args.tag}"
    )
    if failed:
        log("")
        log(
            f"注意：{len(failed)} 个文件没有下载成功，Release 只包含其余 "
            f"{len(sizes)} 个文件。",
            err=True,
        )
        for rel, msg in failed:
            log(f"  - {rel}: {msg}", err=True)
        log(
            "修好共享权限后，用同一个 tag 加 force=true 重跑即可补齐。",
            err=True,
        )
        return 1
    return 0


# --------------------------------------------------------------------------- Hugging Face


def relay_hf(args: argparse.Namespace, plan: list[tuple[str, str]]) -> int:
    if not args.repo:
        raise SystemExit("hf 模式需要 --repo 用户名/仓库名")
    if not REPO_RE.fullmatch(args.repo):
        raise SystemExit(f"HF 仓库名格式不对（应为 user/name）：{args.repo}")

    from huggingface_hub import HfApi

    api = HfApi(token=args.token)
    api.create_repo(
        repo_id=args.repo,
        repo_type=args.repo_type,
        private=True,
        exist_ok=True,
    )
    try:
        info = api.repo_info(args.repo, repo_type=args.repo_type)
        if not getattr(info, "private", True):
            log("警告：这个 HF 仓库不是私有仓库，上传的内容会公开可见。")
    except Exception:
        pass

    dest = safe_rel(args.dest) if args.dest.strip() else ""
    work = Path("_relay_tmp")
    work.mkdir(exist_ok=True)

    try:
        existing = set(api.list_repo_files(args.repo, repo_type=args.repo_type))
    except Exception:
        existing = set()

    failed: list[str] = []
    for idx, (fid, rel) in enumerate(plan, 1):
        target = f"{dest}/{rel}" if dest else rel
        if target in existing and not args.force:
            log(f"[{idx}/{len(plan)}] 跳过（已存在）: {target}")
            continue

        local = work / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        check_free(work)
        log(f"[{idx}/{len(plan)}] 下载 {rel}")
        try:
            size = download_one(fid, rel, work, args.timeout, args.retries)
            log(f"    已下载 {human(size)}，上传 {target}")
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    api.upload_file(
                        path_or_fileobj=str(local),
                        path_in_repo=target,
                        repo_id=args.repo,
                        repo_type=args.repo_type,
                        commit_message=f"relay: {rel}",
                    )
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt == 3:
                        break
                    wait = 2 ** attempt
                    log(f"    上传失败（{exc}），{wait}s 后重试")
                    time.sleep(wait)
            if last_exc is not None:
                raise last_exc
            log(f"    上传完成: {target}")
            if not args.keep:
                local.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            if args.fail_fast:
                raise
            print(f"    失败: {exc}", file=sys.stderr, flush=True)
            failed.append(f"{rel}: {exc}")
            time.sleep(3)

    if failed:
        print("\n以下文件失败：", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\n可以修好后重跑：已上传的文件会跳过，未完成的会重新下载。",
            file=sys.stderr,
        )
        return 1

    log(f"\n全部完成：{args.repo}（{len(plan)} 个文件）")
    log(f"https://huggingface.co/{'datasets/' if args.repo_type == 'dataset' else ''}{args.repo}")
    return 0


# --------------------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 Google Drive 文件搬到 GitHub Release 或 Hugging Face。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--url", required=True, help="Google Drive 文件夹/文件链接，或裸 id")
    ap.add_argument("--storage", default="gh", choices=["gh", "hf"], help="落地方式")
    ap.add_argument("--repo", default="", help="目标仓库：gh 用 owner/name；hf 用 user/name")
    ap.add_argument(
        "--repo-type",
        default="dataset",
        choices=["dataset", "model"],
        help="[hf] HF 仓库类型",
    )
    ap.add_argument("--dest", default="", help="[hf] 仓库内子目录")
    ap.add_argument("--tag", default="relay", help="[gh] Release 标签")
    ap.add_argument("--keep", action="store_true", help="[hf] 上传后保留本地临时文件")
    ap.add_argument("--force", action="store_true", help="已存在的产物也重新上传/覆盖")
    ap.add_argument("--token", default=os.environ.get("HF_TOKEN"), help="[hf] HF token")
    ap.add_argument("--part-size", type=int, default=PART_SIZE_DEFAULT, help="[gh] 分片字节数")
    ap.add_argument("--timeout", type=float, default=60.0, help="网络超时秒数")
    ap.add_argument("--retries", type=int, default=3, help="gdown 每个文件的重试次数")
    ap.add_argument(
        "--fail-fast",
        action="store_true",
        help="任一文件失败就退出（默认跳过失败文件，其余照常打包上传）",
    )
    ap.add_argument("--debug", action="store_true", help="出错时打印完整 traceback")
    args = ap.parse_args()

    try:
        if not TAG_RE.fullmatch(args.tag):
            raise SystemExit(
                "tag 只能包含字母、数字、点、下划线、短横线，且不能以符号开头："
                f"{args.tag}"
            )
        if not (100 * 1024 * 1024 <= args.part_size < 2 * 1024 * 1024 * 1024):
            raise SystemExit("--part-size 必须在 100 MiB 到 2 GiB 之间")
        if args.storage == "hf" and not args.token:
            raise SystemExit("hf 模式需要 HF_TOKEN（workflow 里用 secrets.HF_TOKEN）")

        kind, ident = parse_url(args.url)
        plan = build_plan(kind, ident, args.timeout)
        total_hint = f"{len(plan)} 个文件"
        log(f"计划：{total_hint}（{kind} {ident}）")

        if args.storage == "gh":
            return relay_gh(args, plan)
        return relay_hf(args, plan)

    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            traceback.print_exc()
        print(f"\n[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        text = str(exc).lower()
        if "too many users" in text or "quota" in text:
            print(
                "提示：这是 Drive 的下载配额限制。等 24 小时，或让对方复制一份到"
                "自己账号再分享；也可以试 --storage hf。",
                file=sys.stderr,
            )
        elif "no space" in text or "disk" in text or "磁盘" in text or "broken pipe" in text:
            print(
                "提示：runner 磁盘不足。把文件夹拆小，或改用 --storage hf。",
                file=sys.stderr,
            )
        elif "owner and editors" in text:
            print(
                "提示：这个 Drive 文件没有公开下载权限，匿名中转下不了。"
                "让所有者把该文件设为「Anyone with the link」并允许下载，"
                "或只搬其余文件（默认会跳过失败文件）。",
                file=sys.stderr,
            )
        elif "permission" in text or "403" in text or "not found" in text:
            print(
                "提示：确认 Drive 链接是「Anyone with the link」，"
                "以及仓库 Settings → Actions → Workflow permissions 是 Read and write。",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
