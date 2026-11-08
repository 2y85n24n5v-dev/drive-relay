# Drive Relay —— 不用代理，把 Google Drive 搬到本机

一句话：**让 GitHub Actions 的境外 runner 去 Google Drive 取文件，把产物放到 GitHub Release（或 Hugging Face），你在 Mac 上从国内可达的镜像取回。**

不需要 VPN，不需要改 hosts，不需要自己的境外服务器。真正访问 Google 的是 GitHub 的 runner，你的 Mac 只访问 `github.com` / `ghfast.top` / `hf-mirror.com`。

> 适合：模型权重、数据集、别人分享的 Drive 文件夹。
> 不适合：盗版/侵权内容、别人的私有文件、把 GitHub 当长期网盘/CDN。
> 公开 Release = 任何人拿到链接都能下载。敏感数据请走 Hugging Face 私有仓库，或不要用这套工具。

---

## 0. 先选一条路

| | **A. GitHub Release**（`storage=gh`） | **B. Hugging Face**（`storage=hf`） |
|---|---|---|
| 额外账号 | 不需要 | 需要 HF 账号 + Write token |
| 仓库 | 必须 **Public** | 私有仓库，只有你能看 |
| 单文件 | Release 资产 < 2 GiB，自动 tar 分片 | 无 2 GiB 限制 |
| 本机下载 | `ghfast.top` 镜像 + `pull-release.sh` | `hf-mirror.com` + `hf download` |
| 速度 | 中等 | 通常更快 |
| 适合 | 一次性、总量 < 20 GB | 大数据、反复拉、要保密 |

第一次用、没有 HF 账号：**走 A**。HF 注册本身要能访问 huggingface.co，可能对你也是个门槛。

---

## 1. 文件清单

```
drive-relay/
├── relay.py                          # 在 runner 上跑：列清单、下载、打包、上传
├── requirements.txt                  # gdown + huggingface_hub
├── pull-release.sh                   # 在 Mac 上跑：从 Release 取回（macOS bash 3.2 可用）
├── .github/workflows/drive-relay.yml # Actions 网页上的按钮
├── tests/
│   ├── test_relay_offline.py         # 离线测试：假 gdown + 假 gh
│   └── test_pull_offline.sh          # 离线测试：file:// 模拟 Release
└── README.md
```

`relay.py` 只在 runner 上运行。**你的 Mac 不需要装 gdown、不需要装 Python 包、不需要 HF SDK**；拉取只用系统自带的 `curl`、`tar`、`shasum`、`python3`。

---

## 2. 一次性准备（约 10 分钟，只做一次）

### 2.1 建一个公开的中转仓库

1. 打开 <https://github.com/new>。
2. **Repository name** 填 `drive-relay`（随便起，下面命令里的 `你的用户名/drive-relay` 要对应）。
3. 可见性选 **Public**。
4. 点 **Create repository**。

> 为什么必须 Public：`ghfast.top` 是匿名镜像，读不到私有仓库的 Release。
> 这个仓库只放工具，不放数据；数据在 Release 里，下载完可以删。

### 2.2 上传文件

**最简单的网页操作：**

1. 在新仓库页面点 **Add file → Upload files**。
2. 把 `relay.py`、`requirements.txt`、`pull-release.sh` 拖进去，点 **Commit changes**。
3. 再点 **Add file → Create new file**：
   - 文件名框直接输入 `.github/workflows/drive-relay.yml`（斜杠会自动建目录）；
   - 把 workflow 文件的内容粘进去；
   - 点 **Commit changes**。
4. `tests/` 和 `README.md` 可选；不影响 runner 运行。想上传就再拖一次。

> 网页拖拽 `.github` 这种隐藏目录有时不成功；用第 3 步的“Create new file + 路径”最稳。
> 如果浏览器下载的 `pull-release.sh` 少了执行权限，本机 `chmod +x` 即可。

### 2.3 打开 Actions

