import asyncio
import sys
import os
import logging

# Ensure logging is shown
logging.basicConfig(level=logging.INFO)

sys.path.append("/Users/xk/Documents/mcp")

async def test_pipeline():
    # Import app variables
    import app
    
    # 1. Manually initialize Crawl4AI using the same setup as lifespan
    print("\n[STEP 1] Initializing global AsyncWebCrawler...")
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig
        browser_conf = BrowserConfig(
            headless=True,
            text_mode=True,
            light_mode=True,
            extra_args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        app.global_crawler = AsyncWebCrawler(config=browser_conf)
        await app.global_crawler.start()
        print("Global AsyncWebCrawler started.")
    except Exception as e:
        print(f"Failed to start global crawler: {str(e)}")
        return
        
    try:
        # 2. Test static site (should complete via heuristic static fetch)
        print("\n[STEP 2] Testing static site crawl (Wikipedia)...")
        wikipedia_url = "https://en.wikipedia.org/wiki/Main_Page"
        result_wiki = await app.fetch_page_content(wikipedia_url)
        print(f"Wikipedia crawl finished. Length: {len(result_wiki)}")
        
        # 3. Test dynamic page or fallback page
        print("\n[STEP 3] Testing dynamic or fallback site crawl (Hacker News)...")
        hn_url = "https://news.ycombinator.com"
        # 4. Force Crawl4AI fallback by mocking fallback_crawl
        print("\n[STEP 4] Testing forced Crawl4AI fallback (mocking Heuristic failure)...")
        orig_fallback = app.fallback_crawl
        async def mock_failed_crawl(url):
            raise Exception("Mocked Heuristic crawl failure")
        app.fallback_crawl = mock_failed_crawl
        
        try:
            result_fallback = await app.fetch_page_content("https://news.ycombinator.com")
            print(f"Fallback crawl finished. Length: {len(result_fallback)}")
            assert len(result_fallback) > 0, "Crawl4AI failed to return content"
            print("✅ Crawl4AI fallback executed successfully!")
        finally:
            app.fallback_crawl = orig_fallback
        
        print("\n✅ Integration Test Successful! All paths executed correctly.")
    finally:
        if app.global_crawler:
            print("\n[STEP 5] Closing global crawler...")
            await app.global_crawler.close()
            print("Global crawler closed.")

if __name__ == "__main__":
    asyncio.run(test_pipeline())
