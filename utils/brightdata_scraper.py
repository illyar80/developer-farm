"""
Bright Data Documentation Scraper (Fallback for Planning Layer)
--------------------------------------------------------------
Используется ТОЛЬКО когда локальная модель/Neo4j не знают библиотеку.
Скрапит официальные docs, очищает HTML, возвращает текст для контекста.
НЕ скрейпит тесты, рубрики или acceptance criteria (Goodhart-proof).
"""
import os
import re
import requests
from bs4 import BeautifulSoup
from utils.output import console


BRIGHTDATA_PROXY = os.getenv("BRIGHTDATA_PROXY", "")
MAX_DOC_CHARS = 2500  # Лимит на одну библиотеку (защита контекстного окна)


def _extract_libraries(text: str) -> list[str]:
    """Эвристика: извлекает имена библиотек из spec/context."""
    patterns = [
        r"(?:import|from)\s+([\w.]+)",
        r"(?:use|install|pip|npm|package)\s+([\w-]+)",
        r"(?:library|framework|tool)\s+([\w-]+)",
    ]
    libs = set()
    for pattern in patterns:
        libs.update(re.findall(pattern, text, re.IGNORECASE))
    # Фильтруем стандартные модули и короткие слова
    return [lib for lib in libs if len(lib) > 2 and lib not in {"os", "sys", "re", "json", "typing", "pathlib"}]


def scrape_library_docs(library: str) -> str:
    """
    Скрапит официальную документацию через Bright Data.
    Возвращает очищенный текст или пустую строку при ошибке.
    """
    if not BRIGHTDATA_PROXY:
        return ""

    console.print(f"[cyan]🌐 Bright Data: Fetching docs for '{library}'...[/]")

    # Приоритетные источники (официальные docs)
    targets = [
        f"https://docs.python.org/3/library/{library}.html",
        f"https://{library}.readthedocs.io/en/latest/",
        f"https://pypi.org/project/{library}/",
        f"https://github.com/{library}/{library}#readme",
    ]

    proxies = {"http": BRIGHTDATA_PROXY, "https": BRIGHTDATA_PROXY}
    headers = {"User-Agent": "DeveloperFarm-PlanningBot/1.0"}

    for url in targets:
        try:
            resp = requests.get(url, proxies=proxies, headers=headers, timeout=12)
            if resp.status_code == 200 and len(resp.text) > 500:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Удаляем мусор
                for tag in soup(["script", "style", "nav", "footer", "header", "sidebar"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    console.print(f"[green]✅ Scraped {len(text)} chars from {url}[/]")
                    return text[:MAX_DOC_CHARS]
        except Exception as e:
            console.print(f"[yellow]⚠ Skipped {url}: {type(e).__name__}[/]")
            continue

    console.print(f"[yellow]⚠ No docs found for '{library}' via Bright Data[/]")
    return ""