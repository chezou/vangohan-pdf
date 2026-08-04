#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx2",
#   "pillow",
#   "selenium",
#   "click",
#   "markdown",
# ]
# ///


import base64
import datetime
import logging
import os
import re
import shutil
import pathlib
import time
from io import BytesIO
from typing import List, Optional

import click
import httpx2
import markdown
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException

# This template is based on https://gist.github.com/Fedik/674f4148439698a6681032b3bec370b3
TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
    <meta name="referrer" content="no-referrer" />
    <meta name="referrer" content="unsafe-url" />
    <meta name="referrer" content="origin" />
    <meta name="referrer" content="no-referrer-when-downgrade" />
    <meta name="referrer" content="origin-when-cross-origin" />
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Vangohan Recipe</title>
    <link href="bootstrap.min.css" rel="stylesheet">
    <style>
        body {
            font-family: Helvetica,Arial,sans-serif;
        }
        code, pre {
            font-family: monospace;
        }
    </style>
</head>
<body>
<div class="container my-5">
<div class="col-lg-8 px-0 mx-auto">
{{content}}
</div>
</div>
</body>
</html>
"""

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("WDM").setLevel(logging.WARNING)


class VangohanScraper:
    VANGOHAN_URL = "https://light-nyala-71c.notion.site/VanGohan-Instructions-0290b31c1baf4eeab79613508adeba38"
    ARTICLE_XPATH = '//div[contains(@class, "notion-collection-item")]/a'
    # Notion page URLs always end with a 32 hex digit page id
    NOTION_PAGE_RE = re.compile(r"^https://[^/]+\.notion\.site/[^/?#]*[0-9a-f]{32}")
    # Titles/body text Notion or Cloudflare shows instead of the page itself
    BLOCK_MARKERS = (
        "attention required",
        "access denied",
        "too many requests",
        "error 429",
        "rate limit",
        "you have been blocked",
    )

    def __init__(self):
        self._chrome_options = self._build_chrome_options()
        self.driver = webdriver.Chrome(options=self._chrome_options)
        self._article_urls: List[str] = []

    @staticmethod
    def _build_chrome_options() -> webdriver.ChromeOptions:
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        )
        return chrome_options

    def __del__(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    def _wait_for_cloudflare(self, timeout: int = 120):
        time.sleep(2)
        if self.driver.title == "Just a moment...":
            logger.info("Cloudflare challenge detected, waiting for Turnstile to solve...")
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.title != "Just a moment..."
            )
            logger.info(f"Cloudflare challenge passed, page title: {self.driver.title}")
            time.sleep(2)

    def _blocked_reason(self) -> Optional[str]:
        """Return a short reason if Notion/Cloudflare served an interstitial instead of the page."""
        try:
            title = (self.driver.title or "").lower()
            body = self.driver.find_element(By.TAG_NAME, "body").text[:1000].lower()
        except WebDriverException:
            return None

        for marker in self.BLOCK_MARKERS:
            if marker in title or marker in body:
                return marker
        return None

    def _log_page_diagnostics(self, context: str):
        """Dump enough page state to tell a blocked page apart from a changed DOM."""
        try:
            logger.warning(f"Page diagnostics while {context}:")
            logger.warning(f"  title={self.driver.title!r} url={self.driver.current_url!r}")
            counts = self.driver.execute_script(
                "return {anchors: document.querySelectorAll('a').length,"
                " collection: document.querySelectorAll('[class*=\"notion-collection-item\"]').length,"
                " notion: document.querySelectorAll('[class^=\"notion-\"]').length}"
            )
            logger.warning(f"  element counts={counts}")
            body = self.driver.find_element(By.TAG_NAME, "body").text
            logger.warning(f"  body[:500]={body[:500]!r}")
        except WebDriverException as e:
            logger.warning(f"  could not collect diagnostics: {e}")

    def _load_top_page(self):
        self.driver.get(self.VANGOHAN_URL)
        self._wait_for_cloudflare()
        reason = self._blocked_reason()
        if reason:
            logger.warning(f"Top page looks blocked/throttled: {reason}")
        return reason is None

    def _collect_article_urls(self, timeout: int = 60) -> List[str]:
        """Collect the collection item links of the currently loaded top page.

        Uses presence (not visibility): Notion renders collection items lazily, so
        requiring *every* matched element to be visible can never settle, and we only
        need the href anyway.
        """
        urls: List[str] = []
        try:
            articles = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_all_elements_located((By.XPATH, self.ARTICLE_XPATH))
            )
            urls = [article.get_attribute("href") for article in articles]
        except TimeoutException:
            logger.warning("No notion-collection-item links found, falling back to href pattern")

        if not urls:
            urls = self._collect_article_urls_by_href()

        return [url for url in urls if url]

    def _collect_article_urls_by_href(self) -> List[str]:
        """Fallback for when Notion renames its collection CSS classes."""
        hrefs = self.driver.execute_script(
            "return Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
        )
        return [
            href
            for href in dict.fromkeys(hrefs or [])
            if self.NOTION_PAGE_RE.match(href)
        ]

    def _reinitialize_driver(self):
        logger.info("Reinitializing Chrome driver")
        try:
            self.driver.quit()
        except Exception:
            pass
        time.sleep(3)
        self.driver = webdriver.Chrome(options=self._chrome_options)
        self.driver.get("about:blank")
        time.sleep(1)

    @classmethod
    def tuesday_string(cls, hyphenated: bool = False, abbr: bool = False) -> str:
        today = datetime.date.today()
        day_of_week = today.weekday()
        tuesday = (
            today - datetime.timedelta(days=day_of_week) + datetime.timedelta(days=1)
        )
        return tuesday.strftime(
            f"{'%b' if abbr else '%B'}{'-' if hyphenated else ' '}%-d"
        )

    def save_menu_image(self, output_dir: str, max_retries: int = 3) -> bool:
        logger.info("Deleting an existing menu image")
        menu_img = pathlib.Path(output_dir, "menu.png")
        menu_img.unlink(missing_ok=True)
        for attempt in range(max_retries):
            try:
                logger.info(f"fetching menu image (attempt {attempt + 1}/{max_retries})")
                self._load_top_page()
                # Harvest the recipe links while the top page is loaded, so fetch_recipes()
                # does not have to reload it right after we navigated into the menu page.
                if not self._article_urls:
                    self._article_urls = self._collect_article_urls(timeout=30)
                    logger.info(f"collected {len(self._article_urls)} recipe links")
                if self._fetch_menu_image(" Menu", menu_img):
                    return True
                elif self._fetch_menu_image(
                    VangohanScraper.tuesday_string(abbr=False), menu_img
                ):
                    return True
                elif self._fetch_menu_image(
                    VangohanScraper.tuesday_string(abbr=True), menu_img
                ):
                    return True
                else:
                    return False
            except WebDriverException as e:
                logger.warning(f"WebDriverException on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    self._reinitialize_driver()
                else:
                    logger.error(f"Failed to fetch menu image after {max_retries} attempts")
                    return False
        return False

    def _fetch_menu_image(self, target_str: str, menu_img: pathlib.Path) -> bool:
        logger.info(f"{target_str=}")
        try:
            menu = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located(
                    (
                        By.XPATH,
                        f'//div[contains(text(), "{target_str}")]/ancestor::a',
                    )
                )
            )

            menu.click()  # open menu page
            logger.debug("clicked")

            img = WebDriverWait(self.driver, 40).until(
                EC.visibility_of_element_located(
                    (By.XPATH, '//div[@class="notion-cursor-default"]//img')
                )
            )
            src = img.get_attribute("src")
            r = httpx2.get(src, follow_redirects=True, timeout=30)
            r.raise_for_status()
            i = Image.open(BytesIO(r.content))
            i.save(menu_img)

            return True
        except TimeoutException:
            logger.error(f"TimeoutException to fetch menu image for {target_str}")
            return False
        except httpx2.HTTPStatusError as e:
            logger.error(f"HTTP error fetching menu image: {e.response.status_code}")
            return False

    def collect_recipe_urls(self, max_retries: int = 3) -> List[str]:
        """Load the top page and return its collection links, retrying with backoff.

        Notion regularly serves a partially rendered or throttled top page; a single
        attempt at this is the flakiest step of the whole scrape.
        """
        for attempt in range(max_retries):
            try:
                self._load_top_page()
                urls = self._collect_article_urls()
                if urls:
                    return urls
                self._log_page_diagnostics("collecting recipe links")
            except WebDriverException as e:
                logger.warning(
                    f"WebDriverException collecting recipe links on attempt {attempt + 1}: {e}"
                )
                self._reinitialize_driver()

            if attempt < max_retries - 1:
                wait = 10 * 2**attempt
                logger.warning(f"No recipe links yet, retrying in {wait}s")
                time.sleep(wait)

        raise RuntimeError("Could not find any recipe links on the top page")

    def fetch_recipes(self) -> List[str]:
        try:
            logger.info("fetching recipes")

            urls = self._article_urls or self.collect_recipe_urls()
            logger.info(urls)

            recipes = []
            IGNORE_URL_PATTERNS = [
                "VanGohan-Instructions",
                "Welcome-to-VanGohan",
                "Printable-instructions-",
                VangohanScraper.tuesday_string(hyphenated=True),
                VangohanScraper.tuesday_string(hyphenated=True, abbr=True),
                "-Menu-",
            ]

            for url in urls:
                if any(pat in url for pat in IGNORE_URL_PATTERNS):
                    continue

                recipe_content = self._fetch_single_recipe(url, max_retries=3)
                if recipe_content:
                    recipes.append(recipe_content)

            return recipes

        except Exception as e:
            logger.error(f"Error while fetching recipes: {e}")
            raise

    def _fetch_single_recipe(self, url: str, max_retries: int = 2) -> str:
        for attempt in range(max_retries):
            try:
                logger.info(f"Fetching {url} (attempt {attempt + 1}/{max_retries})")
                self.driver.get(url)
                self._wait_for_cloudflare()

                content_path = '//div[contains(@class, "notion-page-content")]'
                content = WebDriverWait(self.driver, 60).until(
                    EC.presence_of_element_located((By.XPATH, content_path))
                )
                return self.driver.execute_script(
                    "return arguments[0].innerText", content
                )

            except TimeoutException as e:
                logger.warning(f"Timeout fetching {url} on attempt {attempt + 1}: {e}")
                self._log_page_diagnostics(f"fetching {url}")
                if attempt >= max_retries - 1:
                    raise
                time.sleep(5 * 2**attempt)

            except WebDriverException as e:
                logger.warning(f"WebDriverException fetching {url} on attempt {attempt + 1}: {e}")
                if attempt >= max_retries - 1:
                    raise
                # The session is unusable after e.g. "tab crashed"; retrying on the same
                # driver just replays the same error immediately.
                self._reinitialize_driver()

        raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts")

    def save_recipes(
        self, recipes: List[str], fname: str, image_exist: bool = True, lang: str = "ja"
    ):
        logger.info("parsing html")

        en_title1 = "Things you need to prepare"
        en_title2 = "Instructions"
        ja_title1 = "ご自宅でご用意いただくもの"
        ja_title2 = "インストラクション"
        tips = "Tips"

        en_flag = False

        with open(fname, "w") as f:
            today = datetime.date.today()
            day_of_week = today.weekday()
            f.write(
                f"## VanGohan Recipe: Week of {today - datetime.timedelta(days=day_of_week)}\n\n"
            )
            for recipe in recipes:
                rows = recipe.split("\n")
                logger.debug(rows)
                if not rows or len(rows) < 2:
                    logger.warning("Empty recipe")
                    continue

                title_row = 1 if lang == "ja" else 0
                f.write(f"## {rows[title_row]}\n")  # title
                instruction_flag = False
                for row in rows[2:]:
                    if not row:
                        continue
                    elif row == ja_title1 or (is_title2 := row.startswith(ja_title2)):
                        en_flag = False
                        if lang == "ja":
                            if is_title2:
                                instruction_flag = True

                            f.write("\n#### ")
                    elif row == en_title1 or (is_title2 := row.startswith(en_title2)):
                        en_flag = True
                        if lang == "en":
                            if is_title2:
                                instruction_flag = True

                            f.write("\n#### ")
                    elif row == tips:
                        instruction_flag = False
                        f.write("\n#### ")
                    else:
                        if not en_flag and lang == "ja":
                            prefix = "1. " if instruction_flag else "- "
                            f.write(prefix)
                        elif en_flag and lang == "en":
                            prefix = "1. " if instruction_flag else "- "
                            f.write(prefix)

                    if lang == "ja" and en_flag:
                        continue
                    elif lang == "en" and not en_flag:
                        continue

                    f.write(f"{row}\n")

                f.write("\n\n")

            if image_exist:
                f.write("<img class='img-fluid' src='./menu.png'>\n")

    def html2pdf2(self, input_fname: str, output_fname: str):
        logger.info("Saving PDF")

        path = os.path.abspath(input_fname)
        url = pathlib.Path(path).as_uri()

        self.driver.get(url)

        WebDriverWait(self.driver, 10).until(
            lambda driver: driver.execute_script("return document.readyState")
            == "complete"
        )

        print_options = {
            "landscape": False,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": True,
            "pageSize": "Letter",
            "scale": 0.9,
        }
        result = self._send_devtools("Page.printToPDF", print_options)

        with open(output_fname, "wb") as f:
            f.write(base64.b64decode(result["data"]))

        logger.info(f"PDF saved successfully: {output_fname}")

    # From https://gist.github.com/bloodwithmilk25/3e05719829ae875319485bc52fcd294e#file-pdf_generator_simple_version-py
    def _send_devtools(self, cmd, params={}):
        """
        Works only with chromedriver.
        Method uses selenium's execute_cdp_cmd to send Chrome DevTools commands.
        """
        try:
            # Use Selenium 4's built-in CDP command method
            return self.driver.execute_cdp_cmd(cmd, params)
        except AttributeError:
            # Fallback for older selenium versions
            return self.driver.execute("send_command", {"cmd": cmd, "params": params})


def md2html(input_fname: str, output_fname: str):
    with open(input_fname, "r") as f:
        md = f.read()
        extensions = ["extra", "smarty"]
        html = markdown.markdown(md, extensions=extensions, output_format="html5")
        doc = TEMPLATE.replace("{{content}}", html)

        with open(output_fname, "w") as fw:
            fw.write(doc)


@click.command()
@click.option(
    "-l", "--lang", multiple=True, default=("ja",), help="language (ja or en, repeatable)"
)
@click.option("-o", "--output", default="results", help="output folder name")
def cli(lang, output):
    vs = VangohanScraper()
    pathlib.Path(output).mkdir(parents=True, exist_ok=True)
    image_exist = vs.save_menu_image(output)
    recipes = vs.fetch_recipes()

    shutil.copy("bootstrap.min.css", output)

    for l in lang:
        base_name = "vangohan" + ("_en" if l == "en" else "")
        logger.info(f"Generating output for language: {l}")

        vs.save_recipes(recipes, f"{base_name}.md", image_exist=image_exist, lang=l)
        md2html(f"{base_name}.md", pathlib.Path(output, f"{base_name}.html"))
        vs.html2pdf2(
            pathlib.Path(output, f"{base_name}.html"),
            pathlib.Path(output, f"{base_name}.pdf"),
        )


if __name__ == "__main__":
    cli()
