# 知序 · 科研助手（Zhixu Research Assistant）

一个本地运行的个人科研文献工作台。支持真实 PDF 解析与扫描件 OCR、文献版本比对、保真原文阅读、带页码与坐标定位的证据检索，以及可配置 OpenAI 兼容模型的文献问答、句段对照翻译与研究写作。所有文献副本、会话、笔记版本、阅读位置和后台任务均保存在本地 SQLite 中。

## 核心特性

- **文献库与版本管理**：支持拖拽/批量上传 PDF（单份最大 100 MB）或从 `pdf/` 导入样例论文；基于 SHA-256 检测完全重复文件，按 arXiv ID/版本与规范标题自动归组论文家族，支持同族不同版本的文本块差异对比及原页定位。
- **PDF 解析与扫描件 OCR**：基于 PyMuPDF 逐页提取多栏/旋转页坐标、正文、表格、独立公式、目录章节与参考文献；扫描件优先调用 Tesseract，默认内置 RapidOCR ONNX，macOS 另有 Apple Vision 回退。支持解析器升级后“从首页重建解析”以刷新派生文本。
- **原文阅读工作台**：提供页面保真渲染、可选文字层、全文检索、目录与正文/参考文献双向跳转、证据坐标高亮，并自动持久化每篇文献的阅读页码与滚动位置。
- **证据问答与引用核验**：
  - **无模型可用**：默认启用本地 BM25 检索摘录模式，返回带页码与坐标的原文证据片段。
  - **接入模型后**：生成逐条绑定来源 `[1]` 的结构化回答，并在本地对数字/单位、逐字引句与实验关系进行规则校验；校验未通过时自动降级展示原文摘录并保留完整模型草稿供核对。
- **全文与句段对照翻译**：支持中英互译，涵盖划词、段落、整页与页码范围后台翻译；提供不依赖模型的 token 工作量估算、页级检查点断点续传，以及按原文句段编号绑定的对照翻译与双向跳转导出（Markdown / HTML）。
- **论文发现、引用图谱与研究写作**：支持检索 arXiv 并直接下载校验入库；基于共用参考文献与明确匹配规则构建本地引用图谱并导出 Mermaid / DOT；可基于选定文献生成摘要、综述、研究方向与提纲，保存多版本研究笔记并导出 Markdown / BibTeX。
- **实验数据 CSV 绘图**：无需语言模型，上传 CSV（最大 2 MB）即可生成折线图/柱状图与基于统计的图注，支持数据预览及 PNG / SVG 导出。

---

## 快速开始

### 环境要求