1. 点仓库顶部的 **Actions**。
2. 如果看到 “Workflows aren’t being run on this repository”，点 **I understand my workflows, go ahead and enable them**。
3. 左侧应出现 **drive-relay**。

### 2.4 给 workflow 写权限（很重要）

1. **Settings → Actions → General**。
2. 拉到 **Workflow permissions**。
3. 选 **Read and write permissions**。
4. 点 **Save**。

> workflow 里已经写了 `permissions: contents: write`，但仓库/组织策略可能把它降级；不设这一步，`gh release create/upload` 常见 403：
> `Resource not accessible by integration`。

### 2.5（仅 B 路径）HF token

1. 在能打开 huggingface.co 的环境注册账号。
2. 打开 <https://huggingface.co/settings/tokens>，新建一个 **Write** token，复制 `hf_...`。
3. 回到 GitHub 仓库：**Settings → Secrets and variables → Actions → New repository secret**。
4. Name 填 `HF_TOKEN`，Secret 粘贴 token，保存。

---

## 3. 跑一次（A 路径：GitHub Release）

### 3.1 触发

1. 仓库 **Actions → drive-relay → Run workflow**（右侧按钮）。
2. 表单这样填：

| 字段 | 填什么 |
|---|---|
| `drive_url` | 例如 `https://drive.google.com/drive/folders/1DOcOPZb3-5cWxLqn256AhwUVjBPifhuf?usp=sharing` |
| `storage` | `gh` |
| `repo` | 留空（= 当前仓库） |
| `tag` | 例如 `loftr-20261108`（每个任务一个新 tag） |
| `dest` | 留空 |
| `force` | 不勾 |
| `part_size` | 留空（默认 1.9 GiB） |

3. 点 **Run workflow**，刷新页面，点进这次 run 的 **relay** job 看日志。

### 3.2 等它跑完

- 小文件夹（几百 MB）通常 5～15 分钟；大文件夹看体积和 Drive 速度。
- 日志里每个文件会打印进度；出现下面这行就是打包完成：

```
完成：N 个分片，X.X GiB
```

- 打开仓库 **Releases**，应看到 tag 对应的 Release，资产包括：
  - `relay.tar.part.000`、`relay.tar.part.001`…
  - `relay.parts.sha256`（分片校验清单）
  - `SHA256SUMS`（**最后上传**，看到它才代表这次真的完整）

> 如果 Release 里没有 `SHA256SUMS`，说明 workflow 还没跑完或中途失败。不要急着下载，先看日志。

### 3.3 tag 怎么取

- 字母、数字、点、下划线、短横线；不能以符号开头；不要用 `/`。
- **每个新链接用一个新 tag**。同一个 tag 下如果内容变了，工具会拒绝覆盖（防止把旧分片和新分片拼在一起）；确实要覆盖就勾 `force`（会清掉旧分片重传）。
- 同一份内容重跑同一个 tag 是安全的：已上传的分片会跳过。

---

## 4. 在 macOS 上拉回本机

### 4.1 拿到脚本

两种方式：

**方式 1：浏览器下载**

1. 打开仓库里的 `pull-release.sh`。
2. 点右上角 **Raw**，右键“存储为…”保存到 `~/Downloads/pull-release.sh`。
3. 浏览器下载的文件可能带 quarantine，执行一次：

```bash
xattr -d com.apple.quarantine ~/Downloads/pull-release.sh 2>/dev/null || true
```

**方式 2：git clone（如果装了 git）**

```bash
git clone https://github.com/你的用户名/drive-relay.git
cd drive-relay
```

### 4.2 赋执行权限并运行

```bash
cd ~/Downloads          # 或 cd drive-relay
chmod +x pull-release.sh

./pull-release.sh \
  -r 你的用户名/drive-relay \
  -t loftr-20261108 \
  -o ~/data/loftr
```

脚本会：

