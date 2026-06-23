import asyncio
import sys
import os
import time

# Ensure we can import app from the workspace
sys.path.append("/Users/xk/Documents/mcp")

async def main():
    print("Testing Crawl4AI import...")
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
        print("Imports successful!")
    except Exception as e:
        print(f"Import failed: {str(e)}")
        return

    print("Configuring lightweight browser settings...")
    # BrowserConfig controls browser-level behavior
    browser_conf = BrowserConfig(
        headless=True,
        text_mode=True,    # Blocks images, fonts, media
        light_mode=True,   # Disables background extensions/features
        extra_args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
    )
    
    print("Initializing AsyncWebCrawler...")
    crawler = AsyncWebCrawler(config=browser_conf)
    
    # Start the browser
    print("Starting crawler...")
    start_time = time.time()
    await crawler.start()
    print(f"Crawler started in {time.time() - start_time:.2f} seconds.")
    
    try:
        run_conf = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
        
        # Test URL: Use a page that requires JS or a standard web page
        url = "https://news.ycombinator.com"
        print(f"Crawling {url}...")
        
        crawl_start = time.time()
        result = await crawler.arun(url=url, config=run_conf)
        crawl_duration = time.time() - crawl_start
        
        print(f"Crawl finished in {crawl_duration:.2f} seconds.")
        print(f"Success: {result.success}")
        
        if result.success:
            if isinstance(result.markdown, str):
                markdown = result.markdown
            else:
                markdown = result.markdown.raw_markdown if result.markdown and hasattr(result.markdown, 'raw_markdown') else ""
            print(f"Markdown Content Length: {len(markdown)} characters")
            print("\nPreview of first 500 characters:")
            print("-" * 40)
            print(markdown[:500])
            print("-" * 40)
            
            # Simple validation to ensure it's not blocked/empty
            from app import is_blocked_or_empty
            blocked = is_blocked_or_empty(markdown)
            print(f"Is content blocked or empty? {blocked}")
        else:
            print(f"Error Message: {result.error_message if hasattr(result, 'error_message') else 'Unknown error'}")
            
    finally:
        print("Closing crawler...")
        await crawler.close()
        print("Crawler closed.")

if __name__ == "__main__":
    asyncio.run(main())
