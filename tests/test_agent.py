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
