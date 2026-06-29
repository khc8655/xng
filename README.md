---
title: search
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🔍 Multi-Engine Search & Web Crawl MCP Server

这是一个基于 **Model Context Protocol (MCP)** 标准的高性能、高可用 Web 搜索与网页爬取服务器。它专为大语言模型（LLM）和智能体（AI Agents）设计，提供实时的互联网信息获取能力、章节级文档并发爬取、智能 RAG 语义过滤、以及针对中文优化的分词匹配。

---

## 🌟 核心设计与技术架构

服务器的核心入口和逻辑集中在 `app.py` 中。整体采用无中心化、分层降级的架构设计：

```
                              ┌────────────────┐
                              │  Client Agent  │
                              └───────┬────────┘
                                      │ (Bearer Token / SSE Token)
                                      ▼
                           ┌──────────────────────┐
                           │ TokenAuthMiddleware  │
                           └──────────┬───────────┘
                                      │
                                      ▼
                           ┌──────────────────────┐
                           │   FastMCP Routing    │
                           └────┬────────────┬────┘
                                │            │
         ┌──────────────────────┘            └──────────────────────┐
         ▼                                                          ▼
  ┌──────────────┐                                           ┌──────────────┐
  │  search_web  │ (Unified Gateway)                         │  crawl_page  │ / crawl_site (BFS)
  └──────┬───────┘                                           └──────┬───────┘
         │                                                          │
   ┌─────┼──────────────┐                                    ┌──────┼──────────────┐
   ▼     ▼              ▼                                    ▼      ▼              ▼
Tavily  Exa  DuckDuckGo (Free)                            Heuristic Crawl4AI   Firecrawl/Scrapfly
(Key)  (Key)   (HTML Parser)                            (BeautifulSoup) (Local Chrome) (Cloud Bypass)
```

### 1. 统一搜索路由聚合器 (`search_web`)
- **自动分流**：根据 Query 的语种自动路由。中文查询自动路由至火山引擎定制搜索（首选，对 CSDN、知乎、掘金支持极佳）与 Tavily 并行检索；英文查询自动路由至 Tavily / Exa。
- **免 Key 降级**：若未配置任何 Search API 秘钥，自动降级至本地免秘钥的 DuckDuckGo HTML 解析引擎，保障基础搜索可用性。
- **知乎深度检索**：支持 `engines="zhihu"` 专属搜索，或 `engines="hybrid"` 混合搜索模式，并发拉取知乎的深度讨论与专业问答，并在输出中展示点赞数和评论数。

### 2. 智能分层网页抓取器 (`crawl_page` / `crawl_site`)
- **第一层：本地 Heuristic Readability (零内存开销)**
  通过标准 HTTP 客户端拉取静态 HTML，使用类似 Readability.js 的评分算法，剥离导航栏、页脚、侧边栏和广告，提取核心文章内容并转为 Markdown。
- **第二层：本地 Crawl4AI 动态渲染 (中内存开销)**
  如果静态抓取失败、被防爬盾拦截或网页为单页应用（SPA），自动级联至本地 Crawl4AI。它以 `text_mode=True`（不加载图片、字体和样式）和 `light_mode=True` 启动无头 Chromium 浏览器，执行 JS 渲染，节省 70% 内存和带宽。
- **第三层：Firecrawl / Scrapfly 云端越盾 (高可靠付费降级)**
  如果本地渲染仍被 Cloudflare、CAPTCHA 等深度反爬盾阻断，自动路由至 Firecrawl Scrape API，最终降级至 Scrapfly 住宅代理云端浏览器，确保 100% 网页可达性。
- **章节递归爬虫 (`crawl_site`)**
  支持对整个文档章节或 wiki 进行宽度优先（BFS）递归爬取。内置 **`max_depth` 深度控制（默认 2）**、**`max_pages` 页面控制（默认 10）**、**链接 Prefix 域名锁定**，并加入了并发限制（批次大小 3）与 300ms 礼貌延迟，防止因递归抓取导致整个网站被封禁或 LLM 上下文爆炸。

### 3. 高性能内存缓存 (`TTLCache`)
- 包含 10 分钟 TTL 过期策略。
- 引入 **URL 标准化** 机制（忽略尾部斜杠，对 Query 参数按字母排序），极大提升缓存命中率。
- 带有 **LRU（最近最少使用）淘汰机制** 及最大容量上限（Search 300 条，Crawl 150 条），防止多用户并发使用导致内存泄露。

