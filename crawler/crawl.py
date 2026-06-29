#!/usr/bin/env python3
"""
Knox Pulse — Weekly Event Crawler
Scrapes 15 Knoxville event sources and merges new events into
data/listings.json — the single source of truth the site loads at runtime.
The CI workflow commits the updated file every Wednesday at 3pm ET.

Run: python3 crawler/crawl.py
Env: LISTINGS_FILE=/path/to/data/listings.json  (default: ../data/listings.json)
     STATE_FILE=/path/to/scrape-state.json       (default: /tmp/scrape-state.json)
     TICKETMASTER_API_KEY=<key>                  (free key from developer.ticketmaster.com)
"""

import os
import re
import json
import hashlib
import datetime
import urllib.parse
import time
import sys

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Configuration ─────────────────────────────────────────────────────────────

# data/listings.json is the single source of truth for the site (curated
# listings + dated events). New crawled events are merged into it; the CI
# workflow then commits the updated file.
LISTINGS_FILE = os.environ.get(
    "LISTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "listings.json"),
)
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/scrape-state.json")

CATEGORY_IMAGES = {
    "Live Music": "https://wsrv.nl/?url=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2Fb%2Fb8%2FGuitar_1.jpg%2F800px-Guitar_1.jpg&w=800&q=80",
    "Sports & Recreation": "https://wsrv.nl/?url=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2F0%2F05%2FFootball_game_-_panoramic.jpg%2F800px-Football_game_-_panoramic.jpg&w=800&q=80",
    "Outdoor & Nature": "https://wsrv.nl/?url=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2F0%2F05%2FHouse-mountain-outcrop-tn1.jpg%2F800px-House-mountain-outcrop-tn1.jpg&w=800&q=80",
    "Festivals & Events": "https://wsrv.nl/?url=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2F0%2F0f%2FMarket_Square_SA2.JPG%2F800px-Market_Square_SA2.JPG&w=800&q=80",
    "Arts & Museums": "https://wsrv.nl/?url=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2F7%2F7b%2FClingmans_Dome_from_Andrews_Bald.jpg%2F800px-Clingmans_Dome_from_Andrews_Bald.jpg&w=800&q=80",
}
DEFAULT_IMAGE = "https://wsrv.nl/?url=https%3A%2F%2Fupload.wikimedia.org%2Fwikipedia%2Fcommons%2Fthumb%2Fb%2Fb2%2FKnoxville_TN_skyline.jpg%2F800px-Knoxville_TN_skyline.jpg&w=800&q=80"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Local data store (single source of truth) ────────────────────────────────

_listings = []        # full list currently in data/listings.json
_existing_ids = set()  # ids already present, for dedup
_new_count = 0         # number of new events added this run


def load_listings():
    """Load data/listings.json into memory so we can dedup and append."""
    global _listings, _existing_ids
    try:
        with open(LISTINGS_FILE, encoding="utf-8") as f:
            _listings = json.load(f)
    except FileNotFoundError:
        _listings = []
    _existing_ids = {l.get("id") for l in _listings if l.get("id")}
    print(f"Loaded {len(_listings)} entries from {LISTINGS_FILE}")


