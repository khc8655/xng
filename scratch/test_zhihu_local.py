import os
import asyncio
import sys

# Add the workspace directory to the path so we can import app
sys.path.append("/Users/xk/Documents/mcp")

# Mock the environment variables to control flow
os.environ["TAVILY_API_KEY"] = ""  # Force DuckDuckGo fallback for web search
os.environ["EXA_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["GOOGLE_CX"] = ""
os.environ["ZHIHU_ACCESS_SECRET"] = ""  # No Zhihu key initially

import app
from app import search_web

# Mock general web search to return consistent dummy results
async def mock_general_web_search(query):
    return [
        {
            "title": f"Mock Web Result for {query}",
            "url": "https://example.com/web",
            "snippet": f"This is a mocked web search result for query: {query}",
            "engine": "DuckDuckGo"
        }
    ], "DuckDuckGo"

app.run_general_web_search = mock_general_web_search

async def test_all():
    print("--- Test 1: Zhihu search requested but no secret set ---")
    res = await search_web(query="Python", engines="Zhihu")
    print("Result:", res)
    assert "Error: Zhihu search was requested, but ZHIHU_ACCESS_SECRET or ZHIHU_API_KEY is not configured" in res
    print("✅ Test 1 Passed!")

    print("\n--- Test 2: Hybrid search requested but no secret set (should fallback to web search only) ---")
    res = await search_web(query="Python 3.11", engines="hybrid")
    print("First 300 chars of result:")
    print(res[:300])
    assert "DuckDuckGo" in res
    assert "Mock Web Result for Python 3.11" in res
    print("✅ Test 2 Passed!")

    print("\n--- Test 3: Standard web search with engines=None ---")
    res = await search_web(query="Python 3.12")
    print("First 300 chars of result:")
    print(res[:300])
    assert "DuckDuckGo" in res
    assert "Mock Web Result for Python 3.12" in res
    print("✅ Test 3 Passed!")

    print("\n--- Test 4: Mocked Zhihu + Web hybrid search integration ---")
    # Monkeypatch search_zhihu to return a mock list
    async def mock_search_zhihu(query, count=5):
        return [
            {
                "title": "如何评价 Python 3.12 版本的发布？",
                "url": "https://www.zhihu.com/question/123456",
                "snippet": "Python 3.12 引入了更快的解释器和更好的报错提示...",
                "engine": "Zhihu (upvotes: 100 | comments: 23)"
            }
        ]
    app.search_zhihu = mock_search_zhihu
    # Set the secret so hybrid doesn't skip it
    os.environ["ZHIHU_ACCESS_SECRET"] = "dummy_secret"
    
    res = await search_web(query="Python 3.12", engines="hybrid")
    print("Result:")
    print(res)
    assert "Zhihu (upvotes: 100 | comments: 23)" in res
    assert "DuckDuckGo" in res
    print("✅ Test 4 Passed!")

if __name__ == "__main__":
    asyncio.run(test_all())
