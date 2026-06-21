import os
import logging
import re
import time
import subprocess
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from mcp.server.fastmcp import FastMCP
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

BEARER_TOKEN = os.getenv("BEARER_TOKEN")
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searxng.site")

# Global counters and startup time
search_count = 0
crawl_count = 0
start_time = time.time()

# 1. Initialize FastMCP
mcp = FastMCP("Search & Crawl Server")

# 2. Define SearXNG search tool
@mcp.tool()
async def search_web(query: str, engines: str = None, page: int = 1) -> str:
    """
    Search the web using SearXNG.
    Args:
        query: The search query.
        engines: Optional comma-separated list of engines to query (e.g. google, bing, duckduckgo, wikipedia).
        page: Page number for search results.
    """
    global search_count
    search_count += 1
    
    url = SEARXNG_URL.rstrip("/") + "/search"
    params = {
        "q": query,
        "format": "json",
        "pageno": page
    }
    if engines:
        params["engines"] = engines

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
            if response.status_code != 200:
                return f"Error: SearXNG returned status code {response.status_code}. Response: {response.text[:200]}"
            
            data = response.json()
            results = data.get("results", [])
            if not results:
                return "No search results found for the query."

            formatted_results = []
            for idx, r in enumerate(results[:10]):
                title = r.get("title", "No Title")
                link = r.get("url", "#")
                snippet = r.get("content", "No content description.")
                engine = r.get("engine", "Unknown")
                formatted_results.append(
                    f"{idx+1}. **[{title}]({link})**\n"
                    f"   *Source*: {engine}\n"
                    f"   *Snippet*: {snippet}\n"
                )
            return "\n".join(formatted_results)
    except Exception as e:
        return f"Error executing search query: {str(e)}"

# 3. Define Crawl tool with Crawl4AI + Fallback
async def fallback_crawl(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise Exception(f"HTTP status code {response.status_code}")
            
        soup = BeautifulSoup(response.text, "html.parser")
        for element in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav"]):
            element.decompose()
            
        body = soup.find("body") or soup
        markdown_text = md(str(body), heading_style="ATX").strip()
        markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
        return markdown_text

@mcp.tool()
async def crawl_page(url: str) -> str:
    """
    Crawls a web page and returns its content in clean Markdown format.
    Uses Crawl4AI (Chromium) as primary and falls back to HTTP parser if it fails.
    Args:
        url: The absolute URL of the web page to crawl.
    """
    global crawl_count
    crawl_count += 1
    
    try:
        logger.info(f"Crawling with Crawl4AI: {url}")
        browser_conf = BrowserConfig(
            headless=True,
            extra_args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--single-process"]
        )
        run_conf = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
        
        async with AsyncWebCrawler(config=browser_conf) as crawler:
            result = await crawler.arun(url=url, config=run_conf)
            if result.success and result.markdown:
                return result.markdown
            else:
                err_msg = result.error_message or "Unknown failure"
                logger.warning(f"Crawl4AI failed: {err_msg}. Running fallback parser...")
    except Exception as e:
        logger.warning(f"Crawl4AI exception: {str(e)}. Running fallback parser...")

    # Fallback execution
    try:
        logger.info(f"Executing fallback crawler: {url}")
        text = await fallback_crawl(url)
        return f"*(Fallback Parser Output)*\n\n{text}"
    except Exception as e:
        return f"Error crawling page '{url}': {str(e)}"

# Uptime text generator
def get_uptime() -> str:
    uptime_seconds = int(time.time() - start_time)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the MCP session manager starts/stops with the app
    async with mcp.session_manager.run():
        yield

# Create FastAPI app with lifespan manager
app = FastAPI(title="SearXNG Crawl MCP Server", lifespan=lifespan)

class TokenAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Exclude paths from authentication check
        if path in ["/", "/health", "/api/stats"] or path.startswith("/mcp/messages"):
            await self.app(scope, receive, send)
            return

        if BEARER_TOKEN:
            headers = dict(scope.get("headers", []))
            query_string = scope.get("query_string", b"").decode("utf-8")
            
            token = None
            
            # 1. Check Authorization header (HTTP headers in ASGI scope are lowercase bytes)
            auth_header_bytes = headers.get(b"authorization", b"")
            if auth_header_bytes:
                auth_header = auth_header_bytes.decode("utf-8")
                parts = auth_header.split()
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1]
                else:
                    token = auth_header
            
            # 2. Check token query parameter
            if not token and query_string:
                from urllib.parse import parse_qs
                params = parse_qs(query_string)
                token_list = params.get("token")
                if token_list:
                    token = token_list[0]

            if not token:
                await self.send_error(send, 401, "Missing Authorization Token")
                return

            if token != BEARER_TOKEN:
                await self.send_error(send, 403, "Invalid Bearer Token")
                return

        await self.app(scope, receive, send)

    async def send_error(self, send, status_code, message):
        import json
        response_body = json.dumps({"detail": message}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(response_body)).encode("utf-8")),
            ]
        })
        await send({
            "type": "http.response.body",
            "body": response_body,
        })

