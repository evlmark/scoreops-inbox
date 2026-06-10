# filename: scraper_v2.py
#
# Наш собственный парсер Google Sites.
# Берёт ТОЛЬКО контент страницы (контейнер [role="main"]),
# без навигации, без дублей, без картинок.
#
# Workflow:
#   1. Открывается видимый Chrome -> вы логинитесь (Google + 2FA)
#   2. Скрипт рекурсивно обходит все внутренние страницы сайта
#   3. Из каждой берёт только основной контент -> чистый Markdown
#   4. Складывает результат в clean_output/ (по .md на страницу) + knowledge.json
#
# Картинки НЕ скачиваются — наш оценщик (DeepSeek) работает только с текстом.

import asyncio
import sys
import os
import re
import json
from urllib.parse import urljoin, urldefrag, urlparse, unquote
from playwright.async_api import async_playwright, TimeoutError

# --- Configuration ---
START_URL = "https://sites.google.com/dif.tech/pyme/"
# Всё, что начинается с этого префикса, считается "внутренней" страницей сайта
BASE_PREFIX = "https://sites.google.com/dif.tech/pyme"
OUTPUT_DIR = "clean_output"
NAV_TIMEOUT = 90000
SETTLE_MS = 2500


# ----------------------------------------------------------------------------
# HTML -> Markdown (минималистичный конвертер только нужных тегов)
# ----------------------------------------------------------------------------
def html_node_to_markdown(soup_node):
    """Конвертирует BeautifulSoup-узел контента в чистый Markdown."""
    from bs4 import NavigableString, Tag

    lines = []

    def render_inline(node):
        """Текст с инлайновым форматированием (ссылки, жирный)."""
        parts = []
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag):
                name = child.name.lower()
                if name == "a":
                    text = child.get_text(strip=True)
                    href = child.get("href", "")
                    if text and href and href.startswith("http"):
                        parts.append(f"[{text}]({href})")
                    else:
                        parts.append(text)
                elif name in ("b", "strong"):
                    parts.append(f"**{child.get_text(strip=True)}**")
                elif name in ("i", "em"):
                    parts.append(f"*{child.get_text(strip=True)}*")
                elif name == "br":
                    parts.append("\n")
                else:
                    parts.append(render_inline(child))
        return "".join(parts)

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                txt = str(child).strip()
                if txt:
                    lines.append(txt)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()

            if name in ("script", "style", "img", "svg", "noscript", "iframe"):
                continue
            elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(name[1])
                text = render_inline(child).strip()
                if text:
                    lines.append("\n" + "#" * level + " " + text + "\n")
            elif name == "p":
                text = render_inline(child).strip()
                if text:
                    lines.append(text + "\n")
            elif name in ("ul", "ol"):
                ordered = name == "ol"
                idx = 1
                for li in child.find_all("li", recursive=False):
                    text = render_inline(li).strip()
                    if text:
                        prefix = f"{idx}. " if ordered else "- "
                        lines.append(prefix + text)
                        idx += 1
                lines.append("")
            elif name == "table":
                md_table = render_table(child)
                if md_table:
                    lines.append(md_table + "\n")
            elif name in ("div", "section", "article", "span", "main", "header", "footer"):
                walk(child)
            else:
                walk(child)

    def render_table(table):
        # Читаем ячейки с учётом colspan: (текст, сколько колонок занимает).
        raw_rows = []
        for tr in table.find_all("tr"):
            cells = []
            for c in tr.find_all(["td", "th"]):
                text = re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip()
                text = text.replace("|", "\\|")
                try:
                    span = max(1, int(c.get("colspan", 1)))
                except (TypeError, ValueError):
                    span = 1
                cells.append((text, span))
            if cells:
                raw_rows.append(cells)
        if not raw_rows:
            return ""

        ncol = max(sum(s for _, s in r) for r in raw_rows)
        rows = []
        for cells in raw_rows:
            # строка из одной ячейки во всю ширину — это заголовок-разделитель:
            # оставляем текст один раз, остальные колонки пустые
            if len(cells) == 1 and cells[0][1] >= ncol:
                rows.append([cells[0][0]] + [""] * (ncol - 1))
                continue
            # иначе объединённую ячейку дублируем во все её колонки,
            # чтобы значение не выглядело относящимся только к первой
            expanded = []
            for text, span in cells:
                expanded.extend([text] * span)
            if len(expanded) < ncol:
                expanded += [""] * (ncol - len(expanded))
            rows.append(expanded[:ncol])

        if not any(any(c for c in r) for r in rows):
            return ""
        out = []
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("| " + " | ".join(["---"] * ncol) + " |")
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        return "\n".join(out)

    walk(soup_node)

    # Склейка + чистка лишних пустых строк
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ----------------------------------------------------------------------------
# Сбор внутренних ссылок со страницы
# ----------------------------------------------------------------------------
async def collect_internal_links(page):
    hrefs = await page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    found = set()
    for href in hrefs:
        if not href:
            continue
        full = urljoin(page.url, href)
        full, _ = urldefrag(full)  # убрать #anchor
        full = unquote(full)       # декодируем %C3%B3 -> ó, чтобы не плодить дубли
        if full.startswith(BASE_PREFIX):
            # пропускаем прямые ссылки на файлы / системные
            if "/_/" in full:
                continue
            found.add(full.rstrip("/"))
    return found


