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
                                ├────────────┤
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
├── Dockerfile                   # 生产多阶段构建 Docker 镜像定义（已做大小与编译期优化）
├── start.sh                     # 容器启动脚本
├── wasmer.toml                  # Wasmer Edge 部署配置文件
├── app.yaml                     # Wasmer Edge 路由描述文件
├── requirements.txt             # 基础依赖（不含动态浏览器等大体积包）
├── requirements-hf.txt          # 满血版依赖（包含 Crawl4AI, Playwright, lxml）
├── .gitignore                   # Git 忽略配置
├── .wasmerignore                # Wasmer 忽略配置
├── wasmer/
│   └── site-packages/           # 本地预编译的 WASIX WebAssembly Python 库
├── edgeone-mcp/                 # 腾讯 EdgeOne Makers 独立部署包目录
└── scratch/                     # 本地测试与网关辅助脚本目录（被 Git 忽略）
    ├── test_mcp_client_real.py  # 官方 Python MCP SDK SSE 客户端集成测试
    ├── test_custom_domain_direct.py # 针对自定义域名边缘网关的端到端测试
    └── cf_snippet.js            # 最新版 Cloudflare Worker 双向 Token 翻译与容灾网关脚本
```

---

## ⚙️ 配置与环境变量

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

### 3. 网页抓取配置
| 变量名 | 说明 | 获取渠道 |
| :--- | :--- | :--- |
| `FIRECRAWL_API_KEY` | Firecrawl 网页爬取与转 Markdown API Key | [Firecrawl 官网](https://www.firecrawl.dev/) |
| `SCRAPFLY_API_KEY` | Scrapfly 住宅代理越盾爬虫 API Key | [Scrapfly 官网](https://scrapfly.io/) |
| `DISABLE_LOCAL_BROWSER` | 设置为 `true` 时，强制停用本地 Crawl4AI 动态浏览器 | 低配机器优化 |

---

## 🚀 五大安装部署方式

### 方案 A：Hugging Face Spaces 部署（公开空间最佳实践，推荐 ⭐）

本项目原生支持 Hugging Face Spaces Docker 部署，在多端 AI Agents 中可作为持久的云端 MCP 服务。

1. 在 Hugging Face 上创建一个 **New Space**，SDK 选择 **Docker**，模板选择 **Blank**。
2. 将该 Space 的 **Visibility 设为 Public**（为了方便 GitHub 保活监控以及规避私有状态下 HF 网关对 `/mcp/sse` 请求的 500/503 拦截）。
3. 关联你的 Git 仓库，或者推送到 Space：
   ```bash
   git remote add hf https://huggingface.co/spaces/<你的用户名>/<Space名称>
   git push hf main --force
   ```
4. 在 Space 页面点击 **Settings** -> **Variables and secrets**，配置环境变量（如 `BEARER_TOKEN`, `TAVILY_API_KEY` 等）。
5. 容器将自动使用多阶段构建（仅保留编译后的依赖，剔除了 `build-essential` gcc 等，镜像更轻），并分配正确的 `user` 家目录所有权以防 Crawl4AI 报错。

### 方案 B：DCD (Docker Compose Deployment) 轻量化本地/私有云部署

适合在自建服务器上跑私有化服务。
1. 通过 Docker 编译镜像：
   ```bash
   docker build -t mcp-search-crawl:latest .
   ```
2. 运行容器：
   ```bash
   docker run -d \
     -p 7860:7860 \
     -e BEARER_TOKEN="your_secure_token" \
     -e TAVILY_API_KEY="your_tavily_key" \
     --name mcp-server \
     mcp-search-crawl:latest
   ```

### 方案 C：Cloudflare Workers / Snippets 边缘代理网关（双向 Token 翻译与自动容灾）

如果你希望拥有一个固定的自定义域名并拥有自动故障转移（主服务器宕机自动路由到备用服务器），请在 Cloudflare 中使用此方案：

1. **CF 匹配规则配置**：
   在 Cloudflare Rules / Snippets 中，将匹配表达式配置为整个子目录：
   `(http.host eq "s.khc6.eu.cc")` 或 `(http.host eq "s.khc6.eu.cc" and starts_with(http.request.uri.path, "/mcp/"))`。
2. **边缘代理代码**：
   将 `scratch/cf_snippet.js` 的完整内容发布至 Cloudflare。该代码实现了 **双向 Token 翻译适配器**：
   - 客户端使用老的 `KangHong...` 密钥访问时，若流量打向 Hugging Face（使用新 Token），边缘网关会自动将其翻译成新 Token 顺利通过验证；若降级到备用服务器，则保持老 Token 转发。这实现了**所有老 Agent 零配置修改无缝兼容使用**。

### 方案 D：Wasmer Edge 部署 (Serverless WebAssembly)

得益于 Wasm 优良的沙箱与冷启动特性，本程序已深度适配 Wasmer WASIX 运行时。
1. 本地执行部署命令：
   ```bash
   wasmer deploy
   ```
2. 运行环境为 pure python (Wasm 架构)，不包含动态 Chrome 依赖，自动以 Tier 1 (Heuristics) + Tier 3 (Scrapfly/Firecrawl Cloud) 模式运行。

### 方案 E：Tencent EdgeOne Makers 部署（极速 Serverless & AI 总结内置）

1. 进入 `edgeone-mcp/` 目录，执行项目关联命令：
   ```bash
   cd edgeone-mcp/ && edgeone makers link
   ```
2. 本地开发调试：
   ```bash
   edgeone makers dev
   ```
3. 部署发布：
   ```bash
   edgeone makers deploy
   ```

---

## 🛠️ 本地开发与测试指南

### 1. 初始化虚拟环境
建议使用 Python 3.11 版本进行本地调试：
```bash
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements-hf.txt
playwright install chromium
```

### 2. 运行自动化测试套件
在提交代码之前，请**务必**执行本地集成测试，以确保级联降级和分词匹配依然完好：
```bash
python3 scratch/run_manual_tests.py
```
如果看到 `🎉 ALL MODULE TESTS PASSED SUCCESSFULLY!`，说明代码逻辑完全健康。
