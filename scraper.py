import os
import json
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from feedgen.feed import FeedGenerator

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
# Add more LinkedIn company page URLs here to expand the feed
COMPANY_URLS = [
    "https://www.linkedin.com/company/eoa-enhancing-opportunities-for-all/",
]

SESSION_FILE = "session.json"
FEED_FILE = "feed.xml"
MAX_POSTS_PER_COMPANY = 15
SCROLL_PASSES = 3
# ──────────────────────────────────────────────────────────────────────────────

LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD")


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=options)


def save_session(driver: webdriver.Chrome) -> None:
    cookies = driver.get_cookies()
    with open(SESSION_FILE, "w") as fh:
        json.dump(cookies, fh)
    print(f"Session saved to {SESSION_FILE}")


def load_session(driver: webdriver.Chrome) -> None:
    """Inject cookies from session.json into the browser."""
    driver.get("https://www.linkedin.com")
    with open(SESSION_FILE) as fh:
        cookies = json.load(fh)
    for cookie in cookies:
        cookie.pop("sameSite", None)
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass
    driver.refresh()


def login(driver: webdriver.Chrome) -> None:
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        raise EnvironmentError(
            "No session.json found and no credentials available. "
            "Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD as GitHub Actions secrets, "
            "or run the script locally first to generate session.json."
        )
    print("No session found — logging in with credentials...")
    driver.get("https://www.linkedin.com/login")
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "username")))
    driver.find_element(By.ID, "username").send_keys(LINKEDIN_EMAIL)
    driver.find_element(By.ID, "password").send_keys(LINKEDIN_PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "[type=submit]").click()
    try:
        WebDriverWait(driver, 20).until(
            EC.any_of(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".feed-identity-module")),
                EC.presence_of_element_located((By.CSS_SELECTOR, ".authentication-outlet")),
            )
        )
    except TimeoutException:
        raise RuntimeError(
            "Login timed out. LinkedIn may have shown a CAPTCHA or 2FA prompt. "
            "Run the script locally without --headless to complete the verification, "
            "then copy the generated session.json."
        )
    save_session(driver)
    print("Login successful.")


def scroll_to_load(driver: webdriver.Chrome, passes: int = SCROLL_PASSES) -> None:
    for _ in range(passes):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2.5)


