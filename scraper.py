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
from selenium.common.exceptions import TimeoutException
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
SCROLL_PASSES = 5
# ──────────────────────────────────────────────────────────────────────────────

LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD")

# URL path fragments that indicate LinkedIn already has an active session.
_LOGGED_IN_PATHS = ("/feed", "/mynetwork", "/jobs", "/messaging", "/notifications")


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
    print("No session found — navigating to LinkedIn login...")
    driver.get("https://www.linkedin.com/login")

    # Wait until the page has settled on a LinkedIn URL.
    WebDriverWait(driver, 10).until(lambda d: "linkedin.com" in d.current_url)

    # LinkedIn redirects away from /login when the browser already has a valid
    # session (e.g. cookies set by a previous run). Skip the form in that case.
    if any(driver.current_url.startswith(f"https://www.linkedin.com{p}") for p in _LOGGED_IN_PATHS):
        print(f"Already logged in (redirected to {driver.current_url}). Saving session.")
        save_session(driver)
        return

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


def scrape_company_posts(driver: webdriver.Chrome, company_url: str) -> list[dict]:
    # Request posts sorted by recency so the RSS feed always shows the latest content.
    posts_url = company_url.rstrip("/") + "/posts/?feedView=all&sortBy=recency"
    driver.get(posts_url)
    time.sleep(5)
    scroll_to_load(driver)
    time.sleep(3)

    # Extract all post data in a single JavaScript call.
    # Using innerText (not textContent) forces the browser's rendering engine
    # to compute the visible text, which works correctly in headless mode and
    # bypasses lazy-loading issues that affect Selenium's element.text.
    raw_posts: list[dict] = driver.execute_script(
        """
        const seen = new Set();
        const results = [];

        // Try the most specific selector first (activity URN), then broader.
        const containers = Array.from(
            document.querySelectorAll(
                'div.feed-shared-update-v2[data-urn*="activity"], div.feed-shared-update-v2'
            )
        );

        for (const el of containers) {
            if (results.length >= arguments[0]) break;

            // Deduplicate by URN when available, otherwise by text hash.
            const urn = el.getAttribute('data-urn') || '';
            const dedupeKey = urn || el.innerText.slice(0, 80);
            if (dedupeKey && seen.has(dedupeKey)) continue;
            if (dedupeKey) seen.add(dedupeKey);

            const linkEl = el.querySelector(
                'a.update-components-mini-update-v2__link-to-details-page, ' +
                'a[href*="/feed/update/urn"], ' +
                'a[href*="/posts/"]'
            );
            const dateEl = el.querySelector(
                '.update-components-actor__sub-description span[aria-hidden="true"], ' +
                '.update-components-actor__sub-description, ' +
                'time'
            );

            results.push({
                text: el.innerText.trim(),
                href: linkEl ? linkEl.href : '',
                date: dateEl
                    ? (dateEl.getAttribute('datetime') || dateEl.innerText || '').trim()
                    : '',
                urn: urn
            });
        }
        return results;
        """,
        MAX_POSTS_PER_COMPANY,
    )

    print(f"  Extracted {len(raw_posts)} posts via JavaScript")

    posts: list[dict] = []
    for item in raw_posts:
        text = (item.get("text") or "").strip()
        if not text:
            text = f"Post from {company_url}"

        link = item.get("href") or company_url
        date_txt = (item.get("date") or "").strip()

        posts.append(
            {
                "text": text,
                "link": link,
                "date": date_txt,
                "company_url": company_url,
            }
        )

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
