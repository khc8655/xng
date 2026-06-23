import os
import asyncio
import logging
import re
import time
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

BEARER_TOKEN = os.getenv("BEARER_TOKEN")

# Optional import for crawl4ai
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False
    logger.warning("crawl4ai is not installed. Local dynamic crawl will be skipped.")

# Global crawler instance
global_crawler = None


def get_active_engines() -> str:
    engines = []
    if os.getenv("TAVILY_API_KEY"):
        engines.append("Tavily")
    if os.getenv("EXA_API_KEY"):
        engines.append("Exa")
    if os.getenv("GOOGLE_API_KEY") and os.getenv("GOOGLE_CX"):
        engines.append("Google")
    engines.append("DuckDuckGo (Free)")
    return ", ".join(engines)

# Global counters and startup time
search_count = 0
crawl_count = 0
start_time = time.time()

# 0. Lightweight TTL Cache class
class TTLCache:
    def __init__(self, ttl_seconds: int = 600):
        self.ttl = ttl_seconds
        self.cache = {}
        
    def get(self, key):
        if key in self.cache:
            val, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return val
            else:
                del self.cache[key]
        return None
        
    def set(self, key, val):
        self.cache[key] = (val, time.time())

search_cache = TTLCache(ttl_seconds=600)
crawl_cache = TTLCache(ttl_seconds=600)

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, data=params, headers=headers)
            if res.status_code != 200:
                return []
                
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

# Helper functions for the Search Aggregator Gateway
async def search_tavily(query: str, api_key: str) -> list[dict]:
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": 10
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
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
    async with httpx.AsyncClient(timeout=10.0) as client:
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



async def search_google(query: str, api_key: str, cx: str) -> list[dict]:
    url = "https://customsearch.googleapis.com/customsearch/v1"
    params = {
        "q": query,
        "key": api_key,
        "cx": cx,
        "num": 10
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, params=params)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items", [])
            return [
                {
                    "title": item.get("title", "No Title"),
                    "url": item.get("link", "#"),
                    "snippet": item.get("snippet", "No description."),
                    "engine": "Google"
                }
                for item in items
            ]
        else:
            raise Exception(f"Google returned status code {r.status_code}")

async def search_zhihu(query: str, count: int = 5) -> list[dict]:
    """
    Search Zhihu content using the official developer API.
    """
    secret = os.getenv("ZHIHU_ACCESS_SECRET") or os.getenv("ZHIHU_API_KEY")
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
        async with httpx.AsyncClient(timeout=15.0, verify=verify_opt) as client:
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
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
    EXA_API_KEY = os.getenv("EXA_API_KEY")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    
    # 1. Try Tavily
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
            
    # 3. Try Google
    if GOOGLE_API_KEY and GOOGLE_CX:
        try:
            logger.info("Attempting search via Google Custom Search...")
            results = await search_google(query, GOOGLE_API_KEY, GOOGLE_CX)
            return results, "Google"
        except Exception as e:
            logger.warning(f"Google Custom Search failed: {e}. Falling back...")
            
    # 4. Fallback to DuckDuckGo
    logger.info("Executing DuckDuckGo fallback raw search.")
    results = await search_duckduckgo_raw(query)
    return results, "DuckDuckGo"

# 3. Define Unified search tool with failover and caching
@mcp.tool()
async def search_web(query: str, engines: str | None = None, page: int = 1) -> str:
    """
    Perform a broad web search to find real-time information, news, answers, or references.
    
    CRITICAL AGENT INSTRUCTIONS:
    - ALWAYS use this tool FIRST when asked about current events, facts, documentation, or unknown topics.
    - If the user provides a specific URL to read, do NOT use this tool. Use `crawl_page` instead.
    - If your search returns a list of URLs but you need the deep content of a specific result, pass that URL to `crawl_page`.
    - Do NOT hallucinate search results. Only return information present in the snippet.

    Args:
        query: The specific search query. Keep it concise, keyword-focused, and descriptive (e.g., "Python 3.11 release notes").
        engines: Optional. Comma-separated list of engines (e.g., "Tavily, Exa, Google, Zhihu"). 
                 Specify "Zhihu" to search Zhihu content only. Specify "hybrid" or "all" to search both general web and Zhihu in parallel and merge results.
                 Leave as null/None to use all available web defaults.
        page: Optional. The page number for pagination. Defaults to 1. Increment if you need more results for the same query.
        
    Returns:
        A Markdown-formatted list of up to 10 search results containing titles, snippets, and source URLs.
    """
    global search_count
    search_count += 1
    
    # Cache lookup
    cache_key = f"{query}:{engines}:{page}"
    cached = search_cache.get(cache_key)
    if cached:
        logger.info(f"Cache HIT for search query: {query}")
        return cached
        
    # Read environment keys
    ZHIHU_SECRET = os.getenv("ZHIHU_ACCESS_SECRET") or os.getenv("ZHIHU_API_KEY")

    # Parse engines option
    engines_list = [e.strip().lower() for e in engines.split(",")] if engines else []

    web_results = []
    zhihu_results = []
    engine_used = None
    
    # 1. Check if only "zhihu" is requested
    if len(engines_list) == 1 and engines_list[0] == "zhihu":
        if not ZHIHU_SECRET:
            return "Error: Zhihu search was requested, but ZHIHU_ACCESS_SECRET or ZHIHU_API_KEY is not configured in environment variables."
        zhihu_results = await search_zhihu(query)
        engine_used = "Zhihu"
        
    # 2. Check if hybrid / all / both requested
    elif "hybrid" in engines_list or "all" in engines_list or ("zhihu" in engines_list and len(engines_list) > 1):
        # Run both in parallel
        web_task = asyncio.create_task(run_general_web_search(query))
        
        if ZHIHU_SECRET:
            zhihu_task = asyncio.create_task(search_zhihu(query))
            web_res_tuple, zhihu_res = await asyncio.gather(web_task, zhihu_task)
            web_results, engine_used = web_res_tuple
            zhihu_results = zhihu_res
        else:
            logger.warning("Zhihu is requested in hybrid search, but ZHIHU_ACCESS_SECRET/ZHIHU_API_KEY is not set. Defaulting to general search only.")
            web_results, engine_used = await web_task
        
    # 3. Else, default web search (with optional forced single engine)
    else:
        TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
        EXA_API_KEY = os.getenv("EXA_API_KEY")
        GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
        GOOGLE_CX = os.getenv("GOOGLE_CX")
        
        forced_engine = None
        for eng in engines_list:
            if eng in ["tavily", "exa", "google", "duckduckgo"]:
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
        elif forced_engine == "google" and GOOGLE_API_KEY and GOOGLE_CX:
            try:
                web_results = await search_google(query, GOOGLE_API_KEY, GOOGLE_CX)
                engine_used = "Google"
            except Exception as e:
                return f"Error: Forced Google search failed: {e}"
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
    
    # Check for blocking patterns
    normalized = text.lower()
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
        if sig in normalized:
            logger.warning(f"Blocking signature detected: '{sig}'")
            return True
            
    return False

def extract_main_content_heuristic(html_content: str, url: str) -> str:
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
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
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
    async with httpx.AsyncClient(timeout=30.0) as client:
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
        pattern = r'[a-zA-Z0-9_]+|[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]'
        words = re.findall(pattern, text.lower())
        return [w for w in words if w not in STOP_WORDS]
        
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

