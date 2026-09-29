# paperbox 的 docling-serve，**部署在 fnOS NAS 上**（2026-09-29 起）

## 为什么搬走

本机（WSL2，上限 9GB）里 docling 只能给 3000m。公式密集的论文（`2404.05260`，7 页）
**每次都在 ~151s 断连**：容器 `memory.peak == memory.max == 3000MiB`、cgroup `memory.events`
的 `max` 计数 7179、`oom_kill 0`（不是被杀，是在上限处反复回收/分配失败），断连后 worker
还继续烧 ~5 分钟 CPU。详见 `.hermes/plans/2026-09-28_160551-docling-parser-backend.md` §0.7（T2c）。

fnOS：`fishsky@192.168.31.53:65422`，8 核 / 15.6GB 内存，`/vol1` 剩 ~39GB。给 docling 8GB 硬盘内存，
就不会顶到上限。

## 为什么是「离线自建镜像」而不是 `docker build`（下载模型）

上游 quay 镜像里**没有**公式模型 `docling-project/CodeFormulaV2`，而 `do_formula_enrichment=true`
缺它直接 404；所以镜像必须自带这个模型。但 fnOS 上：

- `huggingface.co` **DNS SERVFAIL**、直连 000（上游 DNS 就不给解）；
- 常见代理端口全不通（31.1:7890、31.211:7890、31.53:7890、31.1:7897 都是 000）；
- 而 `quay.io` **是通的**（`/v2/` 回 401）。

⇒ fnOS 能 pull 基础镜像，但**下不了 HF 模型**。于是流程拆成两步：

1. 在本机把模型从已建好的镜像里抽出来（`docker cp`），局域网传到 fnOS；
2. fnOS 上 pull 基础镜像，用 `Dockerfile.offline` 只做 `COPY`（零外网）。

## 部署步骤（改完记得同步这份文件与 plan）

```bash
# ── 1) 本机（WSL）抽出公式模型并传过去 ────────────────────────────────
cid=$(docker create paperbox-docling-cpu:v1.35.0-formula)
docker cp $cid:/opt/app-root/src/.cache/docling/models/docling-project--CodeFormulaV2 \
  /mnt/c/Users/xuanj/Desktop/hermes/paperbox-data/docling/stage/
docker rm $cid
# 传输（WSL 里已装好 fnOS 的免密 key：install -m600 ~/.ssh/id_ed25519 /root/.ssh/）
cd /mnt/c/Users/xuanj/Desktop/hermes/paperbox-data/docling/stage
tar cf - docling-project--CodeFormulaV2 | ssh -p 65422 fishsky@192.168.31.53 \
  'mkdir -p /vol1/docker/docling-build && tar xf - -C /vol1/docker/docling-build'

# ── 2) fnOS：拉基础镜像 + 落文件 ─────────────────────────────────────
ssh -p 65422 fishsky@192.168.31.53
docker pull quay.io/docling-project/docling-serve-cpu:v1.35.0
cd /vol1/docker/docling-build
# 把本仓库的 infra/docling/Dockerfile.offline 与 infra/docling/fnos/docker-compose.yml 放到这里
docker compose build          # 只跑一条 COPY，秒级
docker compose up -d
docker compose ps

# ── 3) 从本机（Windows）验证 ─────────────────────────────────────────
cd C:\Users\xuanj\Desktop\hermes\paperbox
uv run python scripts/probe_docling_markdown.py logs/eval/docling/corpus/2404.05260.pdf `
  --formula --formats md --out logs/eval/docling/probe-fnos --document-timeout 280
```

## 应用侧怎么切

应用侧只有一个地址键：仓库根 `.env` 的 `DOCLING_URL`。

- 指向 NAS：`DOCLING_URL=http://192.168.31.53:8091`
- 回滚本机：`DOCLING_URL=http://127.0.0.1:8091` + `cd infra && docker compose up -d docling`

`PARSER_BACKEND` 仍是 `pypdf`（T8 之前不改），所以这一步只是"把 docling 挪个地方"，
不影响任何业务代码。

## 已知边界

- NAS 上 docling 用的是**同一份镜像内容**（tag `paperbox-docling-cpu:v1.35.0-formula`），
  只是模型是 COPY 进去而不是 download 进去的，解析行为应逐字节一致 —— 换机后必须
  用 `probe-T2` 那 6 篇重跑一遍做**对照**（heading 层级 / 页标记数 / 表格数 / 字符数）。
- fnOS 上没有代理，`docker compose build` 若哪天需要下载东西会失败；保持"离线可构建"。
- 局域网实测：`dd` 裸测 ~9MB/s，但 611MB 模型用 `tar-over-ssh` 实传 **4m04s（≈2.5MB/s）**
  —— tar + ssh 加密有开销，别按裸带宽估时间。