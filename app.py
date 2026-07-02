import os
import asyncio
import logging
import re
import time
import json
import subprocess
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response, HTMLResponse
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")
# Suppress noisy MCP protocol handling log output (PingRequest/ListToolsRequest)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

BEARER_TOKEN = os.getenv("BEARER_TOKEN")

# Cache environment variables at startup to avoid repeated os.getenv calls
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
EXA_API_KEY = os.getenv("EXA_API_KEY")
VOLC_SEARCH_API_KEY = os.getenv("VOLC_SEARCH_API_KEY") or os.getenv("VOLC_API_KEY")
ZHIHU_SECRET = os.getenv("ZHIHU_ACCESS_SECRET") or os.getenv("ZHIHU_API_KEY")
SCRAPFLY_API_KEY = os.getenv("SCRAPFLY_API_KEY")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")

# Optional import for crawl4ai
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False
    logger.warning("crawl4ai is not installed. Local dynamic crawl will be skipped.")

# Global crawler instance
global_crawler = None

# Semaphore to limit concurrent Playwright browser renders (prevents OOM)
_browser_semaphore = asyncio.Semaphore(1)

# Semaphore to limit concurrent crawl requests (prevents target rate-limiting)
_crawl_semaphore = asyncio.Semaphore(3)

# Global httpx client with connection pooling (reused across all requests)
http_client: httpx.AsyncClient | None = None

@asynccontextmanager
async def get_http_client(timeout_seconds: float = 10.0, follow_redirects: bool = False, verify: bool = True):
    global http_client
    if http_client is not None:
        yield http_client
    else:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=follow_redirects,
            verify=verify
        ) as client:
            yield client

# Auto-detect memory limits to prevent container OOM crashes on low-resource environments like DCD
if CRAWL4AI_AVAILABLE:
    disable_env = os.getenv("DISABLE_LOCAL_BROWSER", "").lower() in ("true", "1", "yes")
    if disable_env:
        logger.info("DISABLE_LOCAL_BROWSER environment variable is set. Local browser disabled.")
        CRAWL4AI_AVAILABLE = False
    else:
        try:
            import psutil
            total_mem_gb = psutil.virtual_memory().total / (1024 ** 3)
            logger.info(f"System memory detected: {total_mem_gb:.2f} GB")
            if total_mem_gb < 1.5:
                logger.warning(f"System memory ({total_mem_gb:.2f} GB) is less than 1.5 GB. "
                               "Disabling local browser to prevent container OOM (Out Of Memory) kills.")
                CRAWL4AI_AVAILABLE = False
        except Exception as e:
            logger.warning(f"Could not perform system memory auto-check: {str(e)}")



def get_active_engines() -> str:
    engines = []
    if TAVILY_API_KEY:
        engines.append("Tavily")
    if EXA_API_KEY:
        engines.append("Exa")
    if VOLC_SEARCH_API_KEY:
        engines.append("Volcengine")
    engines.append("DuckDuckGo (Free)")
    return ", ".join(engines)

# Global counters and startup time
search_count = 0
crawl_count = 0
start_time = time.time()

# 0. TTL Cache with LRU eviction and max size limit
class TTLCache:
    def __init__(self, ttl_seconds: int = 600, maxsize: int = 500):
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self.cache = {}
        self._order = []

    def _evict_expired(self):
        now = time.time()
        expired = [k for k, (_, ts) in self.cache.items() if now - ts >= self.ttl]
        for k in expired:
            del self.cache[k]
            if k in self._order:
                self._order.remove(k)

    def get(self, key):
        if key in self.cache:
            val, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                if key in self._order:
                    self._order.remove(key)
                self._order.append(key)
                return val
            else:
                del self.cache[key]
                if key in self._order:
                    self._order.remove(key)
        return None

    def set(self, key, val):
        if key in self.cache:
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            self.cache[key] = (val, time.time())
            return
        self._evict_expired()
        while len(self.cache) >= self.maxsize and self._order:
            oldest = self._order.pop(0)
            self.cache.pop(oldest, None)
        self.cache[key] = (val, time.time())
        self._order.append(key)

search_cache = TTLCache(ttl_seconds=1800, maxsize=300)
crawl_cache = TTLCache(ttl_seconds=1800, maxsize=150)

# 1. Initialize FastMCP with DNS rebinding protection disabled for cloud/proxy deployments
mcp = FastMCP(
    "Search & Crawl Server",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
)

# 2. Define DuckDuckGo search fallback helper
async def search_duckduckgo_raw(query: str) -> list[dict]:
    import urllib.parse
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8"
    }
    try:
        async with get_http_client(timeout_seconds=10.0) as client:
            res = await client.post(url, data=params, headers=headers)
        if res.status_code != 200:
            return []
            
        try:
            soup = BeautifulSoup(res.text, "lxml")
        except Exception:
            soup = BeautifulSoup(res.text, "html.parser")
        results = soup.select(".result")
        if not results:
            return []
            
        organic_results = []
        for result in results:
            classes = result.get("class", [])
            if "result--ad" in classes:
                continue
                
            title_el = result.select_one(".result__title")
            a_el = result.select_one(".result__a")
            snippet_el = result.select_one(".result__snippet")
            
            if title_el and a_el:
                title = title_el.text.strip()
                raw_link = a_el["href"]
                snippet = snippet_el.text.strip() if snippet_el else "No description."
                
                if "y.js?" in raw_link or "ad_provider" in raw_link:
                    continue
                    
                parsed_link = urllib.parse.urlparse(raw_link)
                query_params = urllib.parse.parse_qs(parsed_link.query)
                link = raw_link
                if "uddg" in query_params:
                    link = query_params["uddg"][0]
                elif raw_link.startswith("//"):
                    link = "https:" + raw_link
                    
                organic_results.append({
                    "title": title,
                    "url": link,
                    "snippet": snippet,
                    "engine": "DuckDuckGo"
                })
                if len(organic_results) >= 10:
                    break
        return organic_results
    except Exception as e:
        logger.error(f"Error executing raw DuckDuckGo search: {str(e)}")
        return []

def is_chinese_query(query: str) -> bool:
    """Detect if the query contains any Chinese characters."""
    return bool(re.search(r'[\u4e00-\u9fff]', query))

async def search_volcengine(query: str, api_key: str) -> list[dict]:
    """Search the web using Volcengine Custom Search API."""
    url = "https://open.feedcoopapi.com/search_api/web_search"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "Query": query,
        "SearchType": "web",
        "Count": 10,
        "Filter": {
            "NeedContent": False,
            "NeedUrl": True
        },
        "NeedSummary": True
    }
    logger.info(f"search_volcengine API call: Query='{query}', Key='{api_key[:4]}...{api_key[-4:]}'")
    async with get_http_client(timeout_seconds=10.0) as client:
        r = await client.post(url, json=payload, headers=headers)
    logger.info(f"search_volcengine API response status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        logger.info(f"search_volcengine response keys: {list(data.keys())}")
        result = data.get("Result", {})
        if not result:
            logger.warning("search_volcengine: 'Result' is empty or null")
            return []
        web_results = result.get("WebResults", [])
        logger.info(f"search_volcengine: WebResults count = {len(web_results)}")
        return [
            {
                "title": item.get("Title", "No Title"),
                "url": item.get("Url", "#"),
                "snippet": item.get("Summary") or item.get("Snippet") or "No description.",
                "engine": f"Volcengine (Relevance: {item.get('RankScore', 0.0) * 100:.1f}% | Auth: {item.get('AuthInfoDes', '未知')})"
            }
            for item in web_results
        ]
    else:
        logger.error(f"search_volcengine error response: {r.text}")
        raise Exception(f"Volcengine Custom Search returned status code {r.status_code}: {r.text}")

# Helper functions for the Search Aggregator Gateway
async def search_tavily(query: str, api_key: str) -> list[dict]:
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": 10
    }
    async with get_http_client(timeout_seconds=10.0) as client:
        r = await client.post(url, json=payload)
    if r.status_code == 200:
        data = r.json()
        return [
            {
                "title": item.get("title", "No Title"),
                "url": item.get("url", "#"),
                "snippet": item.get("content", "No description."),
                "engine": "Tavily"
            }
            for item in data.get("results", [])
        ]
    else:
        raise Exception(f"Tavily returned status code {r.status_code}")

