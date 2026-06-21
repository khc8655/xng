import os
import logging
import re
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

BEARER_TOKEN = os.getenv("BEARER_TOKEN")
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://searxng.site")

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

# 3. Define Crawl tool (using lightweight HTTP client + markdown converter)
async def fetch_and_convert_to_markdown(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise Exception(f"HTTP status code {response.status_code}")
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Remove elements that are noise
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
    Args:
        url: The absolute URL of the web page to crawl.
    """
    try:
        logger.info(f"Crawling page: {url}")
        text = await fetch_and_convert_to_markdown(url)
        if not text:
            return "Webpage parsed, but no content was extracted."
        return text
    except Exception as e:
        return f"Error crawling page '{url}': {str(e)}"

# 4. Setup SSE Transport
transport = SseServerTransport("/messages/")

async def handle_sse(request: Request):
    async with transport.connect_sse(request.scope, request.receive, request._send) as (in_stream, out_stream):
        await mcp._mcp_server.run(in_stream, out_stream, mcp._mcp_server.create_initialization_options())
    return Response()

# Create FastAPI app
app = FastAPI(title="SearXNG Crawl MCP Server")

# Mount transport handlers
app.add_route("/sse", handle_sse, methods=["GET"])
app.mount("/messages/", app=transport.handle_post_message)

@app.middleware("http")
async def verify_bearer_token(request: Request, call_next):
    # Exclude health check from token check
    if request.url.path == "/health":
        return await call_next(request)
        
    if BEARER_TOKEN:
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(status_code=401, content={"detail": "Missing Authorization Header"})
            
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(status_code=401, content={"detail": "Invalid Authorization Header Format"})
            
        token = parts[1]
        if token != BEARER_TOKEN:
            return JSONResponse(status_code=403, content={"detail": "Invalid Bearer Token"})
            
    return await call_next(request)

@app.get("/health")
async def health_check():
    return {"status": "ok", "searxng_url": SEARXNG_URL, "auth_enabled": BEARER_TOKEN is not None}