async def fetch_page_content(url: str) -> str:
    SCRAPFLY_API_KEY = os.getenv("SCRAPFLY_API_KEY")
    
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
            run_conf = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
            result = await global_crawler.arun(url=url, config=run_conf)
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
                        crawl4ai_failed = False  # Succeeded execution but blocked
            else:
                err = result.error_message if hasattr(result, "error_message") else "Empty content"
                logger.warning(f"Local Crawl4AI execution returned success=False or empty: {err}")
        except Exception as e_c4ai:
            logger.error(f"Local Crawl4AI execution failed with error: {str(e_c4ai)}")
    else:
        logger.info("Local Crawl4AI is not initialized/available. Skipping local dynamic crawl.")

    # 3. Failover to Scrapfly API if configured
    if SCRAPFLY_API_KEY:
        logger.info(f"Local crawl paths failed/blocked. Failing over to Scrapfly API for: {url}")
        try:
            return await crawl_scrapfly(url, SCRAPFLY_API_KEY)
        except Exception as e_scrapfly:
            logger.error(f"Scrapfly failover also failed: {str(e_scrapfly)}")
            # Return any partial result we got
            if crawl4ai_text:
                logger.info("Returning Crawl4AI text since Scrapfly also failed.")
                return crawl4ai_text
            if not local_failed and local_text:
                logger.info("Returning partially blocked local text since Scrapfly also failed.")
                return local_text
            raise Exception(f"Crawl failed on all local methods and Scrapfly: {str(e_scrapfly)}")
    else:
        logger.warning("Local crawl paths failed/blocked, and Scrapfly API key is not configured.")
        # Return best available local text
        if crawl4ai_text:
            return crawl4ai_text
        if not local_failed and local_text:
            return local_text
        raise Exception("All local crawl methods failed/blocked, and no Scrapfly key configured for failover.")


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

