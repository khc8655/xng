import sys
import os
import asyncio

sys.path.append("/Users/xk/Documents/mcp")

import app
from app import extract_main_content_heuristic, is_blocked_or_empty, fetch_page_content

mock_html = """
<!DOCTYPE html>
<html>
<head><title>Mock Page</title></head>
<body>
    <header class="site-header">
        <nav class="main-nav">
            <ul>
                <li><a href="/home">Home</a></li>
                <li><a href="/about">About</a></li>
                <li><a href="/contact">Contact</a></li>
            </ul>
        </nav>
    </header>
    
    <div class="container">
        <aside class="sidebar">
            <h3>Recent Posts</h3>
            <ul>
                <li><a href="/post1">Post 1</a></li>
                <li><a href="/post2">Post 2</a></li>
            </ul>
        </aside>
        
        <main class="main-content" id="article-body">
            <article class="post-item">
                <h1>The Future of AI Agents</h1>
                <p>AI agents are transforming how we build software. By using LLMs to make decisions, agents can dynamically plan, execute tools, and reflect on outputs.</p>
                <p>This is the second paragraph of the core content, describing details about agentic coding and paired programming.</p>
            </article>
        </main>
        
        <div class="widget-list ads">
            <p>Buy our course now! Click here!</p>
            <a href="/ad1">Ad link 1</a>
            <a href="/ad2">Ad link 2</a>
        </div>
    </div>
    
    <footer class="site-footer">
        <p>&copy; 2026 AI Agent Playground. All rights reserved.</p>
    </footer>
</body>
</html>
"""

async def test_all():
    print("--- Test 1: Heuristic Readability Extracting ---")
    markdown_out = extract_main_content_heuristic(mock_html, "https://example.com/mock")
    print("Extracted Markdown:")
    print(markdown_out)
    
    # Assertions to ensure main content is extracted and header/footer/sidebar are stripped
    assert "The Future of AI Agents" in markdown_out
    assert "AI agents are transforming" in markdown_out
    assert "About" not in markdown_out          # Nav link stripped
    assert "Recent Posts" not in markdown_out   # Sidebar stripped
    assert "All rights reserved" not in markdown_out # Footer stripped
    print("✅ Test 1 Passed! (Heuristics correctly isolated main content and stripped layout noise)")
    
    print("\n--- Test 2: Blocker Detection ---")
    assert is_blocked_or_empty("") == True
    assert is_blocked_or_empty("   ") == True
    assert is_blocked_or_empty("Short text") == True
    assert is_blocked_or_empty("This is a normal paragraph with enough text length to pass the minimum character count of 150 characters. " * 3) == False
    assert is_blocked_or_empty("This is long text but contains Cloudflare DDoS protection warning. Please enable JS to verify you are human.") == True
    print("✅ Test 2 Passed! (Blocker detection correctly flags empty/short/blocked responses)")
    
    print("\n--- Test 3: fetch_page_content with clean local crawl (No Scrapfly usage) ---")
    # Mock fallback_crawl and crawl_scrapfly
    async def mock_fallback_crawl_clean(url):
        return "This is a clean, long, successfully crawled webpage content that passes all blocking checks. " * 3
        
    scrapfly_called = False
    async def mock_crawl_scrapfly(url, key):
        nonlocal scrapfly_called
        scrapfly_called = True
        return "Scrapfly result"
        
    app.fallback_crawl = mock_fallback_crawl_clean
    app.crawl_scrapfly = mock_crawl_scrapfly
    os.environ["SCRAPFLY_API_KEY"] = "mock_key"
    
    res = await fetch_page_content("https://example.com/clean")
    print("Result:", res[:80])
    assert scrapfly_called == False
    assert "clean, long, successfully crawled" in res
    print("✅ Test 3 Passed! (Clean local crawl does not use Scrapfly credits)")
    
    print("\n--- Test 4: fetch_page_content with blocked local crawl (Scrapfly Failover) ---")
    async def mock_fallback_crawl_blocked(url):
        return "Attention: Cloudflare DDoS Protection page. Please wait..."
        
    app.fallback_crawl = mock_fallback_crawl_blocked
    scrapfly_called = False
    
    res = await fetch_page_content("https://example.com/blocked")
    print("Result:", res)
    assert scrapfly_called == True
    assert res == "Scrapfly result"
    print("✅ Test 4 Passed! (Blocked local crawl successfully triggers Scrapfly failover)")

if __name__ == "__main__":
    asyncio.run(test_all())
