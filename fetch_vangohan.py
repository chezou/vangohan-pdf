import base64
import datetime
import logging
import os
import shutil
import pathlib
from io import BytesIO
from typing import List

import chromedriver_autoinstaller
import click
import httpx
import markdown
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

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

    def __init__(self):
        chromedriver_autoinstaller.install()

        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")

        self.driver = webdriver.Chrome(
            options=chrome_options,
        )

    def __del__(self):
        self.driver.quit()

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

    def save_menu_image(self, output_dir: str) -> bool:
        logger.info("Deleting an existing menu image")
        menu_img = pathlib.Path(output_dir, "menu.png")
        menu_img.unlink(missing_ok=True)
        logger.info("fetching menu image")
        self.driver.get(self.VANGOHAN_URL)
        if self._fetch_menu_image(" Menu", menu_img):
            return True
        elif self._fetch_menu_image(VangohanScraper.tuesday_string(abbr=False), menu_img):
            return True
        elif self._fetch_menu_image(VangohanScraper.tuesday_string(abbr=True), menu_img):
            return True
        else:
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

            img = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located(
                    (By.XPATH, '//div[@class="notion-cursor-default"]//img')
                )
            )
            src = img.get_attribute("src")
            r = httpx.get(src, follow_redirects=True)
            i = Image.open(BytesIO(r.content))
            i.save(menu_img)

            return True
        except TimeoutException:
            logger.error(f"TimeoutException to fetch menu image for {target_str}")
            return False

    def fetch_recipes(self) -> List[str]:
        logger.info("fetching recipes")
        self.driver.get(self.VANGOHAN_URL)
        articles = WebDriverWait(self.driver, 20).until(
            EC.visibility_of_all_elements_located(
                (
                    By.XPATH,
                    '//div[contains(@class, "notion-collection-item")]/a',
                )
            )
        )

        urls = [article.get_attribute("href") for article in articles]
        logger.info(urls)

        recipes = []
        IGNORE_URL_PATTERNS = [
            "Welcome-to-VanGohan",
            "Printable-instructions-",
            VangohanScraper.tuesday_string(hyphenated=True),
            VangohanScraper.tuesday_string(hyphenated=True, abbr=True),
            "-Menu-",
        ]

        for url in urls:
            if any(pat in url for pat in IGNORE_URL_PATTERNS):
                continue
            logger.info(url)
            self.driver.get(url)
            content_path = '//div[@class="notion-page-content"]'

            content = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located((By.XPATH, content_path))
            )
            recipes.append(content.get_attribute("innerText"))

        return recipes

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
        print_options = {
            "landscape": False,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": True,
            "pageSize": "Letter",
            "scale": 0.9,
        }
        result = self._send_devtools(self.driver, "Page.printToPDF", print_options)

        with open(output_fname, "wb") as f:
            f.write(base64.b64decode(result["data"]))

    # From https://gist.github.com/bloodwithmilk25/3e05719829ae875319485bc52fcd294e#file-pdf_generator_simple_version-py
    @staticmethod
    def _send_devtools(driver, cmd, params):
        """
        Works only with chromedriver.
        Method uses cromedriver's api to pass various commands to it.
        """
        import json

        resource = f"/session/{driver.session_id}/chromium/send_command_and_get_result"
        url = driver.command_executor._url + resource
        body = json.dumps({"cmd": cmd, "params": params})
        response = driver.command_executor._request("POST", url, body)
        return response.get("value")


def md2html(input_fname: str, output_fname: str):
    with open(input_fname, "r") as f:
        md = f.read()
        extensions = ["extra", "smarty"]
        html = markdown.markdown(md, extensions=extensions, output_format="html5")
        doc = TEMPLATE.replace("{{content}}", html)

        with open(output_fname, "w") as fw:
            fw.write(doc)


@click.command()
@click.option("-l", "--lang", default="ja", help="language (ja or en)")
@click.option("-o", "--output", default="results", help="output folder name")
def cli(lang, output):
    vs = VangohanScraper()
    pathlib.Path(output).mkdir(parents=True, exist_ok=True)
    image_exist = vs.save_menu_image(output)
    recipes = vs.fetch_recipes()

    base_name = "vangohan" + ("_en" if lang == "en" else "")

    vs.save_recipes(recipes, f"{base_name}.md", image_exist=image_exist, lang=lang)
    shutil.copy("bootstrap.min.css", output)

    md2html(f"{base_name}.md", pathlib.Path(output, f"{base_name}.html"))

    vs.html2pdf2(
        pathlib.Path(output, f"{base_name}.html"),
        pathlib.Path(output, f"{base_name}.pdf"),
    )


if __name__ == "__main__":
    cli()