---

## 📂 项目目录结构

```
.
├── app.py                       # 核心入口文件（包含所有 MCP 接口、Web 控制台和业务逻辑）
├── Dockerfile                   # 用于 Hugging Face Spaces 和 Docker 部署的容器定义
├── start.sh                     # 容器启动脚本（处理 Playwright 依赖检查和后台预初始化）
├── wasmer.toml                  # Wasmer Edge 部署配置文件
├── app.yaml                     # Wasmer Edge 路由描述文件
├── requirements.txt             # 基础依赖依赖（WASM 版本不包含 Crawl4AI 等大体积包）
├── requirements-hf.txt          # 满血版依赖（包含 Crawl4AI, Playwright, lxml）
├── .gitignore                   # Git 忽略文件
├── .wasmerignore                # Wasmer 上传包忽略文件
├── wasmer/
│   └── site-packages/           # 本地预编译的 WASIX WebAssembly Python 库（用于 Edge 部署）
└── scratch/                     # 本地测试工具与测试套件目录
    ├── run_manual_tests.py      # 测试运行器（一次性运行所有本地集成测试）
    ├── test_pipeline_local.py   # Heuristic -> Crawl4AI 级联抓取流水线测试
    ├── test_crawling_heuristics.py # Readability 抓取与封锁检测测试
    ├── test_zhihu_local.py      # 混合检索与知乎 API Mock 测试
    └── cf_snippet.js            # 备用：Cloudflare Worker 边缘双流网关路由脚本
```

---

## ⚙️ 配置与环境变量

你可以在部署容器或本地启动时，通过环境变量配置服务器的行为：

### 1. 安全与鉴权
| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `BEARER_TOKEN` | `None` | 自定义安全 Token。若设置，客户端建立连接时必须提供对应的 Bearer 认证或查询参数 `?token=...` |

