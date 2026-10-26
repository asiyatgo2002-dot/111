"""Агент: план → сбор данных → фильтрация → ранжирование → выводы → Excel.

Примеры:
    python -m marketplace_agent                         # домашний текстиль (по умолчанию)
    python -m marketplace_agent "беспроводные наушники" --top 15
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .categories import HOME_TEXTILE, Category, find_category
from .excel_report import save_report
from .llm import Analyst
from .models import Product
from .ozon import OzonClient
from .scoring import (ScoringConfig, dedupe, fill_sales_estimates, filter_candidates, score,
                      select_top)
from .wildberries import WildberriesClient

log = logging.getLogger("marketplace_agent")


@dataclass(frozen=True)
class SearchTask:
    query: str
    subcategory: str = ""


def _slug(text: str) -> str:
    return re.sub(r"[^\w]+", "_", text.lower()).strip("_")[:40] or "report"


def collect(tasks: list[SearchTask], marketplaces: set[str], pages: int, ozon_visible: bool,
            notes: list[str]) -> list[Product]:
    products: list[Product] = []
    clients = []
    if "wb" in marketplaces:
        clients.append(("Wildberries", WildberriesClient()))
    if "ozon" in marketplaces:
        clients.append(("Ozon", OzonClient(headless=not ozon_visible)))
    for name, client in clients:
        for task in tasks:
            q = task.query
            try:
                found = client.search(q, pages=pages)
                for p in found:
                    p.subcategory = task.subcategory
                log.info("%s «%s»: %d товаров", name, q, len(found))
                products.extend(found)
            except Exception as exc:  # один упавший источник не должен ломать весь отчёт
                log.error("%s «%s»: ошибка — %s", name, q, exc)
                notes.append(f"{name}, запрос «{q}»: данные не получены ({exc})")
    return products


def run(topic: str, top_n: int = 15, marketplaces: set[str] | None = None, pages: int | None = None,
        output: Path | None = None, use_ai: bool = True, ozon_visible: bool = False,
        cfg: ScoringConfig | None = None, max_per_subcategory: int | None = None) -> Path:
    cfg = cfg or ScoringConfig()
    marketplaces = marketplaces or {"wb", "ozon"}
    notes: list[str] = []
    analyst = Analyst.maybe_create() if use_ai else None
    if use_ai and not analyst:
        notes.append("ИИ-анализ отключён: не задан ANTHROPIC_API_KEY (работают только эвристики).")

    category: Category | None = find_category(topic)
    exclude: dict[str, tuple[str, ...]] = {}
    if category:
        # Категория: заранее подобранные запросы по каждой подкатегории
        topic = category.title
        tasks = [SearchTask(q, sub.name) for sub in category.subcategories for q in sub.queries]
        exclude = {sub.name: sub.exclude + category.common_exclude for sub in category.subcategories}
        pages = pages or 1
        if max_per_subcategory is None:
            max_per_subcategory = category.max_per_subcategory
    else:
        tasks = [SearchTask(topic)]
        pages = pages or 2
        if analyst:
            try:
                tasks = [SearchTask(q) for q in analyst.plan_queries(topic)]
            except Exception as exc:
                log.warning("ИИ не составил запросы (%s), используется тема как есть", exc)
    max_per_subcategory = max_per_subcategory or 0
    queries = [t.query for t in tasks]
    log.info("Поисковые запросы: %s", queries)

    raw = dedupe(collect(tasks, marketplaces, pages, ozon_visible, notes))
    if not raw:
        raise RuntimeError("Не удалось получить ни одного товара. " + " ".join(notes))
    fill_sales_estimates(raw, cfg)
    candidates = filter_candidates(raw, cfg, exclude)
    log.info("Собрано %d, после фильтров %d", len(raw), len(candidates))

    if analyst and candidates:
        try:
            relevant = analyst.filter_relevant(topic, candidates)
            if relevant:
                log.info("ИИ оставил %d релевантных из %d", len(relevant), len(candidates))
                candidates = relevant
        except Exception as exc:
            log.warning("ИИ-фильтр не сработал (%s)", exc)

    ranked = score(candidates, cfg)
    top = select_top(ranked, top_n, max_per_subcategory)

    summary = ""
    if analyst and top:
        try:
            summary, comments = analyst.summarize(topic, top)
            for p, c in zip(top, comments):
                p.comment = c
        except Exception as exc:
            log.warning("ИИ-вывод не сформирован (%s)", exc)
    if max_per_subcategory:
        notes.append(f"В общий топ берётся не больше {max_per_subcategory} товаров одной подкатегории, "
                     "чтобы он не состоял из одного типа товаров (--max-per-subcategory 0 — без ограничения).")

    now = datetime.now()
    output = output or Path("reports") / f"top{top_n}_{_slug(topic)}_{now:%Y-%m-%d_%H%M}.xlsx"
    save_report(output, topic, queries, top, ranked, cfg, summary, notes, now, top_n)
    # Постоянный файл с актуальным топом — удобно открывать/подключать к таблицам
    latest = output.parent / f"top{top_n}_{_slug(topic)}_latest.xlsx"
    shutil.copyfile(output, latest)
    return output


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="marketplace_agent",
        description="Исследует Ozon и Wildberries и сохраняет топ товаров (продажи + цена/качество) в Excel.",
    )
    ap.add_argument("topic", nargs="?", default=HOME_TEXTILE.title,
                    help="категория или товар; по умолчанию «домашний текстиль» "
                         "(постельное бельё, полотенца, пледы, подушки, одеяла, шторы и др.)")
    ap.add_argument("--top", type=int, default=15, help="размер топа (по умолчанию 15)")
    ap.add_argument("--marketplaces", default="wb,ozon", help="wb,ozon | wb | ozon")
    ap.add_argument("--pages", type=int, help="страниц выдачи на запрос (по умолчанию 1 для категории, 2 для товара)")
    ap.add_argument("--max-per-subcategory", type=int,
                    help="сколько товаров одной подкатегории максимум в топе (для категории по умолчанию 4, 0 — без ограничения)")
    ap.add_argument("--output", type=Path, help="путь к .xlsx (по умолчанию reports/...)")
    ap.add_argument("--min-rating", type=float, default=4.5)
    ap.add_argument("--min-reviews", type=int, default=30)
    ap.add_argument("--no-ai", action="store_true", help="не использовать Claude")
    ap.add_argument("--ozon-visible", action="store_true", help="открыть браузер для Ozon в видимом режиме")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = ScoringConfig(min_rating=args.min_rating, min_reviews=args.min_reviews)
    mps = {m.strip().lower() for m in args.marketplaces.split(",") if m.strip()}
    try:
        path = run(args.topic, args.top, mps, args.pages, args.output, not args.no_ai,
                   args.ozon_visible, cfg, args.max_per_subcategory)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    print(f"Готово: {path.resolve()}")
    return 0