1. 查 Release 资产清单（`api.github.com`，国内可达）；
2. 先下 `relay.parts.sha256` 和 `SHA256SUMS`，确认产物完整；
3. 逐个分片从 `ghfast.top` 下载，支持 `curl -C -` 断点续传；
4. 每个分片校验大小 + sha256；镜像坏了自动回退 `github.com` 直连；
5. `cat 分片 | tar -xf -` 流式解包（不会先合并出一个和分片一样大的 `relay.tar`）；
6. 用 `SHA256SUMS` 校验解出来的每个文件；
7. 删除分片（加 `-k` 保留）。

看到 `完成：/Users/你/data/loftr` 就好了。

### 4.3 常用选项

```bash
# 直连 github.com（镜像抽风时）
./pull-release.sh -r 你的用户名/drive-relay -t TAG -o ~/data/xxx -n

# 保留分片，方便排查
./pull-release.sh -r 你的用户名/drive-relay -t TAG -o ~/data/xxx -k

# 强制重下所有分片（怀疑本地分片坏了）
./pull-release.sh -r 你的用户名/drive-relay -t TAG -o ~/data/xxx -f

# 换镜像（任何形如 https://xxx/https://github.com/... 的前缀）
RELAY_MIRROR="https://ghfast.top/" ./pull-release.sh -r ... -t ... -o ...
```

### 4.4 中途断了怎么办

直接重跑同一条命令。脚本会：

- 已完整且 sha256 对得上的分片：跳过；
- 下了一半的分片：`curl -C -` 续传；
- 解包到一半：`tar` 会覆盖已存在的文件，重跑即可。

### 4.5 macOS 注意事项

1. **python3**：脚本只用标准库。若提示找不到：
   ```bash
   xcode-select --install
   ```
2. **系统 bash 是 3.2**：脚本刻意没用 `mapfile`、关联数组、`${var,,}`；在 `/bin/bash` 3.2 下可跑（已测）。
3. **CRLF**：如果脚本从 Windows 拷来，报 `bad interpreter: /bin/bash^M`：
   ```bash
   tr -d '\r' < pull-release.sh > pull-release.sh.tmp
   mv pull-release.sh.tmp pull-release.sh
   chmod +x pull-release.sh
   ```
4. **磁盘**：解包前大约需要「分片总大小 + 解包后大小」；脚本会先用 `df` 检查，不够会直接报错。换个大磁盘，或分几次搬。
5. **别让 Mac 睡**：
   ```bash
   caffeinate -i ./pull-release.sh -r ... -t ... -o ...
   ```
   `-i` 只防空闲睡眠；**合盖仍会睡**，长任务请开着盖，或接电源用 `caffeinate -s`。
6. **中文文件名**：macOS 的 bsdtar 默认 UTF-8/PAX，解出来正常；别用 Windows 的 7-Zip 解。
7. **Gatekeeper**：`curl` 下载的文件不带 quarantine；浏览器下载的 `.sh` 才需要 `xattr`。
8. **Apple Silicon**：无影响，不需要 Rosetta。

---

## 5. 换一个新链接

同一套流程，只改 `drive_url` 和 `tag`（A 路径），或改 `drive_url`、`repo`、`dest`（B 路径）。

支持：

- 文件夹：`https://drive.google.com/drive/folders/<id>`
- 单文件：`https://drive.google.com/file/d/<id>/view`
- `https://drive.google.com/open?id=<id>`
- Google Docs/Sheets/Slides 的 `/d/<id>` 链接
- 裸 id（10 位以上）

示例：

| 任务 | tag | 本机命令 |
|---|---|---|
| LoFTR 权重 | `loftr` | `./pull-release.sh -r 你/drive-relay -t loftr -o ~/data/loftr` |
| ScanNet 子集 | `scannet-0618` | `./pull-release.sh -r 你/drive-relay -t scannet-0618 -o ~/data/scannet` |
| 单文件 | `ckpt-0618` | `./pull-release.sh -r 你/drive-relay -t ckpt-0618 -o ~/data/ckpt` |