### 2. 搜索引擎配置
| 变量名 | 说明 | 获取渠道 |
| :--- | :--- | :--- |
| `TAVILY_API_KEY` | Tavily 搜索引擎 Key | [Tavily 官网](https://tavily.com/) |
| `EXA_API_KEY` | Exa.ai 语义搜索引擎 Key | [Exa 官网](https://exa.ai/) |
| `VOLC_SEARCH_API_KEY` | 火山引擎（抖音/字节）定制搜索引擎 Key | [火山引擎控制台](https://www.volcengine.com/) |
| `ZHIHU_ACCESS_SECRET` | 知乎内容检索 OpenAPI Key | 内部开发者授权申请 |
| `SEARXNG_URL` | SearXNG 私有部署/公共网关地址 (可选备份) | 自建或使用公共节点 |

### 3. 网页抓取配置
| 变量名 | 说明 | 获取渠道 |
| :--- | :--- | :--- |
| `FIRECRAWL_API_KEY` | Firecrawl 网页爬取与转 Markdown API Key | [Firecrawl 官网](https://www.firecrawl.dev/) |
| `SCRAPFLY_API_KEY` | Scrapfly 住宅代理越盾爬虫 API Key | [Scrapfly 官网](https://scrapfly.io/) |
| `DISABLE_LOCAL_BROWSER` | 设置为 `true` 时，强制停用本地 Crawl4AI 动态浏览器（转为纯轻量抓取）| 本地环境调试或低配机 |

---

## 🚀 三大安装部署方式

### 方案 A：Hugging Face Spaces 部署（最推荐，持久免维护）

本项目原生支持 Hugging Face Spaces Docker 部署，在多端 AI Agents 中可作为持久的云端 MCP 服务。

1. 在 Hugging Face 上创建一个 **New Space**。
2. SDK 选择 **Docker**，模板选择 **Blank**（非 Gradio/Streamlit）。
3. 关联你的 Git 仓库，或者添加 Hugging Face 为远程仓库推送到 Space：
   ```bash
   git remote add hf https://huggingface.co/spaces/<你的用户名>/<Space名称>
   git push hf main --force
   ```
4. 在 Space 页面点击 **Settings** -> **Variables and secrets**，配置上述环境变量（如 `BEARER_TOKEN`, `TAVILY_API_KEY` 等）。
5. 容器将自动构建并上线。为了防止免费 Space 在 48 小时无请求后休眠，请在工作流或外部监控（如 UptimeRobot）中，每 10 分钟请求一次 `/health` 接口。

### 方案 B：DCD (Docker Compose Deployment) 轻量化本地/私有云部署

适合在自建服务器上跑私有化轻量服务。

1. 在项目根目录，通过 Docker 编译镜像：
   ```bash
   docker build -t mcp-search-crawl:latest .
   ```
2. 使用以下命令运行容器：
   ```bash
   docker run -d \
     -p 7860:7860 \
     -e BEARER_TOKEN="your_secure_token" \
     -e TAVILY_API_KEY="your_tavily_key" \
     --name mcp-server \
     mcp-search-crawl:latest
   ```

### 方案 C：Wasmer Edge 部署 (Serverless WebAssembly)

得益于 Wasm 优良的沙箱与冷启动特性，本程序已深度适配 Wasmer WASIX 运行时。

1. 本地完成 WASIX wheels 的安装和 site-packages 预编译（工程中已包含）。
2. 在本地执行部署命令：
   ```bash
   wasmer deploy
   ```
3. 服务会自动打包并发布至 Wasmer Edge 平台。运行环境为 pure python (Wasm 架构)，不包含动态 Chrome 依赖，自动以 Tier 1 (Heuristics) + Tier 3 (Scrapfly/Firecrawl Cloud) 模式无缝运转，完全实现 scale-to-zero（零使用零计费）。

---

## 🛠️ 本地开发与测试指南

如果你需要在此项目上继续迭代功能，请按照以下步骤配置你的开发环境：

### 1. 初始化虚拟环境
建议使用 Python 3.11 版本进行本地调试：
```bash
# 创建虚拟环境
python3.11 -m venv .venv311

# 激活虚拟环境
source .venv311/bin/activate

# 安装所有开发和满血版依赖
pip install -r requirements-hf.txt

# 安装 Playwright 的 Chromium 依赖
playwright install chromium
```

### 2. 本地启动服务
在终端配置环境变量后，运行：
```bash
export BEARER_TOKEN="test_token"
export TAVILY_API_KEY="your_key"
python3 app.py
```
默认服务会监听 `0.0.0.0:7860`。
- **健康检查**: `http://localhost:7860/health`
- **实时统计面板**: `http://localhost:7860/` (可在浏览器直观看到配置成功的引擎为绿色，以及累积的搜索爬取统计)

### 3. 运行自动化测试套件
在提交代码之前，请**务必**执行本地集成测试，以确保级联降级和分词匹配依然完好：
```bash
python3 scratch/run_manual_tests.py
```
如果看到 `🎉 ALL MODULE TESTS PASSED SUCCESSFULLY!` 报告卡，说明代码逻辑完全健康，可以安全部署。

---

## 🤝 开发者交接与二次开发提示

当其他开发者接手此项目时，以下细节能帮助他快速干活：

1. **核心逻辑定位**：
   - MCP 协议定义、接口注册：`app.py` 底部。所有的 MCP Tool 都带有 `@mcp.tool()` 装饰器。
   - `search_web` 的分流与聚合：位于 `app.py` 中部的 `search_web` 函数和 `run_general_web_search` 函数。
   - `crawl_page` 与 `crawl_site` 的抓取级联：位于 `app.py` 的 `fetch_page_content` 及其降级链条中。

2. **添加新的搜索引擎/抓取工具**：
   - 在 `app.py` 中仿照 `search_tavily` 写一个异步的 `search_xxx` 函数，注意捕获所有的 HTTP 异常。
   - 将其集成进 `run_general_web_search` 或 `search_web` 函数中，并在 `get_active_engines()` 增加状态探测。
   - 修改仪表盘 HTML 部分，在控制台增加对应组件的绿色点亮逻辑。

3. **测试防爆逻辑**：
   - 所有的环境变量（如 `ZHIHU_ACCESS_SECRET` 等）在 `app.py` 头部通过 `os.getenv` 读入并缓存到了全局变量。如果编写测试脚本 mock 环境，**不要**在 import 之后直接修改 `os.environ`（这样无效），而是应该像 `scratch/test_zhihu_local.py` 中那样，直接对 `app.ZHIHU_SECRET` 模块级变量进行修改。