def slugify(url):
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] or "home"
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
    return slug or "home"


# ----------------------------------------------------------------------------
# Извлечение контента одной страницы
# ----------------------------------------------------------------------------
async def extract_page(page, url):
    from bs4 import BeautifulSoup

    # domcontentloaded, а не "load" — не ждём тяжёлые медиа (видео и т.п.),
    # которые могут висеть до таймаута. Контента это не касается:
    # встроенные таблицы (srcdoc) успевают подгрузиться за SETTLE_MS.
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    except TimeoutError:
        await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(SETTLE_MS)

    # Помечаем каждый <iframe> индексом — таблицы и др. вставки на Google Sites
    # рендерятся как встроенный HTML ВНУТРИ iframe, а не в основном документе.
    await page.evaluate(
        "() => document.querySelectorAll('iframe')"
        ".forEach((f,i)=>f.setAttribute('data-embed-idx', String(i)))"
    )
    await page.wait_for_timeout(300)

    title = (await page.title()) or slugify(url)

    # В этом макете Google Sites реальный контент статьи лежит в <div>,
    # который идёт СРАЗУ ПОСЛЕ <header id="atIdViewHeader"> (это шапка/навигация).
    # Берём innerHTML этого блока, заменяя каждый iframe на текстовый плейсхолдер.
    main_html = await page.evaluate(
        """() => {
            const h = document.querySelector('#atIdViewHeader');
            let el = (h && h.nextElementSibling) ? h.nextElementSibling
                   : (document.querySelector('[role=main]')
                      || document.querySelector('[data-test-id=content]')
                      || document.querySelector('main'));
            if (!el) return '';
            const clone = el.cloneNode(true);
            clone.querySelectorAll('iframe').forEach(f => {
                const idx = f.getAttribute('data-embed-idx');
                const marker = document.createElement('p');
                marker.textContent = 'EMBEDPLACEHOLDER' + idx + 'END';
                f.replaceWith(marker);
            });
            return clone.innerHTML;
        }"""
    )

    new_links = await collect_internal_links(page)

    if not main_html:
        return None, new_links

    # Конвертируем содержимое встроенных фреймов (таблицы и пр.) в Markdown.
    # Вставка на Google Sites = тройная вложенность iframe:
    #   main -> <iframe data-embed-idx> (обёртка) -> iframe -> iframe srcdoc (таблица).
    # Контент в листовом фрейме; индекс плейсхолдера — на верхней обёртке.
    async def safe_eval(fr, expr, timeout=8):
        # фрейм с видео/картой может висеть — ограничиваем время ожидания
        try:
            return await asyncio.wait_for(fr.evaluate(expr), timeout)
        except Exception:
            return None

    embeds = {}
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        # пропускаем заведомо медийные фреймы (видео, карты) — текста там нет
        if any(s in fr.url for s in ("youtube", "ytimg", "video", "google.com/maps", "drive.google.com/file")):
            continue
        try:
            # берём только листовые фреймы (без вложенных iframe) с контентом
            nchild = await safe_eval(fr, "() => document.querySelectorAll('iframe').length")
            if nchild is None or nchild:
                continue
            fhtml = await safe_eval(fr, "() => document.body ? document.body.innerHTML : ''")
            if not fhtml or not fhtml.strip():
                continue
            # поднимаемся к верхней обёртке, чей родитель — главный фрейм
            top = fr
            while top.parent_frame is not None and top.parent_frame is not page.main_frame:
                top = top.parent_frame
            fe = await top.frame_element()
            idx = await fe.get_attribute("data-embed-idx")
            if idx is None:
                continue
            fmd = html_node_to_markdown(BeautifulSoup(fhtml, "lxml"))
            if fmd.strip():
                embeds[idx] = fmd
        except Exception:
            continue

    soup = BeautifulSoup(main_html, "lxml")
    markdown = html_node_to_markdown(soup)

    # Вставляем embed-блоки на места плейсхолдеров; неиспользованные — убираем.
    for idx, fmd in embeds.items():
        markdown = markdown.replace(f"EMBEDPLACEHOLDER{idx}END", "\n" + fmd + "\n")
    markdown = re.sub(r"EMBEDPLACEHOLDER\d+END", "", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return {"url": url, "title": title.strip(), "markdown": markdown}, new_links


# ----------------------------------------------------------------------------
# Основной обход
# ----------------------------------------------------------------------------
async def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    async with async_playwright() as p:
        print("--- 👤 ВАШ ХОД: авторизация в Google ---", flush=True)
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(START_URL)
        print("Залогиньтесь в открытом окне браузера (Google + 2FA)...", flush=True)

        # Ждём, пока окажемся на нужном сайте И появится контент [role=main].
        # Поллинг каждые 2с до 6 минут — устойчивее, чем строгий glob по URL.
        logged_in = False
        for _ in range(180):
            await page.wait_for_timeout(2000)
            try:
                cur = page.url
                if cur.startswith(BASE_PREFIX):
                    has_main = await page.evaluate(
                        "() => !!(document.querySelector('[role=main]')"
                        " || document.querySelector('[data-test-id=content]')"
                        " || document.querySelector('main'))"
                    )
                    if has_main:
                        logged_in = True
                        break
            except Exception:
                # во время редиректов логина page.url/evaluate могут кидать — игнорируем
                continue

        if not logged_in:
            print("❌ Логин не дождался за 6 минут. Выход.", flush=True)
            await browser.close()
            return

        # Сохраняем cookies — пригодятся для headless-запуска на Railway позже.
        try:
            cookies = await context.cookies()
            with open(os.path.join(OUTPUT_DIR, "_cookies.json"), "w", encoding="utf-8") as f:
                json.dump(cookies, f)
        except Exception:
            pass

        await page.wait_for_timeout(1500)
        print("✅ Логин ок. Начинаю обход сайта.\n", flush=True)

        # BFS-обход
        visited = set()
        queue = [START_URL.rstrip("/")]
        results = []

        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            n = len(visited)
            print(f"({n}) Парсю: {url}")
            try:
                data, links = await extract_page(page, url)
                if data and data["markdown"]:
                    results.append(data)
                    chars = len(data["markdown"])
                    print(f"      ✅ контент: {chars} символов")
                    # сохраняем .md сразу
                    slug = slugify(url)
                    fname = f"{n:02d}_{slug}.md"
                    with open(os.path.join(OUTPUT_DIR, fname), "w", encoding="utf-8") as f:
                        f.write(f"# {data['title']}\n\n")
                        f.write(f"<!-- source: {url} -->\n\n")
                        f.write(data["markdown"])
                else:
                    print("      ⚠️  пусто (нет [role=main] или контента)")
                # добавляем новые ссылки в очередь
                for link in sorted(links):
                    if link not in visited and link not in queue:
                        queue.append(link)
            except Exception as e:
                print(f"      ❌ ошибка: {e}")

        await browser.close()

        # сводный JSON
        json_path = os.path.join(OUTPUT_DIR, "knowledge.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        total_chars = sum(len(r["markdown"]) for r in results)
        print("\n--- 🎉 Готово ---")
        print(f"Страниц с контентом: {len(results)}")
        print(f"Всего символов текста: {total_chars:,}")
        print(f"Результат: {OUTPUT_DIR}/  (по .md на страницу + knowledge.json)")


if __name__ == "__main__":
    asyncio.run(main())
