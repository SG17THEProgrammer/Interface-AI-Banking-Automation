"""
Quick browser visibility test - run this from src/
python browser_test.py
"""
import sys, os, time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright

print("Testing browser visibility...")
print()

with sync_playwright() as p:

    # Test 1: Default chromium
    print("Test 1: Default Playwright Chromium (headless=False)")
    try:
        b = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--start-maximized"],
            slow_mo=500,
        )
        page = b.new_page()
        page.goto("http://localhost:8080/search")
        print(f"  -> Page title: {page.title()}")
        print("  -> Do you see a Chrome window? (waiting 5s...)")
        time.sleep(5)
        b.close()
        print("  -> Closed.")
    except Exception as e:
        print(f"  -> FAILED: {e}")

    print()

    # Test 2: Use system Chrome if available
    print("Test 2: System Chrome via channel='chrome'")
    try:
        b = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--start-maximized"],
            slow_mo=500,
        )
        page = b.new_page()
        page.goto("http://localhost:8080/search")
        print(f"  -> Page title: {page.title()}")
        print("  -> Do you see a Chrome window? (waiting 5s...)")
        time.sleep(5)
        b.close()
        print("  -> Closed.")
    except Exception as e:
        print(f"  -> FAILED: {e}")

print()
print("Which test showed a visible window? Tell me the result.")