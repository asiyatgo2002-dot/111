"""Агент: план → сбор данных → фильтрация → ранжирование → выводы → Excel.

Пример:
    python -m marketplace_agent "беспроводные наушники" --top 15
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from .excel_report import save_report
from .llm import Analyst
from .models import Product
from .ozon import OzonClient
from .scoring import ScoringConfig, dedupe, fill_sales_estimates, filter_candidates, score
from .wildberries import WildberriesClient

log = logging.getLogger("marketplace_agent")


def _slug(text: str) -> str:
    return re.sub(r"[^\w]+", "_", text.lower()).strip("_")[:40] or "report"


def collect(queries: list[str], marketplaces: set[str], pages: int, ozon_visible: bool,
            notes: list[str]) -> list[Product]:
    products: list[Product] = []
    clients = []
    if "wb" in marketplaces:
        clients.append(("Wildberries", WildberriesClient()))
    if "ozon" in marketplaces:
        clients.append(("Ozon", OzonClient(headless=not ozon_visible)))
    for name, client in clients:
        for q in queries:
            try:
                found = client.search(q, pages=pages)
                log.info("%s «%s»: %d товаров", name, q, len(found))
                products.extend(found)
            except Exception as exc:  # один упавший источник не должен ломать весь отчёт
                log.error("%s «%s»: ошибка — %s", name, q, exc)
                notes.append(f"{name}, запрос «{q}»: данные не получены ({exc})")
    return products


def run(topic: str, top_n: int = 15, marketplaces: set[str] | None = None, pages: int = 2,
        output: Path | None = None, use_ai: bool = True, ozon_visible: bool = False,
        cfg: ScoringConfig | None = None) -> Path:
    cfg = cfg or ScoringConfig()
    marketplaces = marketplaces or {"wb", "ozon"}
    notes: list[str] = []
    analyst = Analyst.maybe_create() if use_ai else None
    if use_ai and not analyst:
        notes.append("ИИ-анализ отключён: не задан ANTHROPIC_API_KEY (работают только эвристики).")

    queries = [topic]
    if analyst:
        try:
            queries = analyst.plan_queries(topic)
        except Exception as exc:
            log.warning("ИИ не составил запросы (%s), используется тема как есть", exc)
    log.info("Поисковые запросы: %s", queries)

    raw = dedupe(collect(queries, marketplaces, pages, ozon_visible, notes))
    if not raw:
        raise RuntimeError("Не удалось получить ни одного товара. " + " ".join(notes))
    fill_sales_estimates(raw, cfg)
    candidates = filter_candidates(raw, cfg)
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
    top = ranked[:top_n]

    summary = ""
    if analyst and top:
        try:
            summary, comments = analyst.summarize(topic, top)
            for p, c in zip(top, comments):
                p.comment = c
        except Exception as exc:
            log.warning("ИИ-вывод не сформирован (%s)", exc)

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
    ap.add_argument("topic", help="что ищем, например: «беспроводные наушники»")
    ap.add_argument("--top", type=int, default=15, help="размер топа (по умолчанию 15)")
    ap.add_argument("--marketplaces", default="wb,ozon", help="wb,ozon | wb | ozon")
    ap.add_argument("--pages", type=int, default=2, help="страниц выдачи на запрос (~100 товаров на WB, ~36 на Ozon)")
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
                   args.ozon_visible, cfg)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    print(f"Готово: {path.resolve()}")
    return 0