> 同一个 Drive 文件夹里如果有子文件夹，`gdown` 会递归列出；空文件夹不会保留。
> 同名文件会被自动改成 `名字 (1).ext`，不会互相覆盖。

---

## 6. 用 gh CLI 把流程压成三行（可选）

macOS 装 gh 很方便：

```bash
brew install gh
gh auth login          # 选 GitHub.com -> HTTPS -> 浏览器登录
```

之后：

```bash
# 1) 触发（在仓库目录里跑；workflow 必须在默认分支上）
gh workflow run drive-relay.yml \
  -f drive_url='https://drive.google.com/drive/folders/xxxx' \
  -f storage=gh \
  -f tag=loftr-20261108

# 2) 等它跑完
gh run watch

# 3) 拉回
./pull-release.sh \
  -r "$(gh repo view --json nameWithOwner -q .nameWithOwner)" \
  -t loftr-20261108 \
  -o ~/data/loftr
```

清理 Release（下载确认无误后）：

```bash
gh release delete loftr-20261108 --yes --cleanup-tag
```

> `gh run watch` 默认看最近一次 run；如果同时跑了多个，用 `gh run list` 找 ID，再 `gh run watch <id>`。

---

## 7. B 路径：Hugging Face 私有仓库（可选）

### 7.1 跑 workflow

表单：

| 字段 | 填什么 |
|---|---|
| `drive_url` | Drive 链接 |
| `storage` | `hf` |
| `repo` | `你的HF用户名/drive-relay`（会自动建私有 dataset） |
| `tag` | 随便，HF 模式不用 |
| `dest` | 子目录，例如 `loftr`；不同任务用不同名字 |
| `force` | 一般不用 |

前提：2.5 里已加 `HF_TOKEN` secret。

HF 模式是**逐文件**的：下一个 → 传一个 → 删一个，磁盘峰值≈最大单文件，适合大文件；失败重跑时，已上传的文件会跳过。

### 7.2 在 Mac 上拉

HF 的下载需要 Python 包；建议用独立 venv，避免 Homebrew Python 的 `externally-managed-environment`：

```bash
python3 -m venv ~/.venvs/hf
source ~/.venvs/hf/bin/activate
pip install -U "huggingface_hub[cli]"

export HF_ENDPOINT=https://hf-mirror.com
export HF_TOKEN=hf_你的token        # 私有仓库需要

# 新版 CLI 是 hf；旧版是 huggingface-cli
hf download 你的HF用户名/drive-relay \
  --repo-type dataset \
  --local-dir ~/data/loftr
```

如果 `hf` 不存在，试：

```bash
huggingface-cli download 你的HF用户名/drive-relay \
  --repo-type dataset --local-dir ~/data/loftr
```

> `hf-mirror.com` 对公开仓库最稳；私有仓库带 token 通常也可以，但如果返回 401/403，说明镜像没转发认证。此时改用公开 HF 数据集，或走 A 路径（内容公开）。
> HF 免费额度/私有存储有限制，超大资料可能要付费；以 HF 页面为准。

---

## 8. 它到底怎么工作

### A 路径（gh）

```
Drive ──gdown──► runner 的 _relay_src/
                     │
                     ├─ 逐个文件算 sha256 → SHA256SUMS
                     │
                     └─ tar 流 ──► split ──► relay.tar.part.000/001/...
                                      │
                                      ├─ 上传分片
                                      ├─ 上传 relay.parts.sha256
                                      └─ 最后上传 SHA256SUMS（完成标记）
```

关键设计：