async def search_exa(query: str, api_key: str) -> list[dict]:
    url = "https://api.exa.ai/search"
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "numResults": 10,
        "contents": {
            "text": {
                "maxCharacters": 400
            }
        }
    }
    async with get_http_client(timeout_seconds=10.0) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code == 200:
        data = r.json()
        return [
            {
                "title": item.get("title") or "No Title",
                "url": item.get("url", "#"),
                "snippet": (item.get("text", "")[:350] + "...") if item.get("text") else "No description.",
                "engine": "Exa"
            }
            for item in data.get("results", [])
        ]
    else:
            raise Exception(f"Exa returned status code {r.status_code}")



# Google search implementation has been removed.

async def search_zhihu_impl(query: str, count: int = 5) -> list[dict]:
    """
    Search Zhihu content using the official developer API.
    """
    secret = ZHIHU_SECRET
    if not secret:
        logger.warning("Zhihu Access Secret (ZHIHU_ACCESS_SECRET/ZHIHU_API_KEY) is not configured.")
        return []
        
    base_url = os.getenv("ZHIHU_OPENAPI_BASE_URL", "https://developer.zhihu.com").strip().rstrip("/")
    endpoint = f"{base_url}/api/v1/content/zhihu_search"
    
    params = {
        "Query": query,
        "Count": str(count)
    }
    
    headers = {
        "Authorization": f"Bearer {secret}",
        "X-Request-Timestamp": str(int(time.time())),
        "User-Agent": "Multi-Engine-Search-Crawl-MCP/1.0",
        "Content-Type": "application/json"
    }
    
    # SSL config
    skip_verify = os.getenv("ZHIHU_SKIP_TLS_VERIFY", "").strip() == "1"
    require_verify = os.getenv("ZHIHU_REQUIRE_TLS_VERIFY", "").strip() == "1"
    verify_opt = True
    if skip_verify:
        verify_opt = False
    elif not require_verify:
        try:
            import certifi
            verify_opt = certifi.where()
        except ImportError:
            verify_opt = False
            
    try:
        async with get_http_client(timeout_seconds=15.0, verify=verify_opt) as client:
            logger.info(f"Attempting search via Zhihu API for query: {query}")
            r = await client.get(endpoint, params=params, headers=headers)
        if r.status_code != 200:
            logger.error(f"Zhihu API failed with status {r.status_code}: {r.text}")
            return []
            
        resp_data = r.json()
        data = resp_data.get("Data") if isinstance(resp_data.get("Data"), dict) else {}
        items = data.get("Items") if isinstance(data.get("Items"), list) else []
        
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append({
                "title": item.get("Title", ""),
                "url": item.get("Url", ""),
                "snippet": item.get("ContentText", ""),
                "engine": f"Zhihu (upvotes: {item.get('VoteUpCount', 0)} | comments: {item.get('CommentCount', 0)})"
            })
        return results
    except Exception as e:
        logger.error(f"Error querying Zhihu search API: {str(e)}")
        return []

async def run_general_web_search(query: str) -> tuple[list[dict], str]:

    # 0. Chinese query: run Volcengine + Tavily in parallel for comprehensive coverage
    #    Volcengine excels at domestic Chinese content (CSDN, 掘金, 知乎...)
    #    Tavily excels at international/official docs (Google, Cloudflare, AWS...)
    if is_chinese_query(query):
        parallel_tasks = []
        task_names = []
        
        if VOLC_SEARCH_API_KEY:
            parallel_tasks.append(search_volcengine(query, VOLC_SEARCH_API_KEY))
            task_names.append("Volcengine")
        if TAVILY_API_KEY:
            parallel_tasks.append(search_tavily(query, TAVILY_API_KEY))
            task_names.append("Tavily")
        
        if len(parallel_tasks) >= 2:
            # Both engines available: run in parallel, merge, deduplicate by URL
            logger.info(f"Chinese query: running {' + '.join(task_names)} in parallel...")
            gathered = await asyncio.gather(*parallel_tasks, return_exceptions=True)
            
            merged = []
            seen_urls = set()
            engines_hit = []
            
            for i, result in enumerate(gathered):
                if isinstance(result, Exception):
                    logger.warning(f"{task_names[i]} failed in parallel search: {result}")
                    continue
                if result:
                    engines_hit.append(task_names[i])
                    for r in result:
                        url = r.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            merged.append(r)
            
            if merged:
                return merged, " + ".join(engines_hit)
            # Both failed, fall through to other engines
            
        elif len(parallel_tasks) == 1:
            # Only one engine available for Chinese query
            try:
                results = await parallel_tasks[0]
                if results:
                    return results, task_names[0]
            except Exception as e:
                logger.warning(f"{task_names[0]} search failed: {e}. Falling back...")
            
    # 1. Try Tavily (for non-Chinese queries, or if parallel section above was skipped/failed)
    if TAVILY_API_KEY:
        try:
            logger.info("Attempting search via Tavily...")
            results = await search_tavily(query, TAVILY_API_KEY)
            return results, "Tavily"
        except Exception as e:
            logger.warning(f"Tavily search failed: {e}. Falling back...")
            
    # 2. Try Exa
    if EXA_API_KEY:
        try:
            logger.info("Attempting search via Exa...")
            results = await search_exa(query, EXA_API_KEY)
            return results, "Exa"
        except Exception as e:
            logger.warning(f"Exa search failed: {e}. Falling back...")
            
    # 3. Try Volcengine fallback for non-Chinese queries
    if VOLC_SEARCH_API_KEY:
        try:
            logger.info("Attempting search via Volcengine (Fallback)...")
            results = await search_volcengine(query, VOLC_SEARCH_API_KEY)
            if results:
                return results, "Volcengine"
        except Exception as e:
            logger.warning(f"Volcengine fallback search failed: {e}. Falling back...")
            
    # 4. Fallback to DuckDuckGo
    logger.info("Executing DuckDuckGo fallback raw search.")
    results = await search_duckduckgo_raw(query)
    return results, "DuckDuckGo"

@mcp.tool()
async def search_zhihu(query: str, count: int = 5) -> str:
    """
    Search Zhihu (知乎) - the premier Chinese high-quality Q&A, technical articles, and knowledge sharing community.
    
    CRITICAL AGENT INSTRUCTIONS FOR HIGH EFFICIENCY:
    - **Troubleshooting & Debugging**: Zhihu is extremely valuable for solving coding errors, deployment bugs, or software configurations. It contains rich human-curated guides and troubleshooting steps that are often of much higher quality than spam/scraper search engine results.
    - **When to Use**:
      1. For ANY Chinese queries seeking how to solve a programming error, framework bug, or setup issue.
      2. For comparisons or reviews of technical stacks, tools, cloud services (e.g. GCP, AWS, Cloudflare, Docker).
      3. For developer experience sharing, coding tutorials, best practices, and architecture designs.
      4. For domestic Chinese opinions, community consensus, industry trends, or localized knowledge.
    - **Language Strategy**: Formulate your query in Chinese (e.g. "Cloudflare Worker 限制" or "FastAPI CORS 报错解决") to get the best curated community discussions.
    
    Args:
        query: The search query (must be key terms, preferably in Chinese).
        count: Optional. Number of search results to return. Defaults to 5.
    """
    try:
        results = await search_zhihu_impl(query, count=count)
        if not results:
            return "No results found or Zhihu search is not configured."
            
        formatted = []
        for idx, item in enumerate(results, 1):
            formatted.append(
                f"{idx}. **[{item['title']}]({item['url']})**\n"
                f"   Engine: {item.get('engine', 'Zhihu')}\n"
                f"   Snippet: {item['snippet']}\n"
            )
        return "\n".join(formatted)
    except Exception as e:
        return f"Error executing Zhihu search: {str(e)}"