def write_listings():
    """Write the merged list back to data/listings.json."""
    with open(LISTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(_listings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Wrote {len(_listings)} entries to {LISTINGS_FILE} (+{_new_count} new this run)")


# ── State management (incremental scraping) ───────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def content_hash(text):
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


# ── ID / dedup ─────────────────────────────────────────────────────────────────

# Backstop: known-bad event IDs that must never be (re)added — e.g. out-of-town
# events a source mislabeled as Knoxville. Even if a crawler regresses, these
# are blocked at save time.
BLOCKLIST_IDS = {
    "crawled_dd5da89cba66", "crawled_ea17cc2aec4d",  # Don Toliver (LA)
    "crawled_cdcb76316a32", "crawled_7aeba4c42c2d", "crawled_95fc1e685087",
    "crawled_cb871f2fb139", "crawled_3904e64e42c9", "crawled_5e1df5dae9dc",
    "crawled_c9dfe7f15fec", "crawled_9d8634feed1b",  # LA Sparks x8
    "crawled_c0a73fa4bfcc",  # Diljit Dosanjh (LA)
    "crawled_431d7764fdb0",  # Summer Walker (LA)
    "crawled_1fdedd340a20",  # Kid Cudi (LA)
    "crawled_df466ac50eab", "crawled_856b9c9e4ad2",  # Olivia Dean (LA)
    "crawled_cbb2e482c330", "crawled_b3274658d6eb", "crawled_5958f8c3c3ef",  # Monster Jam (LA)
    "crawled_d291eb2e919d", "crawled_7717152503af",  # Megan Moroney (LA)
    "crawled_4dca891d2cfa",  # Benson Boone (LA)
}


def event_id(title, date_str=""):
    """Stable dedup ID: crawled_ + first 12 chars of MD5(title+date)."""
    raw = title.lower().strip() + date_str
    return "crawled_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def already_exists(eid):
    return eid in _existing_ids


def save_event(evt):
    global _new_count
    eid = evt.get("id") or event_id(evt.get("title", ""), evt.get("eventDate", ""))
    if eid in BLOCKLIST_IDS:
        print(f"    skip (blocklisted): {evt.get('title', '')[:55]}")
        return False
    if already_exists(eid):
        print(f"    skip (exists): {evt.get('title', '')[:55]}")
        return False
    evt["id"] = eid
    _listings.append(evt)
    _existing_ids.add(eid)
    _new_count += 1
    print(f"    saved: {evt.get('title', '')[:60]}")
    return True


# ── Field-derivation helpers ───────────────────────────────────────────────────

def derive_cost_scale(text):
    """Free / $ / $$ / $$$ from price string."""
    if not text:
        return "Free"
    t = text.lower()
    if "free" in t or "no cost" in t or "no charge" in t:
        return "Free"
    numbers = re.findall(r"\$?\s*(\d+(?:\.\d+)?)", t)
    if not numbers:
        return "Free"
    try:
        price = max(float(n) for n in numbers)
        if price <= 15:
            return "$"
        if price <= 40:
            return "$$"
        return "$$$"
    except ValueError:
        return "$"


def derive_time_of_day(time_str):
    """Morning / Afternoon / Evening from time string."""
    if not time_str:
        return "Evening"
    t = time_str.lower()
    # Look for explicit am/pm patterns
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t)
    if match:
        hour = int(match.group(1))
        meridiem = match.group(3)
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour < 12:
            return "Morning"
        if hour < 17:
            return "Afternoon"
        return "Evening"
    if "morning" in t or "breakfast" in t or "brunch" in t:
        return "Morning"
    if "afternoon" in t or "lunch" in t or "noon" in t:
        return "Afternoon"
    return "Evening"


NEIGHBORHOOD_KEYWORDS = {
    "Downtown": [
        "market square", "gay street", "old city", "world's fair", "convention center",
        "civic auditorium", "main street", "summit", "henley", "clinch", "downtown",
        "mill & mine", "mill and mine", "bijou", "tennessee theatre", "barley",
        "jackson avenue", "jackson ave",
    ],
    "Old City": ["old city", "jackson avenue", "jackson ave"],
    "North Knoxville": [
      "north knoxville", "north knox", "broadway", "central avenue", "fountain city",
      "k-town", "dutch valley",
    ],
    "South Knoxville": [
        "south knoxville", "south knox", "ijams", "island home", "sevier avenue",
        "sevier ave", "suttree",
    ],
    "Fort Sanders": ["fort sanders", "the strip", "cumberland avenue", "cumberland ave", "university"],
    "West Knoxville": ["west knoxville", "west knox", "cedar bluff", "farragut", "turkey creek"],
    "East Knoxville": ["east knoxville", "east knox", "magnolia avenue", "magnolia ave"],
    "Bearden": ["bearden", "kingston pike", "homberg"],
    "Karns": ["karns"],
}


def derive_neighborhood(text):
    t = text.lower()
    for neighborhood, keywords in NEIGHBORHOOD_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return neighborhood
    return "Downtown"  # default for Knoxville events


CATEGORY_KEYWORDS = {
    "Live Music": [
        "concert", "live music", "band", "musician", "singer", "guitar", "jazz",
        "bluegrass", "orchestra", "symphony", "choir", "opera", "folk", "blues",
        "rap", "hip hop", "country music", "open mic", "karaoke",
    ],
    "Sports & Recreation": [
        "game", "match", "tournament", "race", "run", "marathon", "5k", "10k",
        "triathlon", "yoga", "fitness", "workout", "sport", "baseball", "basketball",
        "soccer", "football", "hockey", "ice skating", "cycling", "bike",
        "smokies", "icebears", "tennis", "golf",
    ],
    "Outdoor & Nature": [
        "hike", "hiking", "trail", "nature", "outdoor", "kayak", "canoe", "camping",
        "birding", "birdwatch", "botanic", "garden", "park", "river",
        "great smoky", "ijams", "appalachian",
    ],
    "Arts & Museums": [
        "art", "gallery", "museum", "exhibit", "exhibition", "theater", "theatre",
        "film", "movie", "cinema", "photography", "sculpture", "dance", "ballet",
        "play", "performance", "improv", "comedy", "stand-up", "standup",
    ],
    "Food & Drink": [
        "food", "drink", "beer", "wine", "cocktail", "tasting", "brewery", "distillery",
        "restaurant", "dinner", "brunch", "breakfast", "lunch", "culinary", "chef",
        "farm to table", "market", "farmer",
    ],
    "Family & Kids": [
        "family", "kids", "children", "child", "toddler", "baby", "storytime",
        "puppet", "youth", "teen", "school",
    ],
    "Community & Social": [
        "community", "volunteer", "charity", "nonprofit", "benefit", "fundraiser",
        "social", "networking", "meetup", "club",
    ],
    "Festivals & Events": [
        "festival", "fair", "parade", "celebration", "holiday", "carnival",
        "expo", "market", "street party",
    ],
}


def derive_category(title, description=""):
    combined = (title + " " + description).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                return category
    return "Festivals & Events"


def derive_indoor_outdoor(title, description="", venue=""):
    combined = (title + " " + description + " " + venue).lower()
    outdoor_kw = [
        "outdoor", "outside", "park", "trail", "river", "lake", "field", "stadium",
        "amphitheater", "amphitheatre", "hike", "hiking", "kayak", "canoe", "garden",
        "patio", "rooftop", "open air", "open-air",
    ]
    indoor_kw = [
        "indoor", "inside", "theatre", "theater", "museum", "gallery", "bar",
        "restaurant", "auditorium", "ballroom", "arena",
    ]
    out_score = sum(1 for kw in outdoor_kw if kw in combined)
    in_score = sum(1 for kw in indoor_kw if kw in combined)
    if out_score > in_score:
        return "Outdoor"
    if in_score > out_score:
        return "Indoor"
    return "Both"


def derive_age_restrictions(text):
    t = text.lower()
    if "21+" in t or "21 and over" in t or "21 &amp; over" in t:
        return "21+"
    if "18+" in t or "18 and over" in t:
        return "18+"
    return "All Ages"


def derive_stroller_friendly(category, indoor_outdoor, title="", description=""):
    combined = (title + " " + description).lower()
    bar_kw = ["bar", "brewery", "distillery", "21+", "alcohol", "beer", "wine", "cocktail"]
    if any(kw in combined for kw in bar_kw):
        return False
    if category in ("Outdoor & Nature", "Family & Kids"):
        return True
    if indoor_outdoor == "Indoor" and category in ("Arts & Museums", "Community & Social"):
        return True
    return False


def derive_days_of_week(text):
    """Return [0-6] array for recurring events (Mon=0), [] for one-time."""
    t = text.lower()
    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    days = []
    for day, num in day_map.items():
        if day in t:
            days.append(num)
    if "weekday" in t or "weekdays" in t:
        days = [0, 1, 2, 3, 4]
    if "weekend" in t or "weekends" in t:
        days = list(set(days + [5, 6]))
    return sorted(set(days))


def derive_season(text, date_str=""):
    t = (text + " " + date_str).lower()
    if "year-round" in t or "year round" in t or "monthly" in t or "weekly" in t:
        return "Year-round"
    if "summer" in t or "june" in t or "july" in t or "august" in t:
        return "Summer"
    if "spring" in t or "march" in t or "april" in t or "may" in t:
        return "Spring"
    if "fall" in t or "autumn" in t or "october" in t or "november" in t or "september" in t:
        return "Fall"
    if "winter" in t or "december" in t or "january" in t or "february" in t:
        return "Winter"
    # Try to infer from current date if no clues
    month = datetime.datetime.utcnow().month
    if month in (6, 7, 8):
        return "Summer"
    if month in (3, 4, 5):
        return "Spring"
    if month in (9, 10, 11):
        return "Fall"
    return "Winter"


def wrap_image(url):
    """Wrap an image URL with wsrv.nl proxy."""
    if not url:
        return ""
    if "wsrv.nl" in url:
        return url
    return "https://wsrv.nl/?url=" + urllib.parse.quote(url, safe="") + "&w=800&q=80"


def category_image(category):
    return CATEGORY_IMAGES.get(category, DEFAULT_IMAGE)


def build_event(title, date_str="", description="", location="Knoxville, TN",
                venue="", source="", source_url="", image="", price_text="",
                time_str=""):
    """Build a fully normalized event dict."""
    category = derive_category(title, description)
    indoor_outdoor = derive_indoor_outdoor(title, description, venue)
    cost_scale = derive_cost_scale(price_text)
    time_of_day = derive_time_of_day(time_str or date_str)
    neighborhood = derive_neighborhood(venue + " " + location + " " + source_url)
    age_restrictions = derive_age_restrictions(title + " " + description)
    stroller_friendly = derive_stroller_friendly(category, indoor_outdoor, title, description)
    days_of_week = derive_days_of_week(title + " " + description)
    event_type = "recurring" if days_of_week else "one-time"
    season = derive_season(title + " " + description, date_str)

    # Image: use source image if available, else category default
    if image:
        final_image = wrap_image(image)
    else:
        final_image = category_image(category)

    eid = event_id(title, date_str)
    recurrence_rule = {"type": "weekly", "days": days_of_week} if event_type == "recurring" and days_of_week else None

    # Shape matches the unified data/listings.json event schema so crawled
    # events render identically to curated ones.
    return {
        "id": eid,
        "title": title,
        "category": category,
        "location": location or "Knoxville, TN",
        "venueName": venue or "",
        "description": description[:500] if description else "",
        "indoorOutdoor": indoor_outdoor,
        "ageRestrictions": age_restrictions,
        "timeOfDay": time_of_day,
        "costScale": cost_scale,
        "price": price_text or "",
        "season": season,
        "emoji": "📍",
        "eventType": event_type,
        "eventDate": date_str,
        "eventEndDate": "",
        "daysOfWeek": days_of_week,
        "startTime": time_str or "",
        "endTime": "",
        "recurrenceRule": recurrence_rule,
        "tags": [],
        "neighborhood": neighborhood,
        "lat": None,
        "lng": None,
        "image": final_image,
        "strollerFriendly": stroller_friendly,
        "phone": "",
        "website": source_url or "",
        "email": "",
        "instagram": "",
    }


# ── requests-based helper ──────────────────────────────────────────────────────

def soup_get(url, timeout=20):
    """Fetch URL with requests and return BeautifulSoup, or None on error."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        print(f"    requests error {url}: {e}")
        return None


def first_img(tag, base_url=""):
    """Extract first usable image src from a BeautifulSoup tag."""
    if not tag:
        return ""
    img = tag.find("img")
    if not img:
        return ""
    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
    if src and not src.startswith("http") and base_url:
        src = urllib.parse.urljoin(base_url, src)
    return src if src.startswith("http") else ""


# ── Source crawlers ────────────────────────────────────────────────────────────

# 1. visitknoxville.com
def crawl_visitknoxville(page, state):
    source = "visitknoxville.com"
    print(f"\n── {source} ──")
    urls = [
        "https://www.visitknoxville.com/events/",
        "https://www.visitknoxville.com/events/concerts-live-music/",
        "https://www.visitknoxville.com/events/community-events/",
        "https://www.visitknoxville.com/events/outdoor-recreation/",
    ]
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    for url in urls:
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)
            cards = page.query_selector_all(
                "article, .listing-card, [class*='event-card'], [class*='listing']"
            )
            print(f"  {url.split('/')[-2] or 'events'}: {len(cards)} cards")
            for card in cards[:20]:
                try:
                    title_el = card.query_selector("h2,h3,h4,[class*=title]")
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title or len(title) < 4:
                        continue

                    link_el = card.query_selector("a")
                    link = link_el.get_attribute("href") if link_el else ""
                    if link and not link.startswith("http"):
                        link = "https://www.visitknoxville.com" + link

                    date_el = card.query_selector("[class*=date],[class*=Date],time")
                    date_str = date_el.inner_text().strip() if date_el else ""

                    desc_el = card.query_selector("p,[class*=desc],[class*=excerpt]")
                    desc = desc_el.inner_text().strip() if desc_el else ""

                    img_el = card.query_selector("img")
                    img = img_el.get_attribute("src") or "" if img_el else ""

                    chash = content_hash(title + date_str)
                    if chash in seen_hashes:
                        continue
                    seen_hashes.append(chash)

                    evt = build_event(
                        title=title, date_str=date_str, description=desc,
                        source=source, source_url=link, image=img,
                    )
                    if save_event(evt):
                        saved += 1
                except Exception as e:
                    print(f"    card error: {e}")
        except PWTimeout:
            print(f"  timeout: {url}")
        except Exception as e:
            print(f"  page error {url}: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 2. everythingknoxville.com
def crawl_everythingknoxville(state):
    source = "everythingknoxville.com"
    print(f"\n── {source} ──")
    base = "https://www.everythingknoxville.com"
    urls = [
        f"{base}/events/",
        f"{base}/events/concerts/",
        f"{base}/events/community/",
    ]
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    for url in urls:
        soup = soup_get(url)
        if not soup:
            continue
        cards = soup.select("article, .eventlist-event, .event-card, [class*='event']")
        print(f"  {url}: {len(cards)} cards")
        for card in cards[:20]:
            try:
                title_el = card.find(["h1", "h2", "h3", "h4"], class_=re.compile(r"title|heading|name", re.I))
                if not title_el:
                    title_el = card.find(["h2", "h3"])
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 4:
                    continue

                link_el = card.find("a", href=True)
                link = link_el["href"] if link_el else ""
                if link and not link.startswith("http"):
                    link = base + link

                date_el = card.find(["time", "span", "div"], class_=re.compile(r"date|time", re.I))
                date_str = date_el.get_text(strip=True) if date_el else ""

                desc_el = card.find(["p", "div"], class_=re.compile(r"desc|excerpt|summary", re.I))
                desc = desc_el.get_text(strip=True) if desc_el else ""

                img = first_img(card, base)

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str, description=desc,
                    source=source, source_url=link, image=img,
                )
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 3. legacyparks.org/calendar
def crawl_legacyparks(page, state):
    source = "legacyparks.org"
    print(f"\n── {source} ──")
    url = "https://www.legacyparks.org/calendar/"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        cards = page.query_selector_all(
            ".tribe-events-calendar-list__event, .tribe-event, article, [class*='event']"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.query_selector("h2,h3,[class*=title]")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 4:
                    continue

                link_el = card.query_selector("a")
                link = link_el.get_attribute("href") if link_el else ""

                date_el = card.query_selector("time,[class*=date],[class*=start]")
                date_str = date_el.inner_text().strip() if date_el else ""

                desc_el = card.query_selector("p,[class*=description],[class*=excerpt]")
                desc = desc_el.inner_text().strip() if desc_el else ""

                img_el = card.query_selector("img")
                img = img_el.get_attribute("src") or "" if img_el else ""

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str, description=desc,
                    venue="Legacy Parks", location="Knoxville, TN",
                    source=source, source_url=link, image=img,
                )
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 4. 865running.com
def crawl_865running(state):
    source = "865running.com"
    print(f"\n── {source} ──")
    base = "https://www.865running.com"
    urls = [f"{base}/races/", f"{base}/events/"]
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    for url in urls:
        soup = soup_get(url)
        if not soup:
            continue
        cards = soup.select(
            ".race-item, .event-item, article, .entry, [class*='race'], [class*='event']"
        )
        print(f"  {url}: {len(cards)} items")
        for card in cards[:20]:
            try:
                title_el = card.find(["h1", "h2", "h3", "h4"])
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 4:
                    continue

                link_el = card.find("a", href=True)
                link = link_el["href"] if link_el else ""
                if link and not link.startswith("http"):
                    link = base + link

                date_el = card.find(["time", "span", "div"], class_=re.compile(r"date|when", re.I))
                date_str = date_el.get_text(strip=True) if date_el else ""

                price_el = card.find(["span", "div", "p"], class_=re.compile(r"price|cost|fee|reg", re.I))
                price_text = price_el.get_text(strip=True) if price_el else ""

                img = first_img(card, base)

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    description=f"Running event in Knoxville, TN. {title}",
                    source=source, source_url=link, image=img,
                    price_text=price_text,
                )
                # Override category for running events
                evt["category"] = "Sports & Recreation"
                evt["indoorOutdoor"] = "Outdoor"
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Sports & Recreation"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    item error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 5. milb.com/knoxville (HOME games only)
def crawl_smokies_baseball(page, state):
    source = "milb.com/knoxville"
    print(f"\n── {source} (HOME games only) ──")
    url = "https://www.milb.com/knoxville/schedule"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(5000)
        # Games with "vs" are home games; "@" means away
        game_els = page.query_selector_all(
            ".schedule-game, [class*='schedule-item'], [class*='game'], tr.schedule"
        )
        print(f"  found {len(game_els)} game elements")
        home_count = 0
        for el in game_els[:60]:
            try:
                text = el.inner_text()
                # Home games contain "vs" not "@"
                if " vs " not in text.lower() and "vs." not in text.lower():
                    continue
                home_count += 1

                # Extract date
                date_el = el.query_selector("time,[class*=date],[class*=Date]")
                date_str = date_el.inner_text().strip() if date_el else ""
                if not date_str:
                    # Try to extract from text
                    date_match = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}", text)
                    date_str = date_match.group(0) if date_match else ""

                # Extract opponent
                opp_match = re.search(r"vs\.?\s+(.+?)(?:\n|$|\|)", text, re.I)
                opponent = opp_match.group(1).strip() if opp_match else "Visiting Team"

                title = f"Tennessee Smokies vs {opponent}"

                # Extract time
                time_match = re.search(r"\d{1,2}:\d{2}\s*[ap]m", text, re.I)
                time_str = time_match.group(0) if time_match else "7:00 pm"

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    description=f"Tennessee Smokies Double-A baseball home game at Smokies Stadium. {title}.",
                    venue="Smokies Stadium", location="3540 Line Dr, Kodak, TN 37764",
                    source=source, source_url=url,
                    time_str=time_str, price_text="$10-$20",
                )
                evt["category"] = "Sports & Recreation"
                evt["image"] = CATEGORY_IMAGES["Sports & Recreation"]
                evt["neighborhood"] = "Downtown"  # closest major area
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    game error: {e}")
        print(f"  found {home_count} home games")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 6. knoxvillecoliseum.com/events
def crawl_knoxville_coliseum(page, state):
    source = "knoxvillecoliseum.com"
    print(f"\n── {source} ──")
    url = "https://www.knoxvillecoliseum.com/events"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        cards = page.query_selector_all(
            ".event-card, .event-listing, article, [class*='event'], [class*='show']"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.query_selector("h2,h3,h4,[class*=title],[class*=name]")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 4:
                    continue

                link_el = card.query_selector("a")
                link = link_el.get_attribute("href") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.knoxvillecoliseum.com" + link

                date_el = card.query_selector("time,[class*=date],[class*=Date]")
                date_str = date_el.inner_text().strip() if date_el else ""

                img_el = card.query_selector("img")
                img = img_el.get_attribute("src") or "" if img_el else ""

                price_el = card.query_selector("[class*=price],[class*=ticket],[class*=cost]")
                price_text = price_el.inner_text().strip() if price_el else ""

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue="Knoxville Civic Coliseum", location="500 Howard Baker Jr Ave, Knoxville, TN",
                    source=source, source_url=link, image=img, price_text=price_text,
                )
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 7. themillandmine.com/events
def crawl_mill_and_mine(page, state):
    source = "themillandmine.com"
    print(f"\n── {source} ──")
    url = "https://www.themillandmine.com/events"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        cards = page.query_selector_all(
            ".event, .event-card, article, [class*='show'], [class*='event']"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.query_selector("h2,h3,h4,[class*=title]")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 4:
                    continue

                link_el = card.query_selector("a")
                link = link_el.get_attribute("href") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.themillandmine.com" + link

                date_el = card.query_selector("time,[class*=date]")
                date_str = date_el.inner_text().strip() if date_el else ""

                img_el = card.query_selector("img")
                img = img_el.get_attribute("src") or "" if img_el else ""

                price_el = card.query_selector("[class*=price],[class*=ticket]")
                price_text = price_el.inner_text().strip() if price_el else ""

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue="The Mill & Mine", location="227 W Depot Ave, Knoxville, TN",
                    source=source, source_url=link, image=img, price_text=price_text,
                )
                evt["category"] = "Live Music"
                evt["neighborhood"] = "Downtown"
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Live Music"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 8. barleysknoxville.com/events
def crawl_barleys(page, state):
    source = "barleysknoxville.com"
    print(f"\n── {source} ──")
    url = "https://www.barleysknoxville.com/events"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        cards = page.query_selector_all(
            ".event, article, [class*='event'], [class*='show'], li.event"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.query_selector("h2,h3,h4,[class*=title]")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 4:
                    continue

                link_el = card.query_selector("a")
                link = link_el.get_attribute("href") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.barleysknoxville.com" + link

                date_el = card.query_selector("time,[class*=date]")
                date_str = date_el.inner_text().strip() if date_el else ""

                img_el = card.query_selector("img")
                img = img_el.get_attribute("src") or "" if img_el else ""

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue="Barley's Taproom & Pizzeria", location="200 E Jackson Ave, Knoxville, TN",
                    source=source, source_url=link, image=img,
                )
                evt["category"] = "Live Music"
                evt["neighborhood"] = "Old City"
                evt["ageRestrictions"] = "21+"
                evt["strollerFriendly"] = False
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Live Music"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 9. knoxbijou.org/events
def crawl_bijou(state):
    source = "knoxbijou.org"
    print(f"\n── {source} ──")
    base = "https://www.knoxbijou.org"
    url = f"{base}/events"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    soup = soup_get(url)
    if soup:
        cards = soup.select(
            "article, .event-card, .tribe-events-calendar-list__event, [class*='event'], .show"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.find(["h1", "h2", "h3", "h4"])
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 4:
                    continue

                link_el = card.find("a", href=True)
                link = link_el["href"] if link_el else ""
                if link and not link.startswith("http"):
                    link = base + link

                date_el = card.find(["time", "span", "div"], class_=re.compile(r"date|time", re.I))
                date_str = date_el.get_text(strip=True) if date_el else ""

                price_el = card.find(class_=re.compile(r"price|ticket|cost", re.I))
                price_text = price_el.get_text(strip=True) if price_el else ""

                img = first_img(card, base)

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue="Bijou Theatre", location="803 S Gay St, Knoxville, TN",
                    source=source, source_url=link, image=img, price_text=price_text,
                )
                evt["category"] = "Live Music"
                evt["neighborhood"] = "Downtown"
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Live Music"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    else:
        print(f"  failed to fetch {url}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 10. tennesseetheatre.com/events
def crawl_tennessee_theatre(state):
    source = "tennesseetheatre.com"
    print(f"\n── {source} ──")
    base = "https://www.tennesseetheatre.com"
    url = f"{base}/events"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    soup = soup_get(url)
    if soup:
        cards = soup.select(
            "article, .event-card, .eventlist-event, [class*='event'], .show-item"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.find(["h1", "h2", "h3", "h4"])
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 4:
                    continue

                link_el = card.find("a", href=True)
                link = link_el["href"] if link_el else ""
                if link and not link.startswith("http"):
                    link = base + link

                date_el = card.find(["time", "span", "div"], class_=re.compile(r"date|time|when", re.I))
                date_str = date_el.get_text(strip=True) if date_el else ""

                price_el = card.find(class_=re.compile(r"price|ticket|cost", re.I))
                price_text = price_el.get_text(strip=True) if price_el else ""

                img = first_img(card, base)

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue="Tennessee Theatre", location="604 S Gay St, Knoxville, TN",
                    source=source, source_url=link, image=img, price_text=price_text,
                )
                evt["neighborhood"] = "Downtown"
                if not img:
                    evt["image"] = CATEGORY_IMAGES.get(evt["category"], DEFAULT_IMAGE)
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    else:
        print(f"  failed to fetch {url}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 11. ijams.org/programs-events
def crawl_ijams(state):
    source = "ijams.org"
    print(f"\n── {source} ──")
    base = "https://www.ijams.org"
    url = f"{base}/programs-events/"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    soup = soup_get(url)
    if soup:
        cards = soup.select(
            "article, .tribe-events-calendar-list__event, .event-card, [class*='event'], .program-item"
        )
        print(f"  found {len(cards)} events")
        for card in cards[:25]:
            try:
                title_el = card.find(["h1", "h2", "h3", "h4"])
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 4:
                    continue

                link_el = card.find("a", href=True)
                link = link_el["href"] if link_el else ""
                if link and not link.startswith("http"):
                    link = base + link

                date_el = card.find(["time", "span", "div"], class_=re.compile(r"date|time|when|start", re.I))
                date_str = date_el.get_text(strip=True) if date_el else ""

                desc_el = card.find(["p", "div"], class_=re.compile(r"desc|excerpt|summary", re.I))
                desc = desc_el.get_text(strip=True) if desc_el else ""

                price_el = card.find(class_=re.compile(r"price|ticket|cost|fee", re.I))
                price_text = price_el.get_text(strip=True) if price_el else ""

                img = first_img(card, base)

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str, description=desc,
                    venue="Ijams Nature Centre", location="2915 Island Home Ave, Knoxville, TN",
                    source=source, source_url=link, image=img, price_text=price_text,
                )
                evt["category"] = derive_category(title, desc) or "Outdoor & Nature"
                evt["neighborhood"] = "South Knoxville"
                evt["indoorOutdoor"] = "Outdoor"
                evt["strollerFriendly"] = True
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Outdoor & Nature"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    else:
        print(f"  failed to fetch {url}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 12. oneknoxsc.com/schedule
def crawl_oneknox(page, state):
    source = "oneknoxsc.com"
    print(f"\n── {source} ──")
    url = "https://oneknoxsc.com/schedule/"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        cards = page.query_selector_all(
            ".tribe-events-calendar-list__event, .tribe-event, article, [class*='event'], [class*='game'], tr"
        )
        print(f"  found {len(cards)} items")
        for card in cards[:40]:
            try:
                title_el = card.query_selector("h2,h3,h4,[class*=title],[class*=name]")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 4:
                    continue

                link_el = card.query_selector("a")
                link = link_el.get_attribute("href") if link_el else url

                date_el = card.query_selector("time,[class*=date],[class*=start]")
                date_str = date_el.inner_text().strip() if date_el else ""

                time_el = card.query_selector("[class*=time],[class*=start-time]")
                time_str = time_el.inner_text().strip() if time_el else ""

                img_el = card.query_selector("img")
                img = img_el.get_attribute("src") or "" if img_el else ""

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    description=f"One Knox Soccer Club event. {title}.",
                    venue="One Knox SC", location="Knoxville, TN",
                    source=source, source_url=link or url, image=img, time_str=time_str,
                )
                evt["category"] = "Sports & Recreation"
                evt["indoorOutdoor"] = "Outdoor"
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Sports & Recreation"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 13. utsports.com — Football home games
def crawl_utsports_football(state):
    source = "utsports.com/football"
    print(f"\n── {source} ──")
    url = "https://utsports.com/sports/football/schedule"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    soup = soup_get(url)
    if soup:
        rows = soup.select(
            ".sidearm-schedule-game, tr.schedule__game, [class*='schedule-game'], li[class*='game']"
        )
        print(f"  found {len(rows)} games")
        for row in rows[:30]:
            try:
                text = row.get_text(" ", strip=True)
                # Home games: don't contain "at " before opponent or marked as neutral
                if re.search(r'\bat\s+[A-Z]', text):
                    continue  # away game

                title_el = row.find(class_=re.compile(r"opponent|school|team", re.I))
                if not title_el:
                    title_el = row.find(["h3", "h4", "span", "td"], string=re.compile(r"vs|home", re.I))
                opponent_text = title_el.get_text(strip=True) if title_el else ""
                title = f"Tennessee Volunteers Football vs {opponent_text}" if opponent_text else ""
                if not title or len(title) < 10:
                    continue

                date_el = row.find(["time", "span", "td"], class_=re.compile(r"date|when", re.I))
                date_str = date_el.get_text(strip=True) if date_el else ""

                time_el = row.find(["span", "td"], class_=re.compile(r"time|kickoff", re.I))
                time_str = time_el.get_text(strip=True) if time_el else "12:00 pm"

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    description=f"Tennessee Volunteers home football game at Neyland Stadium. {title}.",
                    venue="Neyland Stadium", location="1600 Stadium Dr, Knoxville, TN 37916",
                    source=source, source_url=url, price_text="$30-$150", time_str=time_str,
                )
                evt["category"] = "Sports & Recreation"
                evt["neighborhood"] = "Fort Sanders"
                evt["indoorOutdoor"] = "Outdoor"
                evt["image"] = CATEGORY_IMAGES["Sports & Recreation"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    row error: {e}")
    else:
        print(f"  failed to fetch {url}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 14. scruffycity.com/scruffy-city-hall — grabs show title + flyer image
def crawl_scruffycity(page, state):
    source = "scruffycity.com"
    print(f"\n── {source} ──")
    url = "https://scruffycity.com/scruffy-city-hall/"
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)
        cards = page.query_selector_all(
            ".event, article, [class*='show'], [class*='event'], [class*='concert'], .wp-block-group"
        )
        print(f"  found {len(cards)} items")
        for card in cards[:25]:
            try:
                title_el = card.query_selector("h2,h3,h4,[class*=title]")
                title = title_el.inner_text().strip() if title_el else ""
                if not title or len(title) < 4:
                    continue

                link_el = card.query_selector("a")
                link = link_el.get_attribute("href") if link_el else url

                date_el = card.query_selector("time,[class*=date]")
                date_str = date_el.inner_text().strip() if date_el else ""

                # Grab flyer image specifically
                img_el = card.query_selector("img")
                img = img_el.get_attribute("src") or img_el.get_attribute("data-src") or "" if img_el else ""
                if img and not img.startswith("http"):
                    img = urllib.parse.urljoin("https://scruffycity.com", img)

                price_el = card.query_selector("[class*=price],[class*=ticket],[class*=cost]")
                price_text = price_el.inner_text().strip() if price_el else ""

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue="Scruffy City Hall", location="32 Market Square, Knoxville, TN 37902",
                    source=source, source_url=link or url, image=img, price_text=price_text,
                )
                evt["category"] = "Live Music"
                evt["neighborhood"] = "Market Square"
                if not img:
                    evt["image"] = CATEGORY_IMAGES["Live Music"]
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    card error: {e}")
    except PWTimeout:
        print(f"  timeout: {url}")
    except Exception as e:
        print(f"  page error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# 15. ticketmaster.com — Thompson-Boling Arena
# Knoxville-area TN cities considered "local" — an event whose real venue city
# isn't in this set is rejected (prevents out-of-town events leaking in).
KNOXVILLE_AREA_CITIES = {
    "knoxville", "farragut", "alcoa", "maryville", "oak ridge", "powell",
    "kodak", "sevierville", "pigeon forge", "clinton", "lenoir city",
    "kingston", "halls", "hardin valley",
}


def crawl_ticketmaster_thompson_boling(state):
    source = "ticketmaster.com/knoxville"
    print(f"\n── {source} ──")
    # Ticketmaster Discovery API — query by city/state, NOT a hardcoded venueId.
    # A venueId can collide across markets; city+stateCode keeps results local.
    url = (
        "https://app.ticketmaster.com/discovery/v2/events.json"
        "?apikey=TICKETMASTER_API_KEY"
        "&city=Knoxville"
        "&stateCode=TN"
        "&countryCode=US"
        "&size=50"
        "&sort=date,asc"
    )
    saved = 0
    seen_hashes = state.get(source, {}).get("hashes", [])

    api_key = os.environ.get("TICKETMASTER_API_KEY", "")
    if not api_key:
        print("  TICKETMASTER_API_KEY not set — skipping")
        return 0

    actual_url = url.replace("TICKETMASTER_API_KEY", api_key)
    try:
        resp = requests.get(actual_url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("_embedded", {}).get("events", [])
        print(f"  found {len(events)} events")
        for ev in events:
            try:
                title = ev.get("name", "")
                if not title:
                    continue

                # Verify the event's REAL venue is in a Knoxville-area TN city
                # before trusting/saving it. Skip anything that isn't local.
                venues = ev.get("_embedded", {}).get("venues", [])
                if not venues:
                    print(f"    skip (no venue data): {title[:45]}")
                    continue
                v = venues[0]
                v_city = (v.get("city", {}) or {}).get("name", "").strip()
                v_state = (v.get("state", {}) or {}).get("stateCode", "").strip()
                if v_city.lower() not in KNOXVILLE_AREA_CITIES or v_state.upper() != "TN":
                    print(f"    skip (not Knoxville: {v_city}, {v_state}): {title[:45]}")
                    continue

                # Use the REAL venue name / address / coordinates from the API.
                venue_name = v.get("name", "") or "Knoxville Venue"
                addr = (v.get("address", {}) or {}).get("line1", "")
                postal = v.get("postalCode", "")
                location = ", ".join(p for p in [addr, f"{v_city}, {v_state} {postal}".strip()] if p)
                loc = v.get("location", {}) or {}
                lat = loc.get("latitude")
                lng = loc.get("longitude")

                dates = ev.get("dates", {}).get("start", {})
                date_str = dates.get("localDate", "")
                time_str = dates.get("localTime", "")

                # Price range
                price_ranges = ev.get("priceRanges", [])
                price_text = ""
                if price_ranges:
                    mn = price_ranges[0].get("min", "")
                    mx = price_ranges[0].get("max", "")
                    price_text = f"${mn}-${mx}" if mn and mx else f"${mn or mx}"

                # Image — prefer 16:9 ratio
                images = ev.get("images", [])
                img = ""
                for im in images:
                    if im.get("ratio") == "16_9" and im.get("width", 0) >= 640:
                        img = im.get("url", "")
                        break
                if not img and images:
                    img = images[0].get("url", "")

                url_detail = ev.get("url", "https://www.ticketmaster.com")

                chash = content_hash(title + date_str)
                if chash in seen_hashes:
                    continue
                seen_hashes.append(chash)

                evt = build_event(
                    title=title, date_str=date_str,
                    venue=venue_name, location=location or f"{v_city}, {v_state}",
                    source=source, source_url=url_detail, image=img,
                    price_text=price_text, time_str=time_str,
                )
                if lat and lng:
                    try:
                        evt["lat"] = float(lat)
                        evt["lng"] = float(lng)
                    except (TypeError, ValueError):
                        pass
                if not img:
                    evt["image"] = CATEGORY_IMAGES.get(evt["category"], DEFAULT_IMAGE)
                if save_event(evt):
                    saved += 1
            except Exception as e:
                print(f"    event error: {e}")
    except Exception as e:
        print(f"  API error: {e}")

    state[source] = {"hashes": seen_hashes[-500:], "lastRun": datetime.datetime.utcnow().isoformat()}
    print(f"  → {saved} new events saved")
    return saved


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    start_time = time.time()
    print(f"Knox Pulse Crawler — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Listings file: {LISTINGS_FILE}")

    load_listings()
    state = load_state()
    total_saved = 0

    # Playwright-based crawlers (JS-heavy sites)
    print("\n=== Starting Playwright crawlers ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        # Mask automation signals
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        total_saved += crawl_visitknoxville(page, state)
        total_saved += crawl_legacyparks(page, state)
        total_saved += crawl_smokies_baseball(page, state)
        total_saved += crawl_knoxville_coliseum(page, state)
        total_saved += crawl_mill_and_mine(page, state)
        total_saved += crawl_barleys(page, state)
        total_saved += crawl_oneknox(page, state)
        total_saved += crawl_scruffycity(page, state)

        browser.close()

    # requests + BeautifulSoup crawlers (simpler/static sites)
    print("\n=== Starting requests crawlers ===")
    total_saved += crawl_everythingknoxville(state)
    total_saved += crawl_865running(state)
    total_saved += crawl_bijou(state)
    total_saved += crawl_tennessee_theatre(state)
    total_saved += crawl_ijams(state)
    total_saved += crawl_utsports_football(state)
    total_saved += crawl_ticketmaster_thompson_boling(state)

    # Persist the merged listings + incremental state
    if total_saved:
        write_listings()
    else:
        print("No new events — data/listings.json unchanged.")
    save_state(state)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"Crawler complete — {total_saved} new events merged in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
