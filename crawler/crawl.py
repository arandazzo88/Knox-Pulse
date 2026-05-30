#!/usr/bin/env python3
"""
Knox Pulse — Weekly Event Crawler
Scrapes visitknoxville.com and insideofknoxville.com
Writes new events to Firestore "crawled_events" collection for admin review.
"""
import os, re, json, hashlib, datetime
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, firestore

# ── Firebase init ──────────────────────────────────────────────────────────────
SA_PATH = os.environ.get("FIREBASE_SA_PATH", "/tmp/sa.json")
cred = credentials.Certificate(SA_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

def event_id(title, date_str=""):
    """Stable dedup ID from title + date."""
    raw = f"{title.lower().strip()}-{date_str}"
    return "crawled_" + hashlib.md5(raw.encode()).hexdigest()[:12]

def already_exists(eid):
    """Skip if already in Firestore (any collection)."""
    return db.collection("crawled_events").document(eid).get().exists

def save_event(evt):
    eid = event_id(evt.get("title",""), evt.get("eventDate",""))
    if already_exists(eid):
        print(f"  skip (exists): {evt.get('title','')[:50]}")
        return
    evt["id"] = eid
    evt["status"] = "pending"
    evt["crawledAt"] = datetime.datetime.utcnow().isoformat()
    db.collection("crawled_events").document(eid).set(evt)
    print(f"  saved: {evt.get('title','')[:60]}")

# ── Crawl visitknoxville.com ───────────────────────────────────────────────────
def crawl_visitknoxville(page):
    print("\n── visitknoxville.com ──")
    events = []
    urls = [
        "https://www.visitknoxville.com/events/this-weekend/",
        "https://www.visitknoxville.com/events/concerts-live-music/",
        "https://www.visitknoxville.com/events/community-events/",
    ]
    for url in urls:
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            # Extract event cards — SimpleView renders cards with class patterns
            cards = page.query_selector_all("article, .listing-card, [class*='event-card'], [class*='listing']")
            print(f"  {url.split('/')[-2]}: {len(cards)} cards found")
            for card in cards[:15]:
                try:
                    title = (card.query_selector("h2,h3,h4,[class*=title]") or {})
                    title = title.inner_text().strip() if hasattr(title, "inner_text") else ""
                    if not title or len(title) < 4: continue
                    link_el = card.query_selector("a")
                    link = link_el.get_attribute("href") if link_el else ""
                    if link and not link.startswith("http"): link = "https://www.visitknoxville.com" + link
                    date_el = card.query_selector("[class*=date],[class*=Date],time")
                    date_str = date_el.inner_text().strip() if date_el else ""
                    desc_el = card.query_selector("p,[class*=desc],[class*=excerpt]")
                    desc = desc_el.inner_text().strip() if desc_el else ""
                    img_el = card.query_selector("img")
                    img = img_el.get_attribute("src") if img_el else ""
                    evt = {
                        "title": title, "description": desc[:400],
                        "location": "Knoxville, TN", "source": "visitknoxville.com",
                        "sourceUrl": link, "imageUrl": img,
                        "eventDate": date_str, "category": "Festivals & Events",
                        "eventType": "one-time", "neighborhood": "Downtown",
                        "costScale": "Free", "indoorOutdoor": "Both",
                        "ageRestrictions": "All Ages",
                    }
                    events.append(evt)
                    save_event(evt)
                except Exception as e:
                    print(f"  card error: {e}")
        except Exception as e:
            print(f"  page error {url}: {e}")
    return events

# ── Crawl insideofknoxville.com ───────────────────────────────────────────────
def crawl_insideknoxville(page):
    print("\n── insideofknoxville.com ──")
    events = []
    try:
        page.goto("https://insideofknoxville.com/", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(4000)
        # Handle Cloudflare challenge if present
        if "Just a moment" in page.title():
            print("  Cloudflare challenge — waiting 10s...")
            page.wait_for_timeout(10000)
        # Get article links
        links = page.query_selector_all("article a, .post-title a, h2 a, h3 a")
        seen = set()
        for link_el in links[:30]:
            try:
                href = link_el.get_attribute("href") or ""
                text = link_el.inner_text().strip()
                if not href or href in seen: continue
                seen.add(href)
                # Only grab event-related posts
                keywords = ["event","concert","festival","show","exhibit","open","class","tour","market","music","art","food"]
                if not any(k in text.lower() for k in keywords): continue
                events.append({
                    "title": text, "description": "",
                    "location": "Knoxville, TN", "source": "insideofknoxville.com",
                    "sourceUrl": href, "category": "Festivals & Events",
                    "eventType": "one-time", "neighborhood": "Downtown",
                    "costScale": "Free", "indoorOutdoor": "Both",
                    "ageRestrictions": "All Ages",
                })
                save_event(events[-1])
            except Exception as e:
                print(f"  link error: {e}")
        print(f"  {len(events)} event-related posts found")
    except Exception as e:
        print(f"  page error: {e}")
    return events

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"Knox Pulse Crawler — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US", timezone_id="America/New_York",
            viewport={"width":1280,"height":800}
        )
        page = ctx.new_page()
        # Hide automation signals
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        vk = crawl_visitknoxville(page)
        ik = crawl_insideknoxville(page)
        browser.close()
    total = len(vk) + len(ik)
    print(f"\nDone — {total} events processed. Check admin panel > Crawled tab to review.")

if __name__ == "__main__":
    main()
