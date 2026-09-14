"""
Minimal debug - bypasses replay.py entirely, launches browser directly
Drop in src/ and run: python debug_hitl.py
"""
import sys, os, time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(__file__))

from playwright.sync_api import sync_playwright

print("Step 1: about to launch browser...")

with sync_playwright() as pw:
    print("Step 2: sync_playwright context entered")
    
    browser = pw.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--start-maximized"],
        slow_mo=500,
    )
    print("Step 3: browser launched")
    
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    print("Step 4: page created")
    
    page.goto("http://localhost:8080/search")
    print(f"Step 5: navigated - title: {page.title()}")
    print("        -> Do you see the browser window RIGHT NOW?")
    
    # Type member ID
    page.get_by_label("Member ID").fill("100001")
    print("Step 6: typed member ID")
    print("        -> Can you see 100001 in the search box?")
    
    print("\nStep 7: PAUSING for 30 seconds - browser should stay open")
    print("        This simulates what HITL does (waits for human input)")
    time.sleep(30)
    
    print("Step 8: closing")
    browser.close()

print("Done.")