import base64
import os
import pathlib
from io import BytesIO
from typing import List

import httpx
import markdown
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

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
    <title>Page Title</title>
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
<div class="container mt-3">
{{content}}
</div>
</body>
</html>
"""


class VangohanScraper:
    VANGOHAN_URL = "https://light-nyala-71c.notion.site/VanGohan-Instructions-0290b31c1baf4eeab79613508adeba38"

    def __init__(self):
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")

        self.driver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=chrome_options,
        )

    def __del__(self):
        self.driver.quit()

    def save_menu_image(self):
        self.driver.get(self.VANGOHAN_URL)
        menu = WebDriverWait(self.driver, 20).until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    '//div[contains(text(), " Menu")]/ancestor::a',
                )
            )
        )

        menu.click()  # open menu page

        img = WebDriverWait(self.driver, 20).until(
            EC.visibility_of_element_located(
                (By.XPATH, '//div[@class="notion-cursor-default"]//img')
            )
        )
        src = img.get_attribute("src")
        r = httpx.get(src)
        i = Image.open(BytesIO(r.content))
        i.save("menu.png")

    def fetch_recipes(self) -> List[str]:
        self.driver.get(self.VANGOHAN_URL)
        articles = WebDriverWait(self.driver, 20).until(
            EC.visibility_of_all_elements_located(
                (
                    By.XPATH,
                    '//div[@class="notion-selectable notion-page-block notion-collection-item"]/a',
                )
            )
        )

        urls = [article.get_attribute("href") for article in articles]

        recipes = []
        IGNORE_URL_PATTERNS = ["-Menu-", "Welcome-to-VanGohan"]

        for url in urls:
            if any(pat in url for pat in IGNORE_URL_PATTERNS):
                continue
            self.driver.get(url)
            content_path = '//div[@class="notion-page-content"]'

            content = WebDriverWait(self.driver, 20).until(
                EC.visibility_of_element_located((By.XPATH, content_path))
            )
            recipes.append(content.get_attribute("innerText"))

        return recipes

    def save_recipes(self, recipes: List[str], fname: str, lang: str = "ja"):
        en_title1 = "Things you need to prepare"
        en_title2 = "Instructions"
        ja_title1 = "ご自宅でご用意いただくもの"
        ja_title2 = "インストラクション"

        en_flag = False

        with open(fname, "w") as f:
            for recipe in recipes:
                rows = recipe.split("\n")
                title_row = 1 if lang == "ja" else 0
                f.write(f"## {rows[title_row]}\n")  # title
                instruction_flag = False
                for row in rows[2:]:
                    if not row:
                        continue
                    elif row == ja_title1 or row.startswith(ja_title2):
                        en_flag = False
                        if lang == "ja":
                            if row == ja_title2:
                                instruction_flag = True

                            f.write("\n#### ")
                    elif row == en_title1 or row.startswith(en_title2):
                        en_flag = True
                        if lang == "en":
                            if row == en_title2:
                                instruction_flag = True

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

            f.write("<img src='./menu.png' height='600'>\n")

    def html2pdf2(self, input_fname: str, output_fname: str):
        path = os.path.abspath(input_fname)
        url = pathlib.Path(path).as_uri()

        self.driver.get(url)
        print_options = {
            "landscape": False,
            "displayHeaderFooter": False,
            "printBackground": True,
            "preferCSSPageSize": True,
            "pageSize": "Letter",
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


if __name__ == "__main__":
    vs = VangohanScraper()
    print("fetching menu image")
    vs.save_menu_image()

    print("fetching recipes")
    recipes = vs.fetch_recipes()

    print("parsing html")
    vs.save_recipes(recipes, "vangohan.md", lang="ja")
    md2html("vangohan.md", "vangohan.html")
    pathlib.Path("results").mkdir(parents=True, exist_ok=True)

    print("Saving PDF")
    vs.html2pdf2("vangohan.html", pathlib.Path("results", "vangohan.pdf"))
    vs.save_recipes(recipes, "vangohan-en.md", lang="en")
    md2html("vangohan-en.md", "vangohan-en.html")
    vs.html2pdf2("vangohan-en.html", pathlib.Path("results", "vangohan_en.pdf"))