@mcp.tool()
async def crawl_page(url: str, query: str | None = None) -> str:
    """
    Crawl a specific webpage by its URL to extract and read its full textual content in Markdown format.
    
    CRITICAL AGENT INSTRUCTIONS:
    - ALWAYS use this tool when you need to read the content of a specific URL (e.g. from search results, or provided by the user).
    - Do NOT use this tool for general search queries. (Use `search_web` instead).
    - If the page is too long and you only care about a specific topic, provide the `query` parameter. This will use semantic RAG to extract only the top 6 most relevant paragraphs, saving your token context.
    - If you need the entire page content, leave `query` as null/None. Note that if the page is extremely long (>25,000 characters), an outline and preview will be returned instead of the full text to avoid client-side slowness.
    
    Args:
        url: The absolute HTTP/HTTPS URL of the web page to crawl.
        query: Optional. A specific question or topic to extract from the page. If provided, returns only the most relevant snippets. If null, returns the full page content.
        
    Returns:
        Clean Markdown content of the webpage, or extracted semantic snippets if a query was provided.
    """
    global crawl_count
    crawl_count += 1
    
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Error: URL must start with http:// or https://"
        
    markdown_text = crawl_cache.get(url)
    
    if not markdown_text:
        # Check if URL is GitHub and convert it
        github_raw_url = convert_github_url_to_raw(url)
        target_url = github_raw_url if github_raw_url else url
        
        try:
            text = await fetch_page_content(target_url)
            markdown_text = text
        except Exception as e:
            # If it's a GitHub URL defaulted to main and failed with 404/empty, try master branch fallback
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
                
        # Cache the result under the ORIGINAL url so next requests hit the cache
        crawl_cache.set(url, markdown_text)

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
    global global_crawler
    if CRAWL4AI_AVAILABLE:
        try:
            logger.info("Initializing global AsyncWebCrawler in lifespan...")
            browser_conf = BrowserConfig(
                headless=True,
                text_mode=True,
                light_mode=True,
                extra_args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            global_crawler = AsyncWebCrawler(config=browser_conf)
            await global_crawler.start()
            logger.info("Global AsyncWebCrawler successfully started.")
        except Exception as e:
            logger.error(f"Failed to initialize global AsyncWebCrawler: {str(e)}")
            global_crawler = None
    else:
        logger.warning("Crawl4AI is not available, skipping crawler startup in lifespan.")
        
    yield
    
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

                <!-- Search Gateway status card -->
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">搜索引擎网关</span>
                        <i class="fa-solid fa-magnifying-glass card-icon"></i>
                    </div>
                    <div>
                        <div class="card-value" id="search-count">0</div>
                        <div class="card-desc">本次会话累计执行的搜索次数</div>
                    </div>
                    <ul class="card-details-list">
                        <li>
                            <span>活跃搜索引擎</span>
                            <span id="active-engines" style="word-break: break-all; max-width: 170px; text-align: right;">加载中...</span>
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
                        <div class="card-desc">本次会话累计执行的网页抓取次数</div>
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
                    <span class="config-title"><i class="fa-solid fa-cog"></i> 客户端 Agent 连接配置配置助手</span>
                    <div style="display: flex; gap: 0.5rem;">
                        <button class="tab-btn active" onclick="showTab('sse')">原生 SSE 直连 (推荐 - 零依赖)</button>
                        <button class="tab-btn" onclick="showTab('node')">Node.js Stdio 桥接</button>
                        <button class="tab-btn" onclick="showTab('python')">Python Stdio 桥接</button>
                    </div>
                </div>
                <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.8rem;" id="config-desc">
                    无痛直连！如果您的 Agent 客户端（如 Cursor、ModelScope Agent Studio 等）原生支持 SSE 协议，直接填写以下配置即可，完全无需本地下载安装任何依赖：
                </p>
                <div style="font-size: 0.825rem; color: var(--warning-color); border: 1px solid rgba(245, 158, 11, 0.2); background: rgba(245, 158, 11, 0.05); padding: 0.6rem 0.8rem; border-radius: 8px; margin-bottom: 1rem; display: flex; align-items: center; gap: 0.5rem;">
                    <i class="fa-solid fa-triangle-exclamation"></i>
                    <span><strong>提示:</strong> 请将配置中的 <code>YOUR_BEARER_TOKEN</code> 替换为您在 Hugging Face Space 中配置的实际 <code>BEARER_TOKEN</code> 密钥。</span>
                </div>
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
            // Config Templates
            const sseConfig = `{{
  "mcpServers": {{
    "hf-search-crawl-mcp": {{
      "type": "sse",
      "url": "{sse_url}?token={token_val}"
    }}
  }}
}}`;

            const nodeConfig = `{{
  "mcpServers": {{
    "hf-search-crawl-mcp": {{
      "command": "mcp-remote",
      "args": [
        "{sse_url}?token={token_val}"
      ]
    }}
  }}
}}`;

            const pythonConfig = `{{
  "mcpServers": {{
    "hf-search-crawl-mcp": {{
      "command": "python3",
      "args": [
        "-m",
        "mcp.cli.client",
        "{sse_url}?token={token_val}"
      ]
    }}
  }}
}}`;

            let currentTab = 'sse';

            function updateConfigDisplay() {{
                const configPre = document.getElementById('json-config');
                const desc = document.getElementById('config-desc');
                
                if (currentTab === 'sse') {{
                    configPre.textContent = sseConfig;
                    desc.innerHTML = '无痛直连！如果您的 Agent 客户端（如 Cursor、ModelScope Agent Studio 等）原生支持 SSE 协议，直接填写以下配置即可，完全无需本地下载安装任何依赖：';
                }} else if (currentTab === 'node') {{
                    configPre.textContent = nodeConfig;
                    desc.innerHTML = '对于仅支持 Stdio (标准输入输出) 的客户端（如 Claude Desktop），建议在本地全局安装桥接器：<code>npm install -g mcp-remote</code>，以极速运行，免去 npx 每次运行时在线检查的开销：';
                }} else {{
                    configPre.textContent = pythonConfig;
                    desc.innerHTML = '免 Node 环境！对于仅支持 Stdio 的客户端，您可在本地环境执行 <code>pip install mcp</code>，利用 Python SDK 自带的连接器快速桥接：';
                }}
            }}

            function showTab(tab) {{
                currentTab = tab;
                document.querySelectorAll('.tab-btn').forEach(btn => {{
                    btn.classList.remove('active');
                }});
                
                // Highlight active button
                const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => {{
                    if (tab === 'sse') return b.textContent.includes('原生');
                    if (tab === 'node') return b.textContent.includes('Node');
                    return b.textContent.includes('Python');
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
                    document.getElementById('active-engines').textContent = data.active_engines;
                    
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
