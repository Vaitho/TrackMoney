import time
from datetime import datetime

import charts
from config import TZ

PNG = b"\x89PNG"


def _rows(now: datetime) -> list[dict]:
    categories = ["Casa", "Svago", "Alimentari", "Trasporti", "Salute", "Shopping", "Altro", "Animali", "Istruzione"]
    return [
        {"category": c, "amount_cents": 1000 * (i + 1), "spent_at": int(now.timestamp()) - i * 3600}
        for i, c in enumerate(categories)
    ]


def test_category_totals_fold_small_categories_into_altre():
    totals = charts.category_totals(_rows(datetime.now(TZ)))
    assert len(totals) == charts.MAX_CATEGORIES and totals[-1][0] == "Altre"
    assert sum(v for _, v in totals) == sum(1000 * (i + 1) for i in range(9))


def test_report_images_are_rendered():
    now = datetime.now(TZ)
    start = datetime(now.year, now.month, 1, tzinfo=TZ)
    for mode, budget in (("month", 150000), ("month", None), ("year", 150000)):
        image = charts.build_report(_rows(now), "Spese di prova", mode, start, budget)
        assert image.getvalue().startswith(PNG)


def test_price_history_image_is_rendered():
    now = int(time.time())
    points = [(now - 5 * 86400, 120.0), (now - 3 * 86400, 95.0), (now - 86400, 79.0)]
    for target in (80.0, None):
        assert charts.build_price_history(points, "Cuffie X", target).getvalue().startswith(PNG)
