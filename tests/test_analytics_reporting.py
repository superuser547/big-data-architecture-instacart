from __future__ import annotations

from instacart.reports import analytics_markdown


def test_markdown_report_exposes_lineage_and_statistical_uncertainty() -> None:
    report = analytics_markdown(
        {
            "status": "succeeded",
            "dataset_fingerprint": "fingerprint",
            "ingestion_run_id": "ingestion-id",
            "marts_run_id": "marts-id",
            "analytics_run_id": "analysis-id",
            "duration_seconds": 12.5,
            "mart_size_bytes": 1024,
            "stage_durations_seconds": {"clustering": 2.0, "hypotheses": 9.0},
            "hypotheses": {
                "H1": {
                    "observations": 10,
                    "statistic": 2.0,
                    "p_value": 0.04,
                    "cramers_v": 0.1,
                    "p_value_method": "Pearson chi-square on all orders",
                    "bootstrap": {
                        "cramers_v_ci": [0.05, 0.15],
                        "sampled_customers": 4,
                        "replicates": 20,
                        "random_seed": 2026,
                    },
                },
                "H2": {"tests": {}, "bootstrap": {}},
                "H3": {
                    "orders_with_basket_size_at_least_20": 0,
                    "position_reorder_rates": {"1": None, "10": None, "20": None},
                    "pairwise_mcnemar": {},
                    "bootstrap": {"position_rate_range_ci": None},
                },
            },
        }
    )

    assert "ingestion-id" in report and "marts-id" in report and "analysis-id" in report
    assert "Размер схемы `mart`: 1024 байт" in report
    assert "Скорректированный Cramér’s V" in report
    assert "bootstrap по 4 покупателям, 20 повторов, seed 2026" in report
    assert "не корректируют зависимость между" in report
    assert "заказами с корзиной не менее 20" in report