def _text_from(element, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            text = element.find_element(By.CSS_SELECTOR, sel).text.strip()
            if text:
                return text
        except NoSuchElementException:
            continue
    return ""


def _attr_from(element, selectors: list[str], attr: str) -> str:
    for sel in selectors:
        try:
            return element.find_element(By.CSS_SELECTOR, sel).get_attribute(attr) or ""
        except NoSuchElementException:
            continue
    return ""


def scrape_company_posts(driver: webdriver.Chrome, company_url: str) -> list[dict]:
    posts_url = company_url.rstrip("/") + "/posts/"
    driver.get(posts_url)
    time.sleep(3)
    scroll_to_load(driver)

    # ---- Selectors verified against LinkedIn's live DOM (2026) ----
    #
    # POST_CONTAINERS: data-urn*='activity' anchors to LinkedIn's internal
    # activity URN scheme, which is far more stable than CSS class names.
    # Fallback drops the attribute filter for broader matching.
    #
    # TEXT_SELECTORS: ordered from most specific to most generic.
    # If all CSS selectors fail, the loop falls back to elem.text (the full
    # visible text of the container) and then to a URL-based placeholder,
    # so the feed is never left empty when containers are found.
    #
    # LINK_SELECTORS: update-components-mini-update-v2__link-to-details-page
    # is the dedicated permalink anchor LinkedIn injects on every post card.
    #
    # DATE_SELECTORS: sub-description holds a human-readable age string
    # (e.g. "2d", "1w"); aria-hidden='true' targets the visible copy when
    # LinkedIn renders both visible and screen-reader variants.
    # ---------------------------------------------------------------
    PRIMARY_CONTAINER_SEL = "div.feed-shared-update-v2[data-urn*='activity']"
    FALLBACK_CONTAINER_SEL = "div.feed-shared-update-v2"

    TEXT_SELECTORS = [
        ".feed-shared-update-v2__description-wrapper",
        ".update-components-text",
        ".feed-shared-text",
        ".feed-shared-text__text-view",
        ".attributed-text-segment-list__content",
        "span[dir='ltr']",
        ".break-words span",
        ".update-components-text relative-time",
    ]
    LINK_SELECTORS = [
        "a.update-components-mini-update-v2__link-to-details-page",
        "a[href*='/feed/update/urn']",
        "a[href*='/posts/']",
    ]
    DATE_SELECTORS = [
        ".update-components-actor__sub-description span[aria-hidden='true']",
        ".update-components-actor__sub-description",
        "time",
    ]

    containers = driver.find_elements(By.CSS_SELECTOR, PRIMARY_CONTAINER_SEL)
    if not containers:
        print("  Primary container selector returned 0 results, trying fallback...")
        containers = driver.find_elements(By.CSS_SELECTOR, FALLBACK_CONTAINER_SEL)
    print(f"  Found {len(containers)} post containers")

    posts: list[dict] = []
    for elem in containers[:MAX_POSTS_PER_COMPANY]:
        try:
            text = _text_from(elem, TEXT_SELECTORS)

            # Fallback 1: use the container's full visible text (may include
            # actor name and metadata, but is better than an empty feed).
            if not text:
                text = elem.text.strip()

            # Fallback 2: generic placeholder so the entry is never titleless.
            if not text:
                text = f"Post from {company_url}"

            link = _attr_from(elem, LINK_SELECTORS, "href") or company_url
            date_dt = _attr_from(elem, DATE_SELECTORS, "datetime")
            date_txt = date_dt or _text_from(elem, DATE_SELECTORS)

            posts.append(
                {
                    "text": text,
                    "link": link,
                    "date": date_txt,
                    "company_url": company_url,
                }
            )
        except Exception as exc:
            print(f"  Skipping post element: {exc}")
            continue

    return posts


def generate_rss(all_posts: list[dict]) -> None:
    fg = FeedGenerator()
    fg.id("https://github.com/pablopernia11-bit/linkedin-rss-scraper/feed.xml")
    fg.title("LinkedIn Company Posts")
    fg.description("Aggregated RSS feed of LinkedIn company posts")
    fg.link(href="https://www.linkedin.com", rel="alternate")
    fg.link(
        href="https://raw.githubusercontent.com/pablopernia11-bit/linkedin-rss-scraper/main/feed.xml",
        rel="self",
    )
    fg.language("en")
    fg.updated(datetime.now(timezone.utc))

    for post in all_posts:
        fe = fg.add_entry()
        uid = hashlib.md5(post["text"].encode()).hexdigest()
        fe.id(post["link"] if post["link"] != post["company_url"] else f"{post['company_url']}#{uid}")
        title = post["text"][:120].replace("\n", " ")
        fe.title(title + ("..." if len(post["text"]) > 120 else ""))
        fe.content(post["text"], type="text")
        fe.link(href=post["link"])

        pub_date: datetime
        if post["date"]:
            try:
                pub_date = datetime.fromisoformat(post["date"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pub_date = datetime.now(timezone.utc)
        else:
            pub_date = datetime.now(timezone.utc)

        fe.published(pub_date)
        fe.updated(pub_date)

    fg.rss_file(FEED_FILE, pretty=True)
    print(f"RSS feed written to {FEED_FILE} ({len(all_posts)} entries)")


def main() -> None:
    driver = build_driver()
    try:
        if Path(SESSION_FILE).exists():
            print(f"Found {SESSION_FILE} — loading cookies directly (skipping credential login)...")
            load_session(driver)
            print("Session restored.")
        else:
            # No cached session: fall back to username/password login.
            # login() raises EnvironmentError immediately if credentials are missing.
            login(driver)

        all_posts: list[dict] = []
        for url in COMPANY_URLS:
            print(f"Scraping: {url}")
            posts = scrape_company_posts(driver, url)
            print(f"  → {len(posts)} posts found")
            all_posts.extend(posts)

        if not all_posts:
            print("WARNING: No posts were scraped. LinkedIn selectors may have changed.")

        generate_rss(all_posts)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