# 3. Define Unified search tool with failover and caching
@mcp.tool()
async def search_web(query: str, engines: str | None = None, page: int = 1) -> str:
    """
    Perform a broad web search to find real-time information, news, answers, or documentation.
    
    CRITICAL AGENT INSTRUCTIONS FOR HIGH EFFICIENCY:
    - **Search First**: Use this tool to look up current events, programming APIs, documentation, or factual queries.
    - **Formulate Clean Queries**: For best results, use concise, keyword-focused, space-separated terms (e.g., "FastAPI CORS middleware configuration") rather than conversational sentences or questions.
    - **Evaluate Snippets**: Read the returned search snippets carefully; they often contain the answer directly, or point to a target URL that you can crawl.
    - **Language Strategy**: Use English keywords for technical, programming, or documentation searches. Use Chinese keywords for domestic news or local topics.
    - **Specific URL**: If you already have a target URL, use `crawl_page` directly instead of searching.
    - **No Hallucinations**: Only state facts found directly in the search snippets or crawled content.
    - **Volcengine Search**: Prioritized for Chinese queries to fetch local, high-quality, authoritative results.
    
    Args:
        query: Core search keywords (e.g., "Python 3.12 syntax changes"). Avoid conversational questions.
        engines: Optional. Comma-separated list of engines ("Tavily", "Exa", "Zhihu", "Volcengine"). 
                 Use "Zhihu" for Chinese forum opinions. Use "Volcengine" for domestic Chinese search.
                 Use "hybrid" or "all" to search general web and Zhihu in parallel. 
                 Leave as null/None to use available defaults (highly recommended for general queries).
        page: Optional. Page number for pagination. Defaults to 1.
    """
    global search_count
    search_count += 1
    
    # Cache lookup
    cache_key = f"{query}:default:{page}" if not engines else f"{query}:{engines}:{page}"
    cached = search_cache.get(cache_key)
    if cached:
        logger.info(f"Cache HIT for search query: {query}")
        return cached

    # Parse engines option
    engines_list = [e.strip().lower() for e in engines.split(",")] if engines else []

    web_results = []
    zhihu_results = []
    engine_used = None
    
    # 1. Check if only "zhihu" is requested
    if len(engines_list) == 1 and engines_list[0] == "zhihu":
        if not ZHIHU_SECRET:
            return "Error: Zhihu search was requested, but ZHIHU_ACCESS_SECRET or ZHIHU_API_KEY is not configured in environment variables."
        zhihu_results = await search_zhihu_impl(query)
        engine_used = "Zhihu"
        
    # 2. Check if hybrid / all / both requested
    elif "hybrid" in engines_list or "all" in engines_list or ("zhihu" in engines_list and len(engines_list) > 1):
        web_task = asyncio.create_task(run_general_web_search(query))
        
        if ZHIHU_SECRET:
            zhihu_task = asyncio.create_task(search_zhihu_impl(query))
            web_res_tuple, zhihu_res = await asyncio.gather(web_task, zhihu_task)
            web_results, engine_used = web_res_tuple
            zhihu_results = zhihu_res
        else:
            logger.warning("Zhihu is requested in hybrid search, but ZHIHU_ACCESS_SECRET/ZHIHU_API_KEY is not set. Defaulting to general search only.")
            web_results, engine_used = await web_task
        
    # 3. Else, default web search (with optional forced single engine)
    else:
        
        forced_engine = None
        for eng in engines_list:
            if eng in ["tavily", "exa", "duckduckgo", "volcengine", "volc"]:
                forced_engine = eng
                break
                
        if forced_engine == "tavily" and TAVILY_API_KEY:
            try:
                web_results = await search_tavily(query, TAVILY_API_KEY)
                engine_used = "Tavily"
            except Exception as e:
                return f"Error: Forced Tavily search failed: {e}"
        elif forced_engine == "exa" and EXA_API_KEY:
            try:
                web_results = await search_exa(query, EXA_API_KEY)
                engine_used = "Exa"
            except Exception as e:
                return f"Error: Forced Exa search failed: {e}"
        elif (forced_engine == "volcengine" or forced_engine == "volc") and VOLC_SEARCH_API_KEY:
            try:
                web_results = await search_volcengine(query, VOLC_SEARCH_API_KEY)
                engine_used = "Volcengine"
            except Exception as e:
                return f"Error: Forced Volcengine search failed: {e}"
        elif forced_engine == "duckduckgo":
            web_results = await search_duckduckgo_raw(query)
            engine_used = "DuckDuckGo"
        else:
            # Fallback priority list
            web_res_tuple, engine_used = await run_general_web_search(query)
            web_results = web_res_tuple

    # Merge results
    all_items = []
    if zhihu_results:
        all_items.extend(zhihu_results)
    if web_results:
        all_items.extend(web_results)
        
    if not all_items:
        return "No search results found."
        
    formatted_results = []
    for idx, r in enumerate(all_items[:10]):
        title = r.get("title", "No Title")
        link = r.get("url", "#")
        snippet = r.get("snippet", "No description.")
        engine = r.get("engine", engine_used)
        formatted_results.append(
            f"{idx+1}. **[{title}]({link})**\n"
            f"   *Source*: {engine}\n"
            f"   *Snippet*: {snippet}\n"
        )
        
    result_str = "\n".join(formatted_results)
    search_cache.set(cache_key, result_str)
    return result_str

# 3. Define Crawl tool with Crawl4AI + Heuristic Readability Fallback
def is_blocked_or_empty(text: str) -> bool:
    if not text or len(text.strip()) < 150:
        return True
    
    check_region = text[:2000].lower()
    block_signatures = [
        "cloudflare",
        "captcha",
        "enable javascript",
        "access denied",
        "403 forbidden",
        "ddos",
        "please verify you are a human",
        "security check",
        "robot",
        "distil networks",
        "perimeterx",
        "datadome"
    ]
    for sig in block_signatures:
        if sig in check_region:
            logger.warning(f"Blocking signature detected: '{sig}'")
            return True
            
    return False

