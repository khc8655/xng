---
title: search
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🔍 Multi-Engine Search & Web Crawl MCP Server (Tailored for OpenClaw & Hermes)

这是一个基于 **Model Context Protocol (MCP)** 标准的高性能、高可用 Web 搜索与网页爬取服务器。它专为 **OpenClaw**、**Hermes**、**Cursor**、**Antigravity** 等 LLM Agents 设计，提供极度稳定、极低延迟、极省 Token 的互联网检索与网页提取能力。

---

## 🌟 核心设计与技术架构

```
                              ┌────────────────┐
                              │  Client Agent  │ (OpenClaw / Hermes)
                              └───────┬────────┘
                                      │ (Bearer Token / Streamable HTTP & SSE)
                                      ▼
                           ┌──────────────────────┐
                           │ SimpleAuthMiddleware │ (CORS 204 / HEAD 200 / Auth)
                           └──────────┬───────────┘
                                      │
           ┌──────────────────────────┼──────────────────────────┐
           ▼                          ▼                          ▼
  🛠️ search_web(query)       🛠️ crawl_page(url)         🛠️ crawl_site(url)
 (SearXNG 聚合打分/去重)    (单页动态渲染/Markdown)     (全站文档 BFS 递归)
           │                          │                          │
 ┌─────────┴─────────┐      ┌─────────┴─────────┐      ┌─────────┴─────────┐
 ▼                   ▼      ▼                   ▼      ▼                   ▼
SearXNG 聚合打分   DDG免Key Crawl4AI           Heuristic Crawl4AI           BFS 深度控制
(Tavily/火山/Exa) (保底)  (Chromium 动态)    (静态降级)  (文档并发)          (Max 10 页)
```

### 1. 🛠️ 三大精简核心工具 (Exposed MCP Tools)

| 工具名称 | 输入参数 | 业务逻辑与核心价值 |
| :--- | :--- | :--- |
| **`search_web`** | `query`: 检索词<br>`page`: 分页页码 (默认 1) | **SearXNG 式工业级聚合搜索网关**。多源并发拉取、隐式 GitHub/知乎数据源识别、SearXNG 倒数排名打分、URL 去重与 150 字摘要截断。 |
| **`crawl_page`** | `url`: 目标网页地址 | **单网页深度渲染与提取**。支持 Crawl4AI 无头 Chromium 动态渲染（`text_mode=True` 与 `light_mode=True` 省 70% 内存），抓取高质量 Markdown；自动降级至 Heuristic/云 API。 |
| **`crawl_site`** | `url`: 种子链接<br>`max_depth`: 最大深度 (默认2)<br>`max_pages`: 页面上限 (默认10) | **教程与文档站点全站爬虫**。基于宽度优先（BFS）递归爬取开源 Wiki 和教程站点，带并发限流（Semaphore=3）与 300ms 礼貌延迟，防止 LLM 上下文爆炸。 |

---

### 2. 🧮 工业级 SearXNG 倒数排名打分与去重 (SearXNG Reciprocal Rank Scoring)

`search_web` 借鉴了业界最成熟的开源聚合搜索引擎 **SearXNG** 的打分算法：

1. **多源并发分发 (Parallel Fan-Out)**：收到 Query 后并发调度 Tavily、火山引擎、Exa 及 GitHub/知乎数据源，延迟由最快的引擎决定。
2. **URL 归一化去重**：自动规范化 URL，同链接合并标注多来源（如 `Source: Tavily, Volcengine`），并保留最丰富的一条 Snippet。
3. **SearXNG 倒数排名打分公式**：
   $$\text{Score} = \sum_{i} \frac{\text{Occurrences} \times \text{Weight}_{engine_i}}{\text{Position}_i}$$
   被越多的权威搜索引擎共同命中、且原始排名越靠前的网页，得分越高，自动置顶。
4. **LLM 专属 Token 截断**：按 Score 倒序提取 Top 10，摘要单条限制在 150 字内。

---

### 3. 🌐 100% 协议与跨域兼容性 (CORS & Transport Handling)

内置的 ASGI 中间件 (`SimpleAuthMiddleware`) 彻底消除了各类 Agent 连接不稳定问题：
* **CORS 预检 (OPTIONS)**：无条件响应 `204 No Content` 并带全量 CORS 标头，支持跨域与 Web 网页端 Agent。
* **HEAD 健康打点**：对 `/mcp` 的 `HEAD` 探针直接响应 `200 OK`，消除 Starlette 307 重定向死循环。
* **Accept 请求头规范化**：自动补充 `Accept` 请求头，消除了某些 Client 导致的 `406 Not Acceptable` 拒连。
* **双传输协议支持**：同时 100% 完美支持 **SSE 模式 (`/mcp/sse`)** 与 **非 SSE / Streamable HTTP 模式 (`POST /mcp`)**。

---

## ⚙️ 配置与环境变量

### 1. 安全与鉴权
| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `BEARER_TOKEN` | `None` | 自定义安全 Token。若设置，客户端建立连接时必须提供对应的 Bearer 认证或查询参数 `?token=...` |

### 2. 搜索引擎 API 秘钥
| 变量名 | 说明 | 获取渠道 |
| :--- | :--- | :--- |
| `TAVILY_API_KEY` | Tavily 搜索引擎 Key | [Tavily 官网](https://tavily.com/) |
| `EXA_API_KEY` | Exa.ai 语义搜索引擎 Key | [Exa 官网](https://exa.ai/) |
| `VOLC_SEARCH_API_KEY` | 火山引擎（抖音/字节）定制搜索引擎 Key | [火山引擎控制台](https://www.volcengine.com/) |

### 3. 网页抓取 API 秘钥 (云端越盾降级)
| 变量名 | 说明 | 获取渠道 |
| :--- | :--- | :--- |
| `FIRECRAWL_API_KEY` | Firecrawl 网页爬取与转 Markdown API Key | [Firecrawl 官网](https://www.firecrawl.dev/) |
| `SCRAPFLY_API_KEY` | Scrapfly 住宅代理越盾爬虫 API Key | [Scrapfly 官网](https://scrapfly.io/) |
| `DISABLE_LOCAL_BROWSER` | 设置为 `true` 或 `1` 时，强制停用本地 Crawl4AI 动态浏览器，全面启用轻量级启发式静态抓取（适合低配服务器）。 | 低配机器优化 |

---

## 🚀 部署与使用

### Hugging Face Spaces 部署 (推荐 ⭐)
1. 将项目连接并推送到你的 Hugging Face Space (SDK 选择 Docker)。
2. 空间 Visibility 设为 **Public**。
3. 配置环境变量 `BEARER_TOKEN` 及相关 API Key。
4. 客户端 Agent 连接地址：
   - **Streamable HTTP (推荐)**: `https://<你的子域名>.hf.space/mcp?token=<YOUR_TOKEN>`
   - **SSE 模式**: `https://<你的子域名>.hf.space/mcp/sse?token=<YOUR_TOKEN>`