- **峰值磁盘 ≈ 总量 + 最大单文件**：`tarfile` 边写边删源文件；正在写的那一个文件在源目录和分片里各有一份，所以多出“最大单文件”，不是 2 倍总量。
- **确定性 tar**：文件按相对路径排序，`mtime/uid/gid` 归一化，重跑时分片边界稳定，才能安全跳过已上传分片。
- **两层校验**：`relay.parts.sha256` 管分片；`SHA256SUMS` 管解包后的文件。`SHA256SUMS` 最后上传，拉取端没有它就拒绝解包，避免拿到“传了一半”的 Release。
- **断点续传**：分片级 `curl -C -`；已完整且 sha256 对得上的直接跳过。
- **镜像回退**：`ghfast.top` 失败或内容不对，自动改 `github.com` 直连（国内实测可达）。

### B 路径（hf）

逐文件 `gdown` → 本地临时文件 → `HfApi.upload_file` → 删除本地。没有整包 tar，所以不受 2 GiB 限制；但受 job 6 小时和 runner 磁盘限制。

---

## 9. 限制与容量规划

| 限制 | 数值 | 说明 |
|---|---|---|
| GitHub Release 单资产 | **< 2 GiB** | 默认分片 1.9 GiB |
| 单个 job | **6 小时**（workflow 设 350 分钟） | 超时全部白跑；大文件夹要拆 |
| runner 磁盘 | 清理预装件后约 **25–35 GB** | 取决于 runner 镜像 |
| gh 模式建议总量 | **≤ 20 GB** | 峰值≈总量+最大单文件 |
| gh 模式建议单文件 | **≤ 10 GB** | 再大用 HF |
| Release 资产数 | 1000 | 本工具分片数远小于此 |
| 公开仓库 | gh 路径必须 | 私有 Release 镜像读不到 |
| HF | job 6h + 磁盘 + 私有存储额度 | 大资料看 HF 套餐 |

超了怎么办：

- 把 Drive 文件夹拆成几个子文件夹，分多次跑，用不同 `tag` / `dest`。
- 改用 `storage=hf`。
- 单个文件特别大（>15 GB）：gh 模式峰值会顶到磁盘，直接 HF。

---

## 10. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| Actions 里没有 **Run workflow** 按钮 | workflow 不在默认分支；把 `.github/workflows/drive-relay.yml` 提交到 `main` |
| Actions 页面没有 drive-relay | 文件路径不对（必须是 `.github/workflows/drive-relay.yml`）；或没点 Enable |
| `Resource not accessible by integration` / 403 | 2.4 的 Workflow permissions 没设成 Read and write |
| `Release 里没有 SHA256SUMS` | workflow 没跑完/失败了；看日志，等它出现再拉 |
| `Release 里没有 relay.parts.sha256` | 产物不完整；重跑 workflow |
| `分片数与清单不一致` | 可能正在上传；等几分钟重试。持续出现就 `force` 重跑 |
| `Too many users have viewed or downloaded this file` | Drive 配额限制。等 24 小时；让对方“复制一份”到自己账号再分享；本工具不带 cookies，绕不过 |
| `No space left on device` | 拆小文件夹，或 `storage=hf` |
| job 跑到 6 小时被取消 | 拆文件夹，分多个 tag |
| `ghfast.top` 502/超时 | 加 `-n` 直连；或 `RELAY_MIRROR` 换镜像 |
| 下载大小不符 / sha256 不符 | 镜像返回错误页；脚本会自动回退直连。重跑；还不行就 `-n`，或 `force` 重跑 relay |
| `tar: Unrecognized archive format` | 分片不完整；重跑（会续传） |
| `bad interpreter: /bin/bash^M` | CRLF；见 4.5 第 3 条 |
| `zsh: permission denied` | `chmod +x pull-release.sh` |
| `python3: command not found` | `xcode-select --install` |
| `curl: (35)` / `(7)` | 网络层问题；换网络，或先 `-n` 试直连 |
| HF `401` / `403` | token 没设、没权限，或镜像不转发私有认证；改用公开仓库/A 路径 |
| 解包后文件名乱码 | 在 macOS 上用 bsdtar 解；别用 Windows 7-Zip |

---

## 11. 自检（不需要网络）