- **Python 3.12**（或已安装 [`uv`](https://docs.astral.sh/uv/)，启动脚本可自动创建 Python 3.12 虚拟环境）
- **Node.js 20.19+**（建议 Node 22）
- 首次启动需要联网安装锁定依赖

### 启动应用

```bash
git clone https://github.com/mocchh/zhixu-research-assistant.git
cd zhixu-research-assistant
bash scripts/start.sh
```

启动完成后打开 **http://127.0.0.1:8000**（FastAPI 接口文档位于 **http://127.0.0.1:8000/docs**）：

1. 在**文献库**点击“导入样例”加载 `pdf/` 中的示例论文，或直接拖入本地 PDF。
2. 等待后台解析完成后，即可进入**阅读工作台**、**文献问答**或**引用图谱**。
3. 按 `Ctrl+C` 停止服务；再次运行 `bash scripts/start.sh` 即可继续使用。

> **本地数据说明**：文献副本、SQLite 数据库、缓存与配置保存在 `data/` 目录（已被 `.gitignore` 忽略），不会修改 `pdf/` 中的原始样例文件。可通过环境变量 `RESEARCH_DATA_DIR` 自定义数据目录。同一数据目录由 `.workspace.lock` 文件锁保护，防止多实例并发写入冲突。

### 开发模式

首次运行过 `bash scripts/start.sh` 完成环境准备后，可使用热重载开发模式（后端端口 `8000`，前端 Vite 端口 `5173` 并自动代理 `/api`）：

```bash
bash scripts/dev.sh
```

---

## 模型配置

应用兼容任意 OpenAI `chat/completions` 接口，支持两种配置方式（**界面保存的配置优先于环境变量**）：

1. **界面配置（推荐）**：进入左侧栏**模型设置**，填写服务地址、模型名称与 API 密钥，点击测试连接并保存。
2. **环境变量**：复制 `.env.example` 为 `.env` 并填写：
   ```env
   LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
   LLM_MODEL=qwen-plus
   LLM_API_KEY=your_api_key
   ```

**使用与安全说明**：

- **无密钥模式**：即使不配置模型，上传、OCR、阅读、全文搜索、BM25 检索摘录、引用图谱与 CSV 绘图均可正常使用。
- **本地存储与权限**：界面保存的配置写入本地 `settings.json`（文件权限 `0600`），服务端接口永不向前端返回 API 密钥。
- **地址绑定与清除**：密钥绑定服务地址（含协议、主机、端口与路径）；更换服务地址时需重新填写密钥。点击“清除密钥”可恢复检索摘录模式，重启也不会重新启用 `.env` 中的旧密钥。
- **本地模型服务**：远程服务要求使用 `https://`；本机兼容服务（如 Ollama / vLLM / LM Studio）可使用 `http://127.0.0.1:端口/v1`（若服务端不校验密钥，填入任意占位符即可）。

---

## 备份与恢复

`scripts/backup.py` 支持在应用运行期间对工作区创建一致性 ZIP 快照（含 SQLite 在线快照、PDF 副本、会话、笔记版本、阅读位置与翻译缓存，**不包含模型设置与 API 密钥**），并附带逐文件 SHA-256 校验：

```bash
mkdir -p backups

# 1. 创建备份（会输出 archive_sha256，建议另行保存）
.venv/bin/python scripts/backup.py create --data data --output backups/workspace.zip

# 2. 校验备份完整性
.venv/bin/python scripts/backup.py verify backups/workspace.zip
# 可选整体包校验：--sha256 <archive_sha256>

# 3. 恢复到新的或空的目录（拒绝覆盖现有数据目录）
.venv/bin/python scripts/backup.py restore backups/workspace.zip --target restored-workspace
```

恢复时会自动重写文献路径并将未结束任务置为暂停状态，避免启动后自动触发模型请求。确认无误后，请先停止占用 8000 端口的实例，再使用恢复后的工作区启动：

```bash
RESEARCH_DATA_DIR="$PWD/restored-workspace" bash scripts/start.sh
```

---

## 项目结构

```text
zhixu-research-assistant/
├── backend/
│   ├── app.py                  # FastAPI 入口、静态资源托管与请求校验
│   ├── config.py               # 服务端模型配置与密钥隔离持久化
│   ├── documents.py            # PDF 解析、坐标提取、页面渲染与 OCR 调度
│   ├── storage.py              # SQLite 持久化、论文家族/版本、块/翻译/任务状态
│   ├── tasks.py                # 后台线程任务执行与页级检查点恢复
│   ├── intelligence.py         # BM25 索引检索、模型调用与 arXiv 接口
│   ├── answer_contract.py      # 逐条事实问答结构约束与降级摘录处理
│   ├── answer_validation.py    # 局部引用、数值/单位、引句与实验关系规则校验
│   ├── citations.py            # 参考文献匹配规则与引用图谱构建
│   ├── charts.py               # CSV 确定性统计与 Matplotlib 图表生成
│   ├── document_routes.py      # 检索、参考文献跳转、版本对比与导出路由
│   ├── translation_routes.py   # 整页/划词/句段对照翻译与缓存路由
│   ├── workspace_routes.py     # 会话、笔记版本、阅读位置与任务控制路由
│   ├── request_control.py      # 客户端断连取消上游请求控制
│   ├── workspace_lock.py       # 基于系统文件锁的单实例保护
│   └── backup.py               # 在线快照备份、校验与安全恢复
├── frontend/
│   └── src/                    # React + TypeScript + Vite 工作台前端
├── scripts/
│   ├── start.sh                # 一键安装依赖、构建前端并启动服务
│   ├── dev.sh                  # 前后端分离热重载开发模式
│   ├── backup.py               # 备份/校验/恢复命令行工具
│   ├── check.sh                # 本地完整回归与验收脚本
│   ├── e2e.sh                  # 隔离临时实例上的 Playwright 浏览器回归
│   ├── evaluate_retrieval.py   # 基于真实 PDF 集的离线 BM25 检索评测
│   ├── ocr_macos.swift         # macOS Apple Vision OCR 回退实现
│   ├── stress_import.py        # 批量导入与并发解析压力测试
│   ├── stress_runtime.py       # 混合 HTTP 负载长稳测试
│   ├── validate_install.py     # 干净环境安装与旧库升级验证
│   └── verify_model.py         # 可选真实模型接口预览与小样本校验脚本
├── docs/
│   └── API.md                  # 前后端接口与持久化契约文档
├── pdf/                        # 内置样例论文 PDF
└── tests/                      # 后端集成测试与检索评测基准数据
```

---

## 测试与检查

所有常规测试与检索评测均在无外部模型密钥模式下运行（不产生模型 API 费用）：

```bash
# 1. 仅运行单元测试与前端构建（不依赖 pdf/ 样例论文）
bash scripts/check.sh --unit-only

# 2. 默认检查：后端测试 + 前端单测 + 生产构建 + 真实 PDF 检索验收
bash scripts/check.sh

# 3. 完整回归：含四套检索评测集、批量导入压测与 Playwright 浏览器测试
# 首次运行需先安装测试浏览器：cd frontend && npx playwright install chromium
bash scripts/check.sh --all
```

常用专项工具命令：

```bash
# 后端测试
.venv/bin/python -m pytest tests backend/test_*.py -q

# 前端测试与构建
npm --prefix frontend test
npm --prefix frontend run build

# 离线检索评测（可加 --database data/research.sqlite3 只读复用已入库页面以加快速度）
.venv/bin/python scripts/evaluate_retrieval.py --output test-results/retrieval.json

# 运行包含完整扫描件 OCR 与三引擎浏览器（Chromium / Firefox / WebKit）的检查
(cd frontend && npx playwright install chromium firefox webkit)
RUN_OCR_TESTS=1 E2E_BROWSERS=chromium,firefox,webkit bash scripts/check.sh --all

# 混合 HTTP 负载测试（默认 600 秒）
.venv/bin/python scripts/stress_runtime.py --seconds 600

# 真实模型小样本验证（会向已配置服务发送请求并可能产生费用，建议先 --preview 检查）
.venv/bin/python scripts/verify_model.py --preview --output /tmp/model-preview.json
```

仓库亦提供 [.github/workflows/check.yml](.github/workflows/check.yml) 用于 GitHub Actions 自动化验证（包含应用测试、三引擎浏览器回归、长稳冒烟及语料检索评测）。

---

## 设计边界与注意事项

- **本地单用户定位**：默认监听 `127.0.0.1:8000`，面向个人本地研究场景，暂未提供多用户鉴权、租户隔离或公网部署方案。
- **检索与生成边界**：当前采用局部 BM25 检索与单次结构化模型调用，未接入向量数据库或自主多步 Agent。局部引用规则校验（`lexical_rules`）仅检查字面证据与数值/关系匹配，不构成语义等价证明。
- **解析与排版边界**：PyMuPDF 与 OCR 基线在处理复杂多行公式、跨页表格、极模糊扫描件时可能存在误差，界面始终保留原始页面图像供对照核查；句段对照翻译暂不提供原位中文 PDF 重排。
- **并发与存储**：当前使用 SQLite 与单 Uvicorn worker 线程任务执行器以简化本地部署；会话与笔记采用乐观锁（冲突返回 HTTP `409`），避免多窗口互相覆盖。
