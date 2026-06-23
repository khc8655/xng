import httpx
import json

url = "https://www.python.org"
key = "scp-live-50dd0e01be9743ac9732c0bdac8c3b7d"

def test_scrapfly():
    print("Testing Scrapfly API...")
    params = {
        "key": key,
        "url": url,
        "format": "markdown",
        "only_content": "true"
    }
    
    r = httpx.get("https://api.scrapfly.io/scrape", params=params, timeout=30.0)
    print("Status Code:", r.status_code)
    if r.status_code == 200:
        data = r.json()
        result = data.get("result", {})
        content = result.get("content", "")
        print("Success:", data.get("success"))
        print("Duration:", result.get("duration"))
        print("Antibot Status:", result.get("antibot"))
        print("\nContent Snippet (first 500 chars):")
        print(content[:500])
    else:
        print("Error response:", r.text)

if __name__ == "__main__":
    test_scrapfly()