在仓库目录里：

```bash
python3 tests/test_relay_offline.py
./tests/test_pull_offline.sh
```

- 第一个用假的 `gdown`/`gh` 跑完整 gh 上传流程（含重跑跳过、force 覆盖、单文件、HF 路径、URL/路径校验）。
- 第二个用 `file://` 模拟 GitHub API 和 Release，测正常拉取、续传、镜像回退、缺完成标记拒绝。
- 两个都不访问网络，macOS 自带工具即可。

---

## 12. 如果只是要 LoFTR 权重（捷径）

LoFTR 的原版权重已经在 HF 的 `kornia/loftr` 仓库里（README 写明 Original repo: `zju3dv/LoFTR`），**不需要 Drive 中转**：

```bash
mkdir -p ~/data/loftr
for f in loftr_indoor_ds_new.ckpt loftr_outdoor.ckpt; do
  curl -fL --retry 5 -C - \
    -o ~/data/loftr/$f \
    "https://hf-mirror.com/kornia/loftr/resolve/main/$f"
done
```

已知大小：

| 文件 | 字节 |
|---|---|
| `loftr_indoor_ds_new.ckpt` | 46,355,053 |
| `loftr_outdoor.ckpt` | 46,341,978 |
| `loftr_indoor.ckpt` | 另有，按需下 |

校验：

```bash
shasum -a 256 ~/data/loftr/*.ckpt
```

> 如果 Drive 文件夹里是 `loftr_outdoor_ds_new.ckpt`，`kornia/loftr` 里没有同名文件（只有 `loftr_outdoor.ckpt`），不要混用；用 Drive Relay 搬原文件，或先确认对应关系。

---

## 13. 安全与合规

- **公开 Release = 公开内容**。不要把私有数据、密钥、别人的未公开资料放进去。
- 下载完确认无误后删 Release：
  ```bash
  gh release delete <tag> --yes --cleanup-tag
  ```
  网页端也可以：Release → Delete。
- GitHub Actions 是 CI，不是免费网盘/CDN。只做个人临时中转，不要高频、大量、长期占用；否则可能被限流或封禁。
- 只搬运你有权限访问的文件；不要用本工具绕过 Drive 的访问控制或下载配额。
- HF token 是 Write 权限，泄露后立即在 HF 设置里 revoke；GitHub secret 不要写进 README。
- workflow 把 `drive_url`/`tag` 等通过环境变量传给 Python，tag 在 Python 侧白名单校验，避免把用户输入直接拼进 shell。

---

## 14. 常见问题

**Q：仓库可以私有吗？**
A：gh 路径不行——`ghfast.top` 匿名读不到私有 Release。要私有就用 HF。`pull-release.sh` 支持 `GITHUB_TOKEN` + `-n` 直连私有 Release，但镜像不可用，速度也差，不推荐。

**Q：支持断点续传吗？**
A：支持。本机分片级续传；runner 侧 gh 模式重跑会跳过同大小已存在分片，hf 模式跳过已上传文件。runner 是临时的，跨 job 不会保留源文件。

**Q：为什么不用 gdown 直接下？**
A：你的网络到 `drive.google.com` 是 DNS 污染 + SNI 重置，hosts 改不了；必须有境外出口。GitHub runner 就是现成的境外出口。

**Q：会不会被 GitHub 封？**
A：个人、低频、临时中转一般没问题；把它当 CDN 高频跑就有风险。用完删 Release，别公开传播。

**Q：能下 Google Docs/Sheets 吗？**
A：`gdown` 会尝试导出；格式/文件名由 Google 决定。二进制大文件（模型、数据集）才是这套工具的主场。

**Q：分片能不能用别的工具下？**
A：可以。Release 资产就是普通文件；按 `relay.tar.part.000` 的名字顺序 `cat` 起来就是 tar。但建议用 `pull-release.sh`，它会校验 sha256 并处理镜像回退。
