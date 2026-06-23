import subprocess
import sys
import os

SCRATCH_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXEC = sys.executable

# Catalog of tests
TEST_CASES = {
    "1. Heuristics & Blocker Detection": {
        "file": "test_crawling_heuristics.py",
        "description": "Tests the local HTML readability parsing logic and Cloudflare/DDoS blocker signature detection."
    },
    "2. Crawl4AI Browser Rendering": {
        "file": "test_crawl4ai_local.py",
        "description": "Tests local dynamic crawling using Crawl4AI in lightweight text_mode & light_mode."
    },
    "3. Crawl Pipeline Cascade (Local)": {
        "file": "test_pipeline_local.py",
        "description": "Tests the complete crawling pipeline fallback sequence: static heuristics -> Crawl4AI browser."
    },
    "4. Search routing & Zhihu API": {
        "file": "test_zhihu_local.py",
        "description": "Tests search engine routing logic, hybrid query merging, and mock Zhihu integration."
    },
    "5. Scrapfly Remote API (Optional)": {
        "file": "test_scrapfly.py",
        "description": "Tests live connection to the remote Scrapfly Scraping API (requires active internet)."
    }
}

def run_test_case(name, info):
    filepath = os.path.join(SCRATCH_DIR, info["file"])
    if not os.path.exists(filepath):
        return "MISSING", f"File not found: {filepath}"
    
    print(f"\n==========================================")
    print(f"▶️ Running: {name}")
    print(f"   Description: {info['description']}")
    print(f"   Command: {PYTHON_EXEC} scratch/{info['file']}")
    print(f"==========================================")
    
    try:
        # Run test as a separate process to avoid import conflicts or env pollution
        res = subprocess.run([PYTHON_EXEC, filepath], capture_output=True, text=True, timeout=60)
        if res.returncode == 0:
            print(res.stdout)
            return "PASSED", ""
        else:
            print("STDOUT:")
            print(res.stdout)
            print("STDERR:")
            print(res.stderr)
            return "FAILED", f"Exit code {res.returncode}"
    except subprocess.TimeoutExpired:
        return "TIMEOUT", "Process timed out after 60s"
    except Exception as e:
        return "ERROR", str(e)

def main():
    print("==================================================")
    print("      MCP Server Manual Modules Test Runner       ")
    print("==================================================")
    
    results = {}
    
    # If arguments are passed, run only specific tests
    target_tests = TEST_CASES.keys()
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ["--list", "-l"]:
            print("\nAvailable test modules:")
            for idx, key in enumerate(TEST_CASES.keys(), 1):
                print(f"  {idx}. {key} ({TEST_CASES[key]['file']})")
            return
        
        # Filter based on search query
        target_tests = [k for k in TEST_CASES.keys() if arg in k.lower() or arg in TEST_CASES[k]["file"].lower()]
        if not target_tests:
            print(f"\n❌ No tests matched search query: '{sys.argv[1]}'")
            return
            
    for name in target_tests:
        status, detail = run_test_case(name, TEST_CASES[name])
        results[name] = (status, detail)
        
    print("\n" + "=" * 50)
    print("📊 FINAL MODULES REPORT CARD")
    print("=" * 50)
    
    all_passed = True
    for name, (status, detail) in results.items():
        symbol = "✅" if status == "PASSED" else "❌"
        detail_str = f" ({detail})" if detail else ""
        print(f"  {symbol}  {name:<35} : {status}{detail_str}")
        if status != "PASSED":
            all_passed = False
            
    print("=" * 50)
    if all_passed:
        print("🎉 ALL MODULE TESTS PASSED SUCCESSFULLY!")
    else:
        print("⚠️ SOME MODULE TESTS FAILED OR SKIPPED. Please check the logs above.")
    print("=" * 50)

if __name__ == "__main__":
    main()