app.add_middleware(TokenAuthMiddleware)

# Mount native FastMCP SSE app with mount_path prefix
app.mount("/mcp", mcp.sse_app(mount_path="/mcp"))

@app.get("/health")
async def health_check():
    return {"status": "ok", "searxng_url": SEARXNG_URL, "auth_enabled": BEARER_TOKEN is not None}

# API Endpoint to fetch current stats
@app.get("/api/stats")
async def api_stats():
    return {
        "status": "online",
        "uptime": get_uptime(),
        "searxng_url": SEARXNG_URL,
        "auth_enabled": BEARER_TOKEN is not None,
        "stats": {
            "searches": search_count,
            "crawls": crawl_count
        }
    }

# Premium HTML Dashboard UI
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Dynamic host detection for config helper (checking X-Forwarded-Host from proxy)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "your-space-name.hf.space"))
    proto = request.headers.get("x-forwarded-proto", "https")
    sse_url = f"{proto}://{host}/mcp/sse"
    token_val = "YOUR_BEARER_TOKEN"
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>MCP 服务监控面板</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root {{
                --bg-primary: #0f172a;
                --bg-secondary: rgba(30, 41, 59, 0.7);
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --accent-color: #3b82f6;
                --success-color: #10b981;
                --warning-color: #f59e0b;
                --card-border: rgba(255, 255, 255, 0.08);
            }}

            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}

            body {{
                font-family: 'Inter', system-ui, -apple-system, sans-serif;
                background-color: var(--bg-primary);
                background-image: radial-gradient(circle at 10% 20%, rgba(59, 130, 246, 0.08) 0%, transparent 40%),
                                  radial-gradient(circle at 90% 80%, rgba(99, 102, 241, 0.08) 0%, transparent 40%);
                color: var(--text-main);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                padding: 2rem;
            }}

            header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 2rem;
                padding-bottom: 1rem;
                border-bottom: 1px solid var(--card-border);
            }}

            .logo-area {{
                display: flex;
                align-items: center;
                gap: 0.75rem;
            }}

            .logo-icon {{
                font-size: 1.8rem;
                color: var(--accent-color);
                text-shadow: 0 0 15px rgba(59, 130, 246, 0.5);
            }}

            h1 {{
                font-size: 1.5rem;
                font-weight: 700;
                letter-spacing: -0.025em;
            }}

            .status-badge {{
                display: flex;
                align-items: center;
                gap: 0.5rem;
                background: rgba(16, 185, 129, 0.15);
                border: 1px solid rgba(16, 185, 129, 0.3);
                padding: 0.4rem 0.8rem;
                border-radius: 9999px;
                font-size: 0.875rem;
                font-weight: 500;
                color: var(--success-color);
            }}

            .status-dot {{
                width: 8px;
                height: 8px;
                background-color: var(--success-color);
                border-radius: 50%;
                box-shadow: 0 0 10px var(--success-color);
                animation: pulse 1.8s infinite;
            }}

            @keyframes pulse {{
                0% {{ transform: scale(0.95); opacity: 0.5; }}
                50% {{ transform: scale(1.15); opacity: 1; }}
                100% {{ transform: scale(0.95); opacity: 0.5; }}
            }}

            main {{
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 2rem;
                max-width: 1200px;
                margin: 0 auto;
                width: 100%;
            }}

            .grid-stats {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 1.5rem;
            }}

            .card {{
                background: var(--bg-secondary);
                backdrop-filter: blur(12px);
                border: 1px solid var(--card-border);
                border-radius: 16px;
                padding: 1.5rem;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                position: relative;
                overflow: hidden;
                transition: transform 0.2s ease, border-color 0.2s ease;
            }}

            .card:hover {{
                transform: translateY(-2px);
                border-color: rgba(59, 130, 246, 0.25);
            }}

            .card-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 1rem;
            }}

            .card-title {{
                font-size: 0.875rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                color: var(--text-muted);
            }}

            .card-icon {{
                font-size: 1.25rem;
                color: var(--accent-color);
            }}

            .card-value {{
                font-size: 2.25rem;
                font-weight: 700;
                color: var(--text-main);
                margin: 0.5rem 0;
            }}

            .card-desc {{
                font-size: 0.825rem;
                color: var(--text-muted);
            }}

            .status-indicator {{
                display: inline-flex;
                align-items: center;
                gap: 0.35rem;
                font-weight: 600;
            }}

            .active-text {{ color: var(--success-color); }}
            .inactive-text {{ color: var(--warning-color); }}

            .card-details-list {{
                margin-top: 0.5rem;
                list-style: none;
                font-size: 0.85rem;
                color: var(--text-muted);
            }}

            .card-details-list li {{
                display: flex;
                justify-content: space-between;
                padding: 0.4rem 0;
                border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            }}

            .card-details-list li:last-child {{
                border-bottom: none;
            }}

            .config-section {{
                background: var(--bg-secondary);
                backdrop-filter: blur(12px);
                border: 1px solid var(--card-border);
                border-radius: 16px;
                padding: 1.5rem;
            }}

            .config-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 1.2rem;
            }}

            .config-title {{
                font-size: 1.1rem;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }}

            .copy-btn {{
                background: rgba(59, 130, 246, 0.15);
                border: 1px solid rgba(59, 130, 246, 0.3);
                color: var(--accent-color);
                padding: 0.4rem 0.8rem;
                border-radius: 6px;
                cursor: pointer;
                font-size: 0.85rem;
                font-weight: 500;
                display: flex;
                align-items: center;
                gap: 0.4rem;
                transition: background 0.2s;
            }}

            .copy-btn:hover {{
                background: rgba(59, 130, 246, 0.3);
            }}

            .tab-btn {{
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--card-border);
                color: var(--text-muted);
                padding: 0.4rem 0.8rem;
                border-radius: 6px;
                cursor: pointer;
                font-size: 0.85rem;
                font-weight: 500;
                transition: all 0.2s;
            }}

            .tab-btn:hover {{
                background: rgba(255, 255, 255, 0.1);
                color: var(--text-main);
            }}

            .tab-btn.active {{
                background: rgba(59, 130, 246, 0.2);
                border-color: rgba(59, 130, 246, 0.4);
                color: var(--accent-color);
            }}

            pre {{
                background: rgba(15, 23, 42, 0.8);
                padding: 1rem;
                border-radius: 8px;
                border: 1px solid rgba(255, 255, 255, 0.04);
                overflow-x: auto;
                font-family: 'Courier New', Courier, monospace;
                font-size: 0.875rem;
                color: #38bdf8;
                line-height: 1.5;
            }}

            footer {{
                text-align: center;
                margin-top: 3rem;
                font-size: 0.8rem;
                color: var(--text-muted);
            }}

            footer a {{
                color: var(--accent-color);
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <header>
            <div class="logo-area">
                <i class="fa-solid fa-server logo-icon"></i>
                <h1>MCP 服务监控面板</h1>
            </div>
            <div class="status-badge">
                <span class="status-dot"></span>
                <span>服务正常运行中</span>
            </div>
        </header>

        <main>
            <div class="grid-stats">
                <!-- System Status Card -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">系统运行状态</span>
                        <i class="fa-solid fa-gauge-high card-icon"></i>
                    </div>
                    <div>
                        <div class="card-value" id="uptime">加载中...</div>
                        <div class="card-desc">自本次启动以来的运行时间</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>密钥鉴权</span>
                            <span class="status-indicator" id="auth-status">...</span>
                        </li>
                    </ul>
                </div>

                <!-- SearXNG status card -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">SearXNG 搜索引擎</span>
                        <i class="fa-solid fa-magnifying-glass card-icon"></i>
                    </div>
                    <div>
                        <div class="card-value" id="search-count">0</div>
                        <div class="card-desc">本次会话累计执行的搜索次数</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>接口地址</span>
                            <span id="searxng-url" style="word-break: break-all; max-width: 170px; text-align: right;">加载中...</span>
                        </li>
                    </ul>
                </div>

                <!-- Crawl4AI status card -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">Crawl4AI 网页爬虫</span>
                        <i class="fa-solid fa-spider card-icon"></i>
                    </div>
                    <div>
                        <div class="card-value" id="crawl-count">0</div>
                        <div class="card-desc">本次会话累计执行的爬取次数</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>爬虫引擎</span>
                            <span class="active-text">Playwright (Chromium)</span>
                        </li>
                    </ul>
                </div>
            </div>

            <!-- Client Config block -->
            <div class="config-section">
                <div class="config-header">
                    <span class="config-title"><i class="fa-solid fa-cog"></i> Claude Desktop 客户端配置助手</span>
                    <div style="display: flex; gap: 0.5rem;">
                        <button class="tab-btn active" onclick="showTab('remote')">远程 SSE 桥接</button>
                        <button class="tab-btn" onclick="showTab('local')">本地 Stdio 命令行</button>
                    </div>
                </div>
                <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.8rem;" id="config-desc">
                    推荐！使用 npx 桥接工具 mcp-remote 访问部署在 Hugging Face Spaces 上的远程服务：
                </p>
                <div style="position: relative;">
                    <pre id="json-config" style="padding-top: 2.5rem; min-height: 180px;"></pre>
                    <button class="copy-btn" onclick="copyConfig()" style="position: absolute; right: 10px; top: 10px;"><i class="fa-solid fa-copy"></i> 复制配置</button>
                </div>
            </div>
        </main>

        <footer>
            <p>Model Context Protocol Server | 运行于 Hugging Face Spaces | Powered by <a href="https://github.com/khc8655/xng" target="_blank">khc8655/xng</a></p>
        </footer>

        <script>
            // Config Templates
            const remoteConfig = `{{
  "mcpServers": {{
    "hf-search-crawl-mcp": {{
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "{sse_url}?token={token_val}"
      ]
    }}
  }}
}}}}`;

            const localConfig = `{{
  "mcpServers": {{
    "local-search-crawl-mcp": {{
      "command": "/Users/xk/Documents/mcp/.venv/bin/python3",
      "args": [
        "/Users/xk/Documents/mcp/app.py"
      ],
      "env": {{
        "BEARER_TOKEN": "{token_val}",
        "SEARXNG_URL": "https://searxng.site"
      }}
    }}
  }}
}}}}`;

            let currentTab = 'remote';

            function updateConfigDisplay() {{
                const configPre = document.getElementById('json-config');
                const desc = document.getElementById('config-desc');
                
                if (currentTab === 'remote') {{
                    configPre.textContent = remoteConfig;
                    desc.innerHTML = '使用 <code>mcp-remote</code> 桥接工具连接部署在 Hugging Face 的远程服务器（无需本地配置 Python 环境）：';
                }} else {{
                    configPre.textContent = localConfig;
                    desc.innerHTML = '在您本地的 macOS 电脑上直接使用虚拟环境中的 Python 执行（极速、低延迟、纯本地运行）：';
                }}
            }}

            function showTab(tab) {{
                currentTab = tab;
                document.querySelectorAll('.tab-btn').forEach(btn => {{
                    btn.classList.remove('active');
                }});
                
                // Highlight active button
                const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => {{
                    if (tab === 'remote') return b.textContent.includes('远程');
                    return b.textContent.includes('本地');
                }});
                if (btn) btn.classList.add('active');
                
                updateConfigDisplay();
            }}

            async function fetchStats() {{
                try {{
                    const res = await fetch('/api/stats');
                    if (!res.ok) return;
                    const data = await res.json();
                    
                    // Update DOM
                    document.getElementById('uptime').textContent = data.uptime;
                    document.getElementById('search-count').textContent = data.stats.searches;
                    document.getElementById('crawl-count').textContent = data.stats.crawls;
                    document.getElementById('searxng-url').textContent = data.searxng_url;
                    
                    // Auth indicator
                    const authIndicator = document.getElementById('auth-status');
                    if (data.auth_enabled) {{
                        authIndicator.innerHTML = '<span class="active-text"><i class="fa-solid fa-shield-halved"></i> 已启用</span>';
                    }} else {{
                        authIndicator.innerHTML = '<span class="inactive-text"><i class="fa-solid fa-triangle-exclamation"></i> 未启用</span>';
                    }}
                    
                }} catch (e) {{
                    console.error("Failed fetching statistics", e);
                }}
            }}

            function copyConfig() {{
                const configText = document.getElementById('json-config').textContent;
                navigator.clipboard.writeText(configText).then(() => {{
                    const btn = document.querySelector('.copy-btn');
                    const origHtml = btn.innerHTML;
                    btn.innerHTML = '<i class="fa-solid fa-check"></i> 已复制！';
                    setTimeout(() => {{
                        btn.innerHTML = origHtml;
                    }}, 2000);
                }}).catch(err => {{
                    alert("复制失败: " + err);
                }});
            }}

            // Initial display and poll every 3 seconds
            updateConfigDisplay();
            fetchStats();
            setInterval(fetchStats, 3000);
        </script>
    </body>
    </html>
    """
    return html_content

if __name__ == "__main__":
    mcp.run()
