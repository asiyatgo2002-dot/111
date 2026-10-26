import json
from pathlib import Path

from openpyxl import load_workbook

from marketplace_agent import agent, ozon, wildberries
from marketplace_agent.scoring import ScoringConfig, fill_sales_estimates, filter_candidates, score

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def wb_products():
    return [p for p in map(wildberries.parse_product, load("wb_search.json")["products"]) if p]


def test_wb_parse_prices_in_rubles_and_skips_unavailable():
    products = {p.sku: p for p in wb_products()}
    assert "1005" not in products
    p = products["1001"]
    assert (p.price, p.old_price, p.discount_pct) == (2499.0, 4999.0, 50.0)
    assert p.rating == 4.8 and p.reviews == 12000
    assert products["1006"].price == 890.0  # старый формат salePriceU
    assert wildberries.parse_orders([{"nmId": 1001, "qnt": 50000}]) == {"1001": 50000}


def test_ozon_parse_search_page():
    products = {p.sku: p for p in ozon.parse_search_page(load("ozon_search.json"), "наушники")}
    assert set(products) == {"555001", "777002"}
    aero = products["555001"]
    assert aero.name == "Наушники беспроводные Aero"
    assert (aero.price, aero.old_price) == (1890.0, 3990.0)
    assert (aero.rating, aero.reviews, aero.sales) == (4.7, 8512, 2000)
    assert aero.url == "https://www.ozon.ru/product/naushniki-aero-555001/"
    bass = products["777002"]
    assert (bass.rating, bass.reviews, bass.sales) == (4.9, 1204, None)


def test_filters_and_scoring():
    cfg = ScoringConfig()
    products = wb_products()
    fill_sales_estimates(products, cfg)
    assert products[0].sales == 240000 and "оценка" in products[0].sales_source
    cands = filter_candidates(products, cfg)
    skus = {p.sku for p in cands}
    # 1003 — дешёвый аксессуар, 1004 — мало отзывов, 1006 — низкий рейтинг
    assert skus == {"1001", "1002"}
    ranked = score(cands, cfg)
    assert ranked[0].total_score >= ranked[1].total_score
    assert all(0 <= p.total_score <= 100 for p in ranked)


def test_run_writes_excel(tmp_path, monkeypatch):
    monkeypatch.setattr(wildberries.WildberriesClient, "search", lambda self, q, pages=2: wb_products())
    monkeypatch.setattr(ozon.OzonClient, "search",
                        lambda self, q, pages=2: ozon.parse_search_page(load("ozon_search.json"), q))
    out = agent.run("наушники", top_n=15, output=tmp_path / "r.xlsx", use_ai=False)
    wb = load_workbook(out)
    assert wb.sheetnames[:3] == ["Топ-15", "Wildberries", "Ozon"]
    ws = wb["Топ-15"]
    assert ws["C4"].value == "Товар"
    names = [ws.cell(row=r, column=3).value for r in range(5, ws.max_row + 1)]
    assert len(names) == 4 and "Чехол для наушников" not in names
    assert (tmp_path / "top15_наушники_latest.xlsx").exists()


def test_source_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    def boom(self, q, pages=2):
        raise RuntimeError("антибот")
    monkeypatch.setattr(wildberries.WildberriesClient, "search", lambda self, q, pages=2: wb_products())
    monkeypatch.setattr(ozon.OzonClient, "search", boom)
    out = agent.run("наушники", output=tmp_path / "r.xlsx", use_ai=False)
    notes = [c.value for c in load_workbook(out)["Методика"]["B"]]
    assert any("антибот" in str(n) for n in notes)


def fake_textile_search(self, q, pages=2):
    """По 6 товаров на запрос с разными ценами/продажами + аксессуар в начале выдачи."""
    from marketplace_agent.models import Product
    base = {"постельное белье": 2500, "одеяло": 3000, "плед": 1500}.get(q, 900)
    items = [Product("Wildberries", f"{q}-{i}", f"{q.capitalize()} модель {i}", f"https://x/{q}/{i}",
                     price=base * (1 + i / 5), rating=4.6 + i * 0.05, reviews=500 * (6 - i))
             for i in range(6)]
    items.append(Product("Wildberries", f"{q}-acc", "Карниз для штор" if "штор" in q else "Держатель",
                         f"https://x/{q}/acc", price=base, rating=4.9, reviews=99999))
    if q == "постельное белье":
        items.append(Product("Wildberries", "bed-set", "Комплект постельного белья с наволочками",
                             "https://x/bed", price=2600, rating=4.8, reviews=100000))
    return items


def test_home_textile_category(tmp_path, monkeypatch):
    from marketplace_agent.categories import HOME_TEXTILE, find_category
    assert find_category("Домашний текстиль") is HOME_TEXTILE
    assert find_category("home-textile") is HOME_TEXTILE
    assert find_category("наушники") is None

    calls = []
    def wb_search(self, q, pages=2):
        calls.append((q, pages))
        return fake_textile_search(self, q, pages)
    monkeypatch.setattr(wildberries.WildberriesClient, "search", wb_search)
    monkeypatch.setattr(ozon.OzonClient, "search", lambda self, q, pages=2: [])
    out = agent.run("домашний текстиль", output=tmp_path / "t.xlsx", use_ai=False)

    all_queries = [q for sub in HOME_TEXTILE.subcategories for q in sub.queries]
    assert [q for q, _ in calls] == all_queries and all(p == 1 for _, p in calls)

    wb = load_workbook(out)
    assert {"Топ-15", "По категориям", "Все кандидаты", "Методика"} <= set(wb.sheetnames)
    ws = wb["Топ-15"]
    header = [c.value for c in ws[4]]
    assert header[:4] == ["№", "Маркетплейс", "Категория", "Товар"]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=5)]
    assert len(rows) == 15
    subs = [r[2] for r in rows]
    assert max(subs.count(s) for s in set(subs)) <= HOME_TEXTILE.max_per_subcategory
    names = [r[3] for r in rows]
    all_names = [r[3].value for r in wb["Все кандидаты"].iter_rows(min_row=2)]
    assert "Комплект постельного белья с наволочками" in names  # «наволочк» не в начале — не аксессуар
    assert not any(n.startswith(("Карниз", "Держатель")) for n in all_names)


def test_select_top_cap():
    from marketplace_agent.models import Product
    from marketplace_agent.scoring import select_top
    ranked = [Product("WB", str(i), "x", "u", 1, subcategory="A" if i < 5 else "B") for i in range(8)]
    assert [p.sku for p in select_top(ranked, 4, 2)] == ["0", "1", "5", "6"]
    assert len(select_top(ranked, 4, 0)) == 4


def test_ozon_textds_name_and_variants():
    from marketplace_agent.models import Product
    from marketplace_agent.scoring import select_top
    item = {"id": "123456", "action": {"link": "/product/pled-123456/"},
            "mainState": [{"type": "priceV2", "priceV2": {"price": [{"text": "899 ₽"}]}},
                          {"type": "textDS", "id": "name",
                           "textDS": {"text": "Плед 150х200 GALTEX", "textColor": "textPrimary",
                                      "preset": "PRESET_CUSTOM"}}]}
    assert ozon.parse_item(item).name == "Плед 150х200 GALTEX"

    a = Product("Ozon", "1", "Плед мокко", "u", price=800, rating=4.9, reviews=7000)
    b = Product("Ozon", "2", "Плед графит", "u", price=850, rating=4.9, reviews=7004)
    c = Product("Ozon", "3", "Одеяло", "u", price=1200, rating=4.9, reviews=9000)
    assert [p.sku for p in select_top([a, b, c], 3)] == ["1", "3"]