def extract_main_content_heuristic(html_content: str, url: str) -> str:
    try:
        soup = BeautifulSoup(html_content, "lxml")
    except Exception:
        soup = BeautifulSoup(html_content, "html.parser")
    
    # 1. Clean absolute structural noise first
    for element in list(soup.find_all(True)):
        if element.name in ["script", "style", "noscript", "iframe", "header", "footer", "nav", "svg", "form", "aside"]:
            element.decompose()
            continue
            
    # 2. Score potential main containers (div, article, section)
    candidates = []
    for element in soup.find_all(["div", "article", "section"]):
        text = element.get_text().strip()
        text_len = len(text)
        if text_len < 100:
            continue
            
        p_count = len(element.find_all("p"))
        
        # Link density calculation: ratio of link text to total text
        link_text_len = 0
        for a in element.find_all("a"):
            link_text_len += len(a.get_text().strip())
        
        link_density = link_text_len / text_len if text_len > 0 else 0
        
        # Heuristic scoring formula
        score = p_count * 12 + (text_len // 100)
        
        # Class and ID keyword matches
        class_list = element.attrs.get("class") if hasattr(element, "attrs") and element.attrs else None
        class_names = "".join(class_list).lower() if isinstance(class_list, list) else str(class_list or "").lower()
        id_name = str(element.attrs.get("id", "")).lower() if hasattr(element, "attrs") and element.attrs else ""
        
        # Positive layout keyword boost
        for kw in ["content", "article", "body", "text", "story", "main", "post"]:
            if kw in class_names or kw in id_name:
                score += 35
                
        # Negative layout keyword penalty
        for kw in ["comment", "footer", "sidebar", "ad", "menu", "nav", "widget", "cookie", "popup", "social-share", "ads", "share"]:
            if kw in class_names or kw in id_name:
                score -= 60
                
        # High link density penalty (indicative of navigation/menu boxes)
        if link_density > 0.4:
            score -= 120
            
        candidates.append((score, element))
        
    best_element = None
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_element = candidates[0]
        if best_score < 20:
            best_element = None
            
    # Extract only from the highest-scoring container if it passes thresholds
    target_element = best_element if best_element else (soup.find("body") or soup)
    
    # 3. Strip layout noise in the target element
    for element in list(target_element.find_all(True)):
        if hasattr(element, "attrs") and element.attrs:
            class_list = element.attrs.get("class")
            class_names = "".join(class_list).lower() if isinstance(class_list, list) else str(class_list or "").lower()
            id_name = str(element.attrs.get("id", "")).lower()
        else:
            class_names = ""
            id_name = ""
            
        is_noise = False
        for kw in ["sidebar", "menu", "footer", "aside", "banner", "cookie", "popup", "social-share", "ads"]:
            if kw in class_names or kw in id_name:
                is_noise = True
                break
        if is_noise:
            element.decompose()
            
    # Convert to markdown
    markdown_text = md(str(target_element), heading_style="ATX").strip()
    markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
    return markdown_text

async def fallback_crawl(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    async with get_http_client(timeout_seconds=15.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"HTTP status code {response.status_code}")
        
    return extract_main_content_heuristic(response.text, url)

async def crawl_scrapfly(url: str, api_key: str) -> str:
    """
    Crawls a web page using Scrapfly Scrape API with Javascript rendering,
    antibot bypass, and automatic Markdown formatting.
    """
    scrapfly_url = "https://api.scrapfly.io/scrape"
    exclude_sel = "nav,header,footer,aside,.nav,.menu,.sidebar,.footer,.header,#nav,#menu,#sidebar,#footer,#header,.cookie,.popup,.social-share,.ad-container,.ads"
    params = {
        "key": api_key,
        "url": url,
        "format": "markdown",
        "only_content": "true",
        "exclude_selectors": exclude_sel
    }
    async with get_http_client(timeout_seconds=30.0) as client:
        logger.info(f"Attempting crawl via Scrapfly API for: {url}")
        r = await client.get(scrapfly_url, params=params)
    if r.status_code == 200:
        data = r.json()
        result = data.get("result", {})
        content = result.get("content", "")
        if content:
            return content
        else:
            raise Exception("Scrapfly returned empty content")
    else:
        err_msg = f"HTTP {r.status_code}"
        try:
            err_msg = r.json().get("detail", err_msg)
        except:
            pass
        raise Exception(f"Scrapfly API failed: {err_msg}")

def chunk_markdown(text: str, max_chars: int = 1000, overlap: int = 200) -> list[str]:
    """
    Splits markdown text into overlapping chunks of maximum size max_chars,
    respecting paragraph (\\n\\n) and line (\\n) boundaries where possible.
    """
    # Safeguard parameters
    overlap = max(0, min(overlap, max_chars - 1))
    
    # 1. Split by double newlines to find paragraphs
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    
    # Break down paragraphs into smaller atomic segments if they exceed max_chars
    segments = []
    for p in paragraphs:
        if len(p) <= max_chars:
            segments.append(p)
        else:
            # Fallback to line splits for oversized paragraphs
            lines = [l.strip() for l in p.split("\n") if l.strip()]
            for line in lines:
                if len(line) <= max_chars:
                    segments.append(line)
                else:
                    # If a single line is still larger than max_chars, split by character slices with overlap
                    start = 0
                    while start < len(line):
                        end = start + max_chars
                        segments.append(line[start:end])
                        start += max_chars - overlap
                        if start >= len(line) or max_chars <= overlap:
                            break
                            
    chunks = []
    current_chunk_segments = []
    current_length = 0
    
    for seg in segments:
        seg_len = len(seg)
        # If adding this segment exceeds max_chars, finalize the current chunk
        if current_chunk_segments and current_length + len("\n\n") + seg_len > max_chars:
            chunk_text = "\n\n".join(current_chunk_segments)
            chunks.append(chunk_text)
            
            # Start a new chunk with trailing segments for overlap
            overlap_segments = []
            overlap_len = 0
            for prev_seg in reversed(current_chunk_segments):
                if overlap_len + len(prev_seg) + (len("\n\n") if overlap_segments else 0) <= overlap:
                    overlap_segments.insert(0, prev_seg)
                    overlap_len += len(prev_seg) + (len("\n\n") if len(overlap_segments) > 1 else 0)
                else:
                    break
            current_chunk_segments = overlap_segments + [seg]
            current_length = sum(len(s) for s in current_chunk_segments) + (len("\n\n") * (len(current_chunk_segments) - 1))
        else:
            current_chunk_segments.append(seg)
            if current_length > 0:
                current_length += len("\n\n")
            current_length += seg_len
            
    if current_chunk_segments:
        chunks.append("\n\n".join(current_chunk_segments))
        
    return chunks

def rank_chunks(query: str, chunks: list[str]) -> list[tuple[float, int, str]]:
    """
    Ranks chunks against a query using TF-IDF and Cosine Similarity.
    Returns a list of tuples: (similarity_score, original_index, chunk_text)
    """
    import math
    from collections import Counter
    
    STOP_WORDS = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by", 
        "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "this", "that", "these", "those", "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
        "us", "them", "my", "your", "his", "their", "our", "its"
    }
    
    def tokenize(text: str) -> list[str]:
        words = re.findall(r'[a-zA-Z0-9_]+|[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text.lower())
        result = []
        i = 0
        while i < len(words):
            if re.match(r'[\u4e00-\u9fff]', words[i]):
                if i + 1 < len(words) and re.match(r'[\u4e00-\u9fff]', words[i + 1]):
                    result.append(words[i] + words[i + 1])
                result.append(words[i])
            else:
                result.append(words[i])
            i += 1
        return [w for w in result if w not in STOP_WORDS]
        
    if not chunks:
        return []
        
    query_tokens = tokenize(query)
    if not query_tokens:
        # Fallback if query has no tokens: return first 3 chunks with 0 similarity
        return [(0.0, idx, chunk) for idx, chunk in enumerate(chunks[:3])]
        
    chunk_token_lists = [tokenize(c) for c in chunks]
    
    # Calculate Document Frequency (DF)
    N = len(chunks)
    df = Counter()
    for tokens in chunk_token_lists:
        for token in set(tokens):
            df[token] += 1
            
    # Calculate IDF for query tokens
    idf = {}
    for token in query_tokens:
        d_f = df.get(token, 0)
        idf[token] = math.log((1 + N) / (1 + d_f)) + 1
        
    # Vectorize query
    query_tf = Counter(query_tokens)
    query_vec = {}
    query_norm_sq = 0.0
    for token in set(query_tokens):
        tf_val = query_tf[token] / len(query_tokens)
        query_vec[token] = tf_val * idf[token]
        query_norm_sq += query_vec[token] ** 2
    query_norm = math.sqrt(query_norm_sq)
    
    if query_norm == 0.0:
        return [(0.0, idx, chunk) for idx, chunk in enumerate(chunks[:3])]
        
    ranked = []
    for idx, (chunk, tokens) in enumerate(zip(chunks, chunk_token_lists)):
        if not tokens:
            ranked.append((0.0, idx, chunk))
            continue
            
        chunk_tf = Counter(tokens)
        chunk_vec = {}
        chunk_norm_sq = 0.0
        for token in set(tokens):
            token_df = df.get(token, 0)
            token_idf = math.log((1 + N) / (1 + token_df)) + 1
            tf_val = chunk_tf[token] / len(tokens)
            chunk_vec[token] = tf_val * token_idf
            chunk_norm_sq += chunk_vec[token] ** 2
            
        chunk_norm = math.sqrt(chunk_norm_sq)
        
        dot_product = 0.0
        for token in set(query_tokens):
            if token in chunk_vec:
                dot_product += query_vec[token] * chunk_vec[token]
                
        similarity = 0.0 if chunk_norm == 0.0 else dot_product / (query_norm * chunk_norm)
        ranked.append((similarity, idx, chunk))
        
    # Sort by similarity descending
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked

def convert_github_url_to_raw(url: str) -> str | None:
    url_stripped = url.strip()
    # Match standard file view: https://github.com/{owner}/{repo}/blob/{branch}/{filepath}
    # Or https://github.com/{owner}/{repo}/raw/{branch}/{filepath}
    match = re.match(r'https?://github\.com/([^/]+)/([^/]+)/(blob|raw)/([^/]+)/(.*)', url_stripped)
    if match:
        owner, repo, _, branch, filepath = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{filepath}"
        
    # Match repo homepage: https://github.com/{owner}/{repo} (optionally with trailing slash or .git)
    match_repo = re.match(r'https?://github\.com/([^/]+)/([^/]+)/?$', url_stripped.rstrip("/"))
    if match_repo:
        owner, repo = match_repo.groups()
        if repo.endswith(".git"):
            repo = repo[:-4]
        # Default to main branch, we will fall back to master if this returns 404
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md"
        
    return None

async def crawl_firecrawl(url: str, api_key: str) -> str:
    """Scrape webpage to clean Markdown using Firecrawl cloud API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "url": url,
        "formats": ["markdown"]
    }
    async with get_http_client(timeout_seconds=30.0) as client:
        resp = await client.post("https://api.firecrawl.dev/v1/scrape", json=payload, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Firecrawl API returned status code {resp.status_code}: {resp.text}")
    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Firecrawl scrape failed: {data.get('error', 'Unknown error')}")
    return data.get("data", {}).get("markdown", "")

async def _fetch_page_content_impl(url: str) -> str:
    # 1. Try local HTTP client with heuristic readability first
    logger.info(f"Attempting local HTTP crawl first: {url}")
    local_text = ""
    local_failed = False
    try:
        local_text = await fallback_crawl(url)
    except Exception as e:
        logger.warning(f"Local HTTP crawl failed: {str(e)}")
        local_failed = True
        
    # Check if local crawl succeeded and passed verification
    if not local_failed and not is_blocked_or_empty(local_text):
        logger.info(f"Local HTTP crawl succeeded and passed verification for: {url}")
        return local_text
        
    # 2. Try local Crawl4AI (dynamic browser rendering) if available
    crawl4ai_text = ""
    crawl4ai_failed = True
    if global_crawler is not None:
        logger.info(f"Local HTTP failed/blocked. Attempting local Crawl4AI crawl for: {url}")
        try:
            async with _browser_semaphore:
                run_conf = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
                result = await asyncio.wait_for(
                    global_crawler.arun(url=url, config=run_conf),
                    timeout=20.0
                )
            if result.success and result.markdown:
                if isinstance(result.markdown, str):
                    extracted_text = result.markdown
                else:
                    extracted_text = result.markdown.raw_markdown if hasattr(result.markdown, 'raw_markdown') else ""
                
                if extracted_text:
                    crawl4ai_text = extracted_text
                    if not is_blocked_or_empty(crawl4ai_text):
                        logger.info(f"Local Crawl4AI crawl succeeded and passed verification for: {url}")
                        return crawl4ai_text
                    else:
                        logger.warning(f"Local Crawl4AI returned blocked or empty content for: {url}")
                        crawl4ai_failed = False
            else:
                err = result.error_message if hasattr(result, "error_message") else "Empty content"
                logger.warning(f"Local Crawl4AI execution returned success=False or empty: {err}")
        except asyncio.TimeoutError:
            logger.error(f"Local Crawl4AI timed out (20s) for: {url}")
        except Exception as e_c4ai:
            logger.error(f"Local Crawl4AI execution failed with error: {str(e_c4ai)}")
    else:
        logger.info("Local Crawl4AI is not initialized/available. Skipping local dynamic crawl.")

    # 3. Failover to Firecrawl API if configured
    if FIRECRAWL_API_KEY:
        logger.info(f"Local crawl paths failed/blocked. Failing over to Firecrawl API for: {url}")
        try:
            return await crawl_firecrawl(url, FIRECRAWL_API_KEY)
        except Exception as e_firecrawl:
            logger.error(f"Firecrawl failover failed: {str(e_firecrawl)}")

    # 4. Failover to Scrapfly API if configured
    if SCRAPFLY_API_KEY:
        logger.info(f"Local crawl paths failed/blocked (or Firecrawl failed). Failing over to Scrapfly API for: {url}")
        try:
            return await crawl_scrapfly(url, SCRAPFLY_API_KEY)
        except Exception as e_scrapfly:
            logger.error(f"Scrapfly failover also failed: {str(e_scrapfly)}")
            if crawl4ai_text:
                logger.info("Returning Crawl4AI text since Scrapfly also failed.")
                return crawl4ai_text
            if not local_failed and local_text:
                logger.info("Returning partially blocked local text since Scrapfly also failed.")
                return local_text
            raise Exception(f"Crawl failed on all local methods, Firecrawl, and Scrapfly: {str(e_scrapfly)}")
    else:
        logger.warning("Local crawl paths failed/blocked, and Scrapfly API key is not configured.")
        if crawl4ai_text:
            return crawl4ai_text
        if not local_failed and local_text:
            return local_text
        raise Exception("All local crawl methods failed/blocked, and no Firecrawl/Scrapfly keys configured for failover.")


async def fetch_page_content(url: str) -> str:
    return await asyncio.wait_for(_fetch_page_content_impl(url), timeout=25.0)


def generate_markdown_outline(markdown_text: str) -> str:
    lines = markdown_text.split("\n")
    headers = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            headers.append(stripped)
            
    outline = "\n".join(headers)
    text_length = len(markdown_text)
    preview = markdown_text[:1200] + "..." if len(markdown_text) > 1200 else markdown_text
    
    return f"""⚠️ **Warning: The webpage content is extremely long ({text_length} characters).**
To prevent context bloat and speed up your response, a structural outline of the page is shown below instead of the full text.

### How to access the full content:
Please use the `crawl_page` tool again and provide a specific `query` parameter (e.g. `query="Refund Policy"`) to extract only the relevant paragraphs.

---

## Webpage Outline (Headers)

{outline if outline else "*No headers found in the webpage.*"}

---

## Content Preview (First 1200 chars)

{preview}
"""

def _normalize_url_for_cache(url: str) -> str:
    from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
    parsed = urlparse(url)
    query = urlencode(sorted(parse_qs(parsed.query).items()), doseq=True) if parsed.query else ""
    path = parsed.path.rstrip('/')
    if not path:
        path = '/'
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, ''))

@mcp.tool()
async def crawl_page(url: str, query: str | None = None) -> str:
    """
    Crawl a specific webpage by its URL to extract and read its full content in clean Markdown format.
    
    CRITICAL AGENT INSTRUCTIONS FOR HIGH EFFICIENCY:
    - **Use for Specific URLs**: Call this tool when you have a target URL (from search results or user input) and need to read its deep content.
    - **Avoid Searching**: Do NOT pass general search queries or questions into the `url` parameter. (Use `search_web` instead).
    - **Semantic Filtering (RAG)**: If you are looking for a specific topic or answering a user's question, **ALWAYS provide the `query` parameter**. This uses local semantic RAG to extract only the top 6 most relevant paragraphs, which prevents token context bloat and speeds up processing.
    - **Full Page Content**: Leave `query` as null/None ONLY if you absolutely need to read the entire document. Note that if the page is extremely long (>25,000 characters), an outline and preview will be returned instead of the full text to avoid client-side slowdowns.
    
    Args:
        url: The absolute HTTP/HTTPS URL of the webpage to crawl.
        query: Optional. A specific question or topic to extract from the page. If provided, returns only the top 6 semantically relevant snippets. If null, returns full content.
    """
    global crawl_count
    crawl_count += 1
    
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Error: URL must start with http:// or https://"
    
    normalized_url = _normalize_url_for_cache(url)
    markdown_text = crawl_cache.get(normalized_url)
    
    if not markdown_text:
        github_raw_url = convert_github_url_to_raw(url)
        target_url = github_raw_url if github_raw_url else url
        
        try:
            text = await fetch_page_content(target_url)
            markdown_text = text
        except Exception as e:
            if github_raw_url and "/main/" in github_raw_url and ("404" in str(e) or "empty" in str(e).lower()):
                fallback_github_url = github_raw_url.replace("/main/", "/master/", 1)
                logger.info(f"GitHub main branch returned 404. Falling back to master branch: {fallback_github_url}")
                try:
                    text = await fetch_page_content(fallback_github_url)
                    markdown_text = text
                except Exception as e_inner:
                    return f"Error crawling GitHub page (failed on both main and master branches): {str(e_inner)}"
            else:
                return f"Error crawling page '{url}': {str(e)}"
                
        crawl_cache.set(normalized_url, markdown_text)
        if github_raw_url:
            crawl_cache.set(_normalize_url_for_cache(github_raw_url), markdown_text)

    # If query is None but the webpage is extremely long, return outline to prevent client slowdown
    if not query and len(markdown_text) > 25000:
        return generate_markdown_outline(markdown_text)

    # If query is provided, perform semantic RAG chunking
    if query:
        logger.info(f"Performing semantic chunking for query: {query}")
        chunks = chunk_markdown(markdown_text, max_chars=1500, overlap=300)
        if not chunks:
            return "No content could be extracted from this webpage."
            
        top_chunks = rank_chunks(query, chunks)
        if not top_chunks:
            return "No matching content could be ranked."
            
        # Check if the highest similarity is 0
        has_matches = top_chunks and top_chunks[0][0] > 0.0
        
        # Sort by similarity descending, return top 6
        top_chunks_to_return = top_chunks[:6]
        
        header = ""
        if not has_matches:
            header += "⚠️ **Warning: No direct keyword matches were found for your query.** The webpage might be written in a different language, or the keywords do not exist in the text. Showing the first few paragraphs as fallback. Try searching with keywords matched to the page's actual language.\n\n"
        
        header += f"*Showing top {len(top_chunks_to_return)} segments of the webpage for the query: \"{query}\"*\n\n"
        
        # Format the top chunks
        total_chunks = len(chunks)
        formatted_snippets = []
        for idx, (similarity, orig_idx, chunk_text) in enumerate(top_chunks_to_return):
            formatted_snippets.append(
                f"### Segment {idx + 1} (Relevance: {similarity:.2f} | Position: {orig_idx + 1}/{total_chunks})\n\n{chunk_text}"
            )
        return header + "\n\n---\n\n".join(formatted_snippets)
        
    return markdown_text

def extract_markdown_links(markdown_text: str, current_url: str) -> list[str]:
    """Extract unique absolute HTTP(S) links from Markdown text, excluding images and static assets."""
    import urllib.parse
    # Match [text](url) but NOT ![alt](url) — use negative lookbehind for '!'
    # Allow spaces inside parentheses to capture hover titles like [text](url "title")
    matches = re.findall(r'(?<!!)\[[^\]]*\]\(([^)]+)\)', markdown_text)
    resolved = set()
    # Common static file extensions to exclude
    static_exts = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.css', '.js', '.pdf', '.zip', '.tar', '.gz', '.mp4', '.mp3', '.woff', '.woff2', '.ttf', '.eot'}
    for link in matches:
        # Extract the URL portion (the first space-separated token if title is present)
        link_stripped = link.strip()
        if not link_stripped:
            continue
        link_url = link_stripped.split()[0]
        link_clean = link_url.split("#")[0].split("?")[0].strip()
        if not link_clean or len(link_clean) > 500:
            continue
        abs_url = urllib.parse.urljoin(current_url, link_clean)
        if not (abs_url.startswith("http://") or abs_url.startswith("https://")):
            continue
        # Skip static asset URLs
        parsed_path = urllib.parse.urlparse(abs_url).path.lower()
        if any(parsed_path.endswith(ext) for ext in static_exts):
            continue
        resolved.add(abs_url)
    return list(resolved)

@mcp.tool()
async def crawl_site(start_url: str, max_pages: int = 10, max_depth: int = 2, prefix_filter: str | None = None) -> str:
    """
    Recursively crawl a specific documentation section or chapter of a website starting from a URL.
    This fetches multiple sibling and child pages under the same directory prefix to gather complete context.
    
    CRITICAL AGENT INSTRUCTIONS FOR HIGH EFFICIENCY:
    - **Use for Complete Documentation Chapters**: Use this tool when you need to understand an entire section of a guide or API doc (e.g. all guides under '/edge/' or '/docs/'), rather than just reading one page.
    - **Identify Section Structure**: Great for crawling API references, tutorial chapters, user guides, or site wikis.
    - **Scope Control**: By default, it automatically restricts crawling to links that share the parent folder path of the start_url.
    - **Depth Control**: max_depth controls how many link-hops deep the crawler goes (0 = start page only, 1 = start page + direct links, 2 = two hops). Keep this small to avoid crawling the entire site.
    - **Avoid Context Overload**: Limits to 10 pages and depth 2 by default. Do not request more than 25 pages or depth 5.
    
    Args:
        start_url: The starting URL (e.g. 'https://docs.wasmer.io/edge/deploy').
        max_pages: Optional. The maximum number of pages to crawl (default 10, max 25).
        max_depth: Optional. The maximum link-hop depth from the start page (default 2, max 5). 0 means only the start page itself.
        prefix_filter: Optional. Override the prefix URL. Only links starting with this URL will be crawled. If null, automatically uses the parent folder of the start_url.
    """
    import urllib.parse
    global crawl_count
    
    if not (start_url.startswith("http://") or start_url.startswith("https://")):
        return "Error: Start URL must start with http:// or https://"
        
    max_pages = min(max(max_pages, 1), 25)
    max_depth = min(max(max_depth, 0), 5)
    
    # Determine directory prefix to restrict the crawl to the same chapter
    if not prefix_filter:
        parsed_start = urllib.parse.urlparse(start_url)
        start_path = parsed_start.path.rstrip("/")
        if "/" in start_path:
            prefix_path = "/".join(start_path.split("/")[:-1]) + "/"
        else:
            prefix_path = "/"
        prefix_filter = f"{parsed_start.scheme}://{parsed_start.netloc}{prefix_path}"
        
    logger.info(f"Starting documentation crawl. Start URL: {start_url}, Prefix: {prefix_filter}, Max pages: {max_pages}, Max depth: {max_depth}")
    
    visited = {}       # url -> markdown content
    url_depth = {}      # url -> depth level
    to_visit = [(start_url, 0)]  # (url, depth) tuples
    
    while to_visit and len(visited) < max_pages:
        batch = to_visit[:3]
        to_visit = to_visit[len(batch):]
        
        batch_urls = [url for url, _ in batch]
        batch_depths = {url: depth for url, depth in batch}
        
        logger.info(f"Crawling batch of {len(batch_urls)} pages (depths: {[d for _, d in batch]})...")
        
        fetch_tasks = {}
        for url in batch_urls:
            cached = crawl_cache.get(url)
            if cached:
                visited[url] = cached
                url_depth[url] = batch_depths[url]
                crawl_count += 1
            else:
                fetch_tasks[url] = fetch_page_content(url)
        
        if fetch_tasks:
            async def _fetch_with_limit(u, coro):
                async with _crawl_semaphore:
                    await asyncio.sleep(0.3)
                    return await coro
            
            task_urls = list(fetch_tasks.keys())
            limited_tasks = [_fetch_with_limit(u, fetch_tasks[u]) for u in task_urls]
            results = await asyncio.gather(*limited_tasks, return_exceptions=True)
            
            for url, result in zip(task_urls, results):
                crawl_count += 1
                depth = batch_depths[url]
                if isinstance(result, Exception):
                    logger.error(f"Error fetching {url} (depth {depth}): {result}")
                    visited[url] = f"Error fetching page: {str(result)}"
                    url_depth[url] = depth
                    continue
                    
                visited[url] = result
                url_depth[url] = depth
                crawl_cache.set(url, result)
        
        # Extract links only from pages whose depth < max_depth
        new_links = []  # list of (link, depth) tuples
        for url in batch_urls:
            depth = batch_depths[url]
            if depth >= max_depth:
                continue  # Do not extract links beyond max depth
            content = visited.get(url, "")
            if content.startswith("Error fetching"):
                continue
            extracted = extract_markdown_links(content, url)
            queued_urls = {u for u, _ in to_visit}
            seen_in_new = {u for u, _ in new_links}
            for link in extracted:
                if link.startswith(prefix_filter) and link not in visited and link not in queued_urls and link not in seen_in_new:
                    new_links.append((link, depth + 1))
                    
        # Append new links with their correct depth to queue
        to_visit.extend(new_links)
        
    # Format the crawled pages
    total = len(visited)
    formatted = []
    for idx, (url, content) in enumerate(visited.items(), 1):
        depth = url_depth.get(url, 0)
        # Truncate very long individual pages to prevent context explosion
        if len(content) > 15000:
            content = content[:15000] + f"\n\n... (truncated, {len(content)} chars total)"
        formatted.append(f"## Page {idx}/{total} (depth {depth}): {url}\n\n{content}")
        
    return "\n\n---\n\n".join(formatted)

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

async def init_crawler_bg():
    global global_crawler
    if not CRAWL4AI_AVAILABLE:
        logger.warning("Crawl4AI is not available, skipping crawler startup in background task.")
        return
        
    try:
        logger.info("Initializing global AsyncWebCrawler in background task...")
        browser_conf = BrowserConfig(
            headless=True,
            text_mode=True,
            light_mode=True,
            extra_args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        crawler_instance = AsyncWebCrawler(config=browser_conf)
        
        # Start the crawler with a 15-second timeout guard
        await asyncio.wait_for(crawler_instance.start(), timeout=15.0)
        global_crawler = crawler_instance
        logger.info("Global AsyncWebCrawler successfully started in background task.")
    except asyncio.TimeoutError:
        logger.error("Failed to initialize global AsyncWebCrawler: Timeout (15s) expired during launch.")
        global_crawler = None
    except Exception as e:
        logger.error(f"Failed to initialize global AsyncWebCrawler in background task: {str(e)}")
        global_crawler = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True
    )
    logger.info("Global httpx client initialized with connection pooling.")

    if CRAWL4AI_AVAILABLE:
        asyncio.create_task(init_crawler_bg())
    else:
        logger.warning("Crawl4AI is not available, skipping crawler task creation in lifespan.")
        
    yield
    
    if http_client:
        await http_client.aclose()
        http_client = None
        logger.info("Global httpx client closed.")
    
    if global_crawler:
        logger.info("Closing global AsyncWebCrawler...")
        try:
            await global_crawler.close()
            logger.info("Global AsyncWebCrawler closed.")
        except Exception as e:
            logger.error(f"Error closing global AsyncWebCrawler: {str(e)}")

# Create FastAPI app
app = FastAPI(title="Multi-Engine Search & Crawl MCP Server", lifespan=lifespan)


class SimpleAuthMiddleware:
    """
    A cleaner ASGI middleware that ONLY handles Bearer token authentication
    for the /mcp routes, without breaking ASGI path routing or FastMCP apps.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        # Only protect /mcp routes, but exclude /mcp/messages since SSE clients do not send headers on POST
        if path.startswith("/mcp") and not path.startswith("/mcp/messages") and BEARER_TOKEN:
            from urllib.parse import parse_qs
            headers = dict(scope.get("headers", []))
            query_string = scope.get("query_string", b"").decode("utf-8")
            token = None
            
            # Check Authorization header
            auth_header_bytes = headers.get(b"authorization", b"")
            if auth_header_bytes:
                auth_header = auth_header_bytes.decode("utf-8")
                parts = auth_header.split()
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    token = parts[1]
                else:
                    token = auth_header
            
            # Check query param
            if not token and query_string:
                params = parse_qs(query_string)
                if "token" in params:
                    token = params["token"][0]

            if not token:
                logger.warning(f"Auth failed (Missing Token): {method} {path}")
                await self.send_error(send, 401, "Missing Authorization Token")
                return

            import secrets
            if not secrets.compare_digest(token, BEARER_TOKEN):
                logger.warning(f"Auth failed (Invalid Token - length {len(token)} vs expected {len(BEARER_TOKEN)}): {method} {path}")
                await self.send_error(send, 403, "Invalid Bearer Token")
                return

        # Pass through to the underlying FastAPI app and FastMCP mounts
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

app.add_middleware(SimpleAuthMiddleware)

# Mount native FastMCP SSE app
# The SSE endpoint will be available at GET /mcp/sse (since FastMCP sse_app maps /sse internally)
# Wait, FastMCP's sse_app() internally creates Starlette routes: GET / and POST /messages
# By mounting at /mcp, clients should connect to GET /mcp/ and POST /mcp/messages
# For compatibility with clients expecting /mcp/sse, FastMCP in its newer versions maps GET /sse
app.mount("/mcp", mcp.sse_app())

@app.get("/health")
async def health_check():
    return {"status": "ok", "active_engines": get_active_engines(), "auth_enabled": BEARER_TOKEN is not None}

# API Endpoint to fetch current stats
@app.get("/api/stats")
async def api_stats():
    return {
        "status": "online",
        "uptime": get_uptime(),
        "active_engines": get_active_engines(),
        "auth_enabled": BEARER_TOKEN is not None,
        "crawl4ai_available": CRAWL4AI_AVAILABLE,
        "crawl4ai_active": global_crawler is not None,
        "stats": {
            "searches": search_count,
            "crawls": crawl_count
        }
    }

# Premium HTML Dashboard UI
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    server_start_time = start_time
    # Dynamic host detection for config helper (checking X-Forwarded-Host from proxy)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "khcsearch.wasmer.app"))
    proto = request.headers.get("x-forwarded-proto", "https")
    sse_url = f"{proto}://{host}/mcp/sse"
    # Security: mask the token in the public dashboard to prevent leaking secrets
    # Show only first 4 and last 2 chars, e.g. "KangH...n@"
    if BEARER_TOKEN:
        if len(BEARER_TOKEN) > 8:
            token_val = BEARER_TOKEN[:5] + "..." + BEARER_TOKEN[-2:]
        else:
            token_val = BEARER_TOKEN[:2] + "..."
    else:
        token_val = "YOUR_BEARER_TOKEN"
    
    # 1. Check BEARER_TOKEN and configure status indicators & banners
    if BEARER_TOKEN:
        auth_title = "已启用"
        auth_status_html = '<span class="status-indicator active-text"><i class="fa-solid fa-shield-halved"></i> 已配置密钥</span>'
        tip_banner_html = f"""
        <div style="font-size: 0.825rem; color: var(--success-color); border: 1px solid rgba(16, 185, 129, 0.2); background: rgba(16, 185, 129, 0.05); padding: 0.6rem 0.8rem; border-radius: 8px; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem;">
            <i class="fa-solid fa-circle-check"></i>
            <span><strong>提示:</strong> 密钥已配置！出于安全考虑，配置中的 Token 已脱敏显示（<code>{token_val}</code>），请将其替换为您的完整密钥后再导入 Agent。</span>
        </div>
        """
    else:
        auth_title = "未启用"
        auth_status_html = '<span class="status-indicator inactive-text" style="color: var(--warning-color);"><i class="fa-solid fa-triangle-exclamation"></i> 未配置密钥</span>'
        tip_banner_html = f"""
        <div style="font-size: 0.825rem; color: var(--warning-color); border: 1px solid rgba(245, 158, 11, 0.2); background: rgba(245, 158, 11, 0.05); padding: 0.6rem 0.8rem; border-radius: 8px; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem;">
            <i class="fa-solid fa-triangle-exclamation"></i>
            <span><strong>提示:</strong> 请将配置中的 <code>YOUR_BEARER_TOKEN</code> 替换为您在 Hugging Face Space 中配置的实际 <code>BEARER_TOKEN</code> 密钥。</span>
        </div>
        """

    # 2. Render active engines list in HTML (configured in green, fallback in gray)
    engines_list = []
    if os.getenv("TAVILY_API_KEY"):
        engines_list.append('<span class="active-text">Tavily</span>')
    if os.getenv("EXA_API_KEY"):
        engines_list.append('<span class="active-text">Exa</span>')
    if os.getenv("VOLC_SEARCH_API_KEY") or os.getenv("VOLC_API_KEY"):
        engines_list.append('<span class="active-text">Volcengine</span>')
    engines_list.append('<span style="color: var(--text-muted);">DuckDuckGo (Free)</span>')
    search_engines_html = ", ".join(engines_list)
    
    # 3. Crawler engine description
    c_engines = []
    if global_crawler is not None:
        c_engines.append("Playwright (Chromium)")
    if os.getenv("FIRECRAWL_API_KEY"):
        c_engines.append("Firecrawl (Cloud)")
    if os.getenv("SCRAPFLY_API_KEY"):
        c_engines.append("Scrapfly (Proxy)")
        
    if c_engines:
        crawler_engine_html = f'<span class="active-text"><i class="fa-solid fa-spider"></i> {" + ".join(c_engines)}</span>'
    elif CRAWL4AI_AVAILABLE:
        crawler_engine_html = '<span class="inactive-text"><i class="fa-solid fa-spinner fa-spin"></i> 正在启动...</span>'
    else:
        crawler_engine_html = '<span class="inactive-text" style="color: var(--warning-color);"><i class="fa-solid fa-microchip"></i> Heuristics (轻量降级)</span>'

    # 4. Generate copyable configs (token is masked for security, user must fill in full token)
    sse_config_str = (
        '{\n'
        '  "mcpServers": {\n'
        '    "hf-search-crawl-mcp": {\n'
        '      "type": "sse",\n'
        f'      "url": "{sse_url}?token={token_val}"\n'
        '    }\n'
        '  }\n'
        '}'
    )
    
    hermes_config_str = (
        'mcp_servers:\n'
        '  hf-search-crawl-mcp:\n'
        f'    url: "{sse_url}?token={token_val}"\n'
        '    transport: sse'
    )
    
    claude_code_cli = f'claude mcp add hf-search-crawl-mcp --transport http "{sse_url}?token={token_val}"'
    claude_config_str = (
        f'# 命令行添加 (推荐 - 复制并在终端运行):\n'
        f'{claude_code_cli}\n\n'
        f'# 或手动写入 ~/.claude.json 中的 mcpServers:\n'
        '{\n'
        '  "mcpServers": {\n'
        '    "hf-search-crawl-mcp": {\n'
        '      "type": "http",\n'
        f'      "url": "{sse_url}?token={token_val}"\n'
        '    }\n'
        '  }\n'
        '}'
    )
    
    sse_config_js = json.dumps(sse_config_str)
    hermes_config_js = json.dumps(hermes_config_str)
    claude_config_js = json.dumps(claude_config_str)

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
                        <div class="card-value" id="uptime">计算中...</div>
                        <div class="card-desc">自本次启动以来的运行时间</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>密钥鉴权</span>
                            {auth_status_html}
                        </li>
                    </ul>
                </div>

                <!-- Search Gateway status card -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">搜索引擎网关</span>
                        <i class="fa-solid fa-magnifying-glass card-icon"></i>
                    </div>
                    <div>
                        <div class="card-value">{search_count}</div>
                        <div class="card-desc">本次会话累计执行的搜索次数</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>活跃搜索引擎</span>
                            <span style="word-break: break-all; max-width: 170px; text-align: right;">{search_engines_html}</span>
                        </li>
                    </ul>
                </div>

                <!-- Crawl4AI status card -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">CRAWL4AI 网页爬虫</span>
                        <i class="fa-solid fa-spider card-icon"></i>
                    </div>
                    <div>
                        <div class="card-value">{crawl_count}</div>
                        <div class="card-desc">本次会话累计执行的网页抓取次数</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>爬虫引擎</span>
                            {crawler_engine_html}
                        </li>
                    </ul>
                </div>
            </div>

            <!-- Client Config block -->
            <div class="config-section">
                <div class="config-header">
                    <span class="config-title"><i class="fa-solid fa-cog"></i> 客户端 Agent 连接配置配置助手</span>
                    <div style="display: flex; gap: 0.5rem;">
                        <button class="tab-btn active" onclick="showTab('sse')">通用版本 (原生 SSE)</button>
                        <button class="tab-btn" onclick="showTab('hermes')">Hermes 版本 (YAML)</button>
                        <button class="tab-btn" onclick="showTab('claude')">Claude Code 版本 (CLI)</button>
                    </div>
                </div>
                <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.8rem;" id="config-desc">
                    通用直连版本：如果您的 Agent 客户端（如 Cursor、ModelScope Agent Studio 等）原生支持 SSE 协议，直接填写以下配置即可：
                </p>
                {tip_banner_html}
                <div style="position: relative;">
                    <pre id="json-config" style="padding-top: 2.5rem; min-height: 180px;"></pre>
                    <button class="copy-btn" onclick="copyConfig()" style="position: absolute; right: 10px; top: 10px;"><i class="fa-solid fa-copy"></i> 复制配置</button>
                </div>
            </div>
        </main>

        <footer>
            <p>Model Context Protocol Server | 运行于 Hugging Face Spaces</p>
        </footer>

        <script>
            // Config Templates injected from Python safely via json.dumps
            const sseConfig = {sse_config_js};
            const hermesConfig = {hermes_config_js};
            const claudeConfig = {claude_config_js};

            let currentTab = 'sse';

            function updateConfigDisplay() {{
                const configPre = document.getElementById('json-config');
                const desc = document.getElementById('config-desc');
                
                if (currentTab === 'sse') {{
                    configPre.textContent = sseConfig;
                    desc.innerHTML = '通用直连版本：如果您的 Agent 客户端（如 Cursor、ModelScope Agent Studio 等）原生支持 SSE 协议，直接填写以下配置即可：';
                }} else if (currentTab === 'hermes') {{
                    configPre.textContent = hermesConfig;
                    desc.innerHTML = 'Hermes 客户端：请将以下配置写入到您的 <code>~/.hermes/config.yaml</code> 文件中：';
                }} else {{
                    configPre.textContent = claudeConfig;
                    desc.innerHTML = 'Claude Code 命令行客户端：复制并在您的终端中直接执行以下命令，或手动配置 <code>~/.claude.json</code>：';
                }}
            }}

            function showTab(tab) {{
                currentTab = tab;
                document.querySelectorAll('.tab-btn').forEach(btn => {{
                    btn.classList.remove('active');
                }});
                
                // Highlight active button
                const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => {{
                    if (tab === 'sse') return b.textContent.includes('通用');
                    if (tab === 'hermes') return b.textContent.includes('Hermes');
                    return b.textContent.includes('Claude');
                }});
                if (btn) btn.classList.add('active');
                
                updateConfigDisplay();
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

            // Client-side ticking uptime based on server start time (zero network requests)
            const serverStartTime = {server_start_time} * 1000;

            function updateUptime() {{
                const diffMs = Date.now() - serverStartTime;
                const diffSecs = Math.max(0, Math.floor(diffMs / 1000));
                
                const days = Math.floor(diffSecs / 86400);
                const hours = Math.floor((diffSecs % 86400) / 3600);
                const minutes = Math.floor((diffSecs % 3600) / 60);
                const seconds = diffSecs % 60;
                
                let uptimeStr = "";
                if (days > 0) uptimeStr += days + "天 ";
                if (hours > 0 || days > 0) uptimeStr += hours + "小时 ";
                if (minutes > 0 || hours > 0 || days > 0) uptimeStr += minutes + "分 ";
                uptimeStr += seconds + "秒";
                
                document.getElementById('uptime').textContent = uptimeStr;
            }}

            updateUptime();
            setInterval(updateUptime, 1000);

            // Initial display - NO background polling to allow Wasmer scale-to-zero sleep/hibernation
            updateConfigDisplay();
        </script>
    </body>
    </html>
    """
    return html_content

if __name__ == "__main__":
    mcp.run()
