import os
import json
import re
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

# ─── Noise filter ─────────────────────────────────────────────────────────────────
_NOISE_EXACT: frozenset[str] = frozenset({
    # Spanish
    "recomendar", "comentar", "compartir", "enviar", "seguir",
    "ver más", "ver menos", "me gusta", "mostrar traducción",
    "ocultar traducción", "reaccionar", "reacciones", "comentarios",
    "visible para cualquier persona", "visible para todos",
    "publicaciones", "noticias", "inicio",
    # English
    "like", "comment", "share", "send", "follow", "unfollow",
    "see more", "see less", "show translation", "hide translation",
    "visible to anyone", "react", "reactions", "comments", "repost",
})

_NOISE_RE: list[re.Pattern] = [
    re.compile(r"^\d[\d\s.,]*\s*(reacciones?|reactions?|me gusta|likes?)\b", re.I),
    re.compile(r"^\d[\d\s.,]*\s*(comentarios?|comments?)\b", re.I),
    re.compile(r"^\d[\d\s.,]*\s*(veces\s+compartido|shares?|reposts?)\b", re.I),
    re.compile(r"^\d[\d\s.,]*\s*(seguidores?|followers?)\b", re.I),
    re.compile(r"^hace\s+\d+\s*(segundo|minuto|hora|día|semana|mes|año)s?\b", re.I),
    re.compile(r"^\d+\s*(s|m|h|d|w)\b", re.I),
    re.compile(r"^número de publicación en el feed\b", re.I),
    re.compile(r"^feed post number\b", re.I),
    re.compile(r"^visible\s+(para|to)\b", re.I),
    re.compile(r"^\d+\s*(impression|impresión)", re.I),
    re.compile(r"^\d+\s*(visualización|view)s?", re.I),
]
# ──────────────────────────────────────────────────────────────────────────────


def _clean_post_text(raw: str) -> str:
    """Strip LinkedIn UI chrome from raw innerText, returning only post content."""
    cleaned: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower() in _NOISE_EXACT:
            continue
        if any(p.match(s) for p in _NOISE_RE):
            continue
        cleaned.append(s)
    return "\n".join(cleaned).strip()


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

    WebDriverWait(driver, 10).until(lambda d: "linkedin.com" in d.current_url)

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


def _parse_pub_date(date_str: str) -> datetime:
    """Parse an ISO date string; return datetime.min on failure (sorts last)."""
    if not date_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def scrape_company_posts(driver: webdriver.Chrome, company_url: str) -> list[dict]:
    posts_url = company_url.rstrip("/") + "/posts/?feedView=all&sortBy=recency"
    driver.get(posts_url)
    time.sleep(5)
    scroll_to_load(driver)
    time.sleep(3)

    raw_posts: list[dict] = driver.execute_script(
        """
        const seen = new Set();
        const results = [];

        const containers = Array.from(
            document.querySelectorAll(
                'div.feed-shared-update-v2[data-urn*="activity"], div.feed-shared-update-v2'
            )
        );

        for (const el of containers) {
            if (results.length >= arguments[0]) break;

            const urn = el.getAttribute('data-urn') || '';
            const dedupeKey = urn || el.innerText.slice(0, 80);
            if (dedupeKey && seen.has(dedupeKey)) continue;
            if (dedupeKey) seen.add(dedupeKey);

            // ─ Text: target the post body to avoid actor/action-bar noise ─
            const bodyEl = el.querySelector(
                '.feed-shared-update-v2__description-wrapper, ' +
                '.update-components-text, ' +
                '.feed-shared-text'
            );
            const rawText = bodyEl ? bodyEl.innerText.trim() : el.innerText.trim();

            // ─ Link resolution (priority order) ─────────────────────────────────
            // 1. Build from data-urn — most reliable, always has the activity ID
            let href = '';
            if (urn && urn.startsWith('urn:li:activity:')) {
                href = 'https://www.linkedin.com/feed/update/' + urn;
            }
            // 2. <a> whose href contains "activity-" (slug-style post permalink)
            if (!href) {
                const a = el.querySelector('a[href*="activity-"]');
                if (a) href = a.href;
            }
            // 3. <a> whose href contains "/posts/"
            if (!href) {
                const a = el.querySelector('a[href*="/posts/"]');
                if (a) href = a.href;
            }
            // 4. Any other feed/update anchor as last resort
            if (!href) {
                const a = el.querySelector(
                    'a[href*="/feed/update/"], ' +
                    'a.update-components-mini-update-v2__link-to-details-page'
                );
                if (a) href = a.href;
            }
            // ────────────────────────────────────────────────────────────────

            const dateEl = el.querySelector(
                '.update-components-actor__sub-description span[aria-hidden="true"], ' +
                '.update-components-actor__sub-description, ' +
                'time'
            );

            results.push({
                text: rawText,
                href: href,
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
        raw_text = (item.get("text") or "").strip()
        text = _clean_post_text(raw_text)
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

    posts.reverse()
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

    sorted_posts = sorted(all_posts, key=lambda p: _parse_pub_date(p["date"]), reverse=True)

    for post in sorted_posts:
        fe = fg.add_entry()
        uid = hashlib.md5(post["text"].encode()).hexdigest()
        fe.id(post["link"] if post["link"] != post["company_url"] else f"{post['company_url']}#{uid}")
        title = post["text"][:120].replace("\n", " ")
        fe.title(title + ("..." if len(post["text"]) > 120 else ""))
        fe.content(post["text"], type="text")
        fe.link(href=post["link"])

        pub_date = _parse_pub_date(post["date"])
        if pub_date == datetime.min.replace(tzinfo=timezone.utc):
            pub_date = datetime.now(timezone.utc)

        fe.published(pub_date)
        fe.updated(pub_date)

    fg.rss_file(FEED_FILE, pretty=True)
    print(f"RSS feed written to {FEED_FILE} ({len(sorted_posts)} entries)")


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
