from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


def _page_fixture(page: str) -> tuple[str, str]:
    readiness = (
        "Readiness(True, True, True, 'sha256:sample', 'ingestion-1', 'marts-1', 'analysis-1')"
    )
    if page == "overview":
        fixture = """{
            'overview': {'user_count': 2, 'order_count': 3, 'known_item_count': 4,
                'product_count': 5, 'avg_basket_size': 2.0, 'repeat_rate': 0.25,
                'known_order_count': 2, 'prior_order_count': 1, 'train_order_count': 1,
                'test_order_count': 1},
            'activity': [{'order_dow': 0, 'order_hour_of_day': 9, 'order_count': 2}],
            'known_summary': {'known_order_count': 1, 'buyer_count': 1,
                'item_count': 2, 'repeated_item_count': 1, 'avg_basket_size': 2.0},
            'basket_counts': [{'basket_from': 1, 'order_count': 1}],
            'intervals': [{'interval_days': 7, 'order_count': 1}],
        }"""
        setup = "overview.load_overview = lambda *args, **kwargs: fixture"
        render = "overview.render(readiness, 'postgresql://fixture')"
    elif page == "categories":
        fixture = """{
            'options': [
                {'group_type': 'department', 'group_id': 1, 'group_name': 'Produce',
                 'department_id': 1, 'department_name': 'Produce'},
                {'group_type': 'aisle', 'group_id': 3, 'group_name': 'Fresh Fruits',
                 'department_id': 1, 'department_name': 'Produce'},
            ],
            'popularity': [
                {'group_type': 'department', 'group_id': 1, 'group_name': 'Produce',
                 'department_id': 1, 'department_name': 'Produce', 'item_count': 5,
                 'reorder_count': 2, 'order_count': 3},
                {'group_type': 'aisle', 'group_id': 3, 'group_name': 'Fresh Fruits',
                 'department_id': 1, 'department_name': 'Produce', 'item_count': 5,
                 'reorder_count': 2, 'order_count': 3},
            ],
            'products': [{'product_id': 12, 'product_name': 'Apples', 'aisle_id': 3,
                'aisle_name': 'Fresh Fruits', 'department_id': 1, 'department_name': 'Produce',
                'item_count': 5, 'reorder_count': 2, 'order_count': 3}],
            'activity': [{'category_id': 1, 'category_name': 'Produce',
                'item_count': 5, 'reorder_count': 2}],
        }"""
        setup = "categories.load_category_data = lambda *args, **kwargs: fixture"
        render = "categories.render(readiness, 'postgresql://fixture')"
    elif page == "hypotheses":
        fixture = """{
            'results': [
                {'hypothesis_id': 'H1', 'h0': 'H0 time', 'h1': 'H1 time', 'method': 'χ²',
                 'observation_count': 10, 'statistic': 2.0, 'p_value': 0.2,
                 'effect_name': 'Cramér V', 'effect_value': 0.1, 'ci_lower': 0.0,
                 'ci_upper': 0.2, 'interpretation': 'Описательная связь',
                 'metrics': {'observed': [[1] * 24 for _ in range(7)],
                     'row_normalized': [[1 / 24] * 24 for _ in range(7)],
                     'pearson_residuals': [[0] * 24 for _ in range(7)],
                     'log10_p_value': -0.7}},
                {'hypothesis_id': 'H2_department', 'h0': 'H0 department', 'h1': 'H1 department',
                 'method': 'χ²', 'observation_count': 10, 'statistic': 2.0, 'p_value': 0.2,
                 'effect_name': 'Cramér V', 'effect_value': 0.1, 'ci_lower': 0.0,
                 'ci_upper': 0.2, 'interpretation': 'Описательная связь',
                 'metrics': {'log10_p_value': -0.7,
                     'holm_adjusted_p_value_across_category_levels': 0.4}},
                {'hypothesis_id': 'H2_aisle', 'h0': 'H0 aisle', 'h1': 'H1 aisle', 'method': 'χ²',
                 'observation_count': 10, 'statistic': 2.0, 'p_value': 0.2,
                 'effect_name': 'Cramér V', 'effect_value': 0.1, 'ci_lower': 0.0,
                 'ci_upper': 0.2, 'interpretation': 'Описательная связь',
                 'metrics': {'log10_p_value': -0.7,
                     'holm_adjusted_p_value_across_category_levels': 0.4}},
                {'hypothesis_id': 'H3', 'h0': 'H0 positions', 'h1': 'H1 positions',
                 'method': 'Cochran Q', 'observation_count': 5, 'statistic': 1.0,
                 'p_value': 0.3, 'effect_name': 'Range', 'effect_value': 0.1,
                 'ci_lower': 0.05, 'ci_upper': 0.15, 'interpretation': 'Различий мало',
                 'metrics': {'cochran_q_log10_p_value': -0.5,
                     'position_reorder_rates': {'1': 0.2, '10': 0.3, '20': 0.4}}},
            ],
            'categories': [
                {'hypothesis_id': 'H2_department', 'category_level': 'department',
                 'category_id': 1, 'category_name': 'Produce', 'item_count': 5,
                 'reorder_count': 2, 'reorder_rate': 0.4, 'ci_lower': 0.2, 'ci_upper': 0.6,
                 'eval_set': 'prior+train', 'cart_position': 0},
                {'hypothesis_id': 'H3_category', 'category_level': 'department',
                 'category_id': 1, 'category_name': 'Produce', 'item_count': 2,
                 'reorder_count': 1, 'reorder_rate': 0.5, 'ci_lower': 0.1, 'ci_upper': 0.9,
                 'eval_set': 'prior', 'cart_position': 1},
                {'hypothesis_id': 'H3_category', 'category_level': 'department',
                 'category_id': 1, 'category_name': 'Produce', 'item_count': 2,
                 'reorder_count': 1, 'reorder_rate': 0.5, 'ci_lower': 0.1, 'ci_upper': 0.9,
                 'eval_set': 'train', 'cart_position': 10},
            ],
            'pairs': [
                {'comparison': '1_vs_10', 'first_position': 1, 'second_position': 10,
                 'department_matched': matched, 'order_count': 4, 'discordant_first_only': 1,
                 'discordant_second_only': 1, 'risk_difference': 0.0, 'ci_lower': -0.1,
                 'ci_upper': 0.1, 'p_value': 0.8, 'p_adjusted': 1.0}
                for matched in (False, True)
            ],
        }"""
        setup = (
            "hypotheses.load_hypotheses = lambda *args, **kwargs: fixture\n"
            "hypotheses.load_h1_slice = lambda *args, **kwargs: "
            "[{'eval_set': 'prior', 'order_dow': 0, 'order_hour_of_day': 9, 'order_count': 2}]"
        )
        render = "hypotheses.render(readiness, 'postgresql://fixture')"
    else:
        fixture = """{
            'profiles': [
                {'cluster_id': 0, 'customer_count': 4, 'feature_means': {'mean': {
                    'prior_order_count': 2.0, 'avg_basket_size': 3.0, 'reorder_rate': 0.2,
                    'avg_days_between_orders': 8.0, 'unique_product_count': 4.0,
                    'unique_aisle_count': 2.0, 'unique_department_count': 1.0}}},
                {'cluster_id': 1, 'customer_count': 4, 'feature_means': {'mean': {
                    'prior_order_count': 4.0, 'avg_basket_size': 5.0, 'reorder_rate': 0.5,
                    'avg_days_between_orders': 5.0, 'unique_product_count': 8.0,
                    'unique_aisle_count': 4.0, 'unique_department_count': 2.0}}},
            ],
            'metrics': [
                {'k': 2, 'inertia': 10.0, 'silhouette': 0.4,
                 'silhouette_sample_size': 8, 'selected': True},
                {'k': 3, 'inertia': 7.0, 'silhouette': 0.3,
                 'silhouette_sample_size': 8, 'selected': False},
            ],
            'points': [
                {'cluster_id': 0, 'pca_1': 0.1, 'pca_2': 0.2},
                {'cluster_id': 1, 'pca_1': 0.8, 'pca_2': 0.9},
            ],
            'population_size': 8, 'selected_k': 2, 'selection_basis': 'maximum silhouette',
            'pca_explained_variance_ratio': [0.4, 0.2], 'displayed_sample_size': 2,
            'sample_limit': 12000,
        }"""
        setup = "segmentation.load_segments = lambda *args, **kwargs: fixture"
        render = "segmentation.render(readiness, 'postgresql://fixture')"

    modules = {
        "overview": "from instacart.dashboard.pages import overview",
        "categories": "from instacart.dashboard.pages import categories",
        "hypotheses": "from instacart.dashboard.pages import hypotheses",
        "segmentation": "from instacart.dashboard.pages import segmentation",
    }
    script = (
        "from instacart.dashboard.data import Readiness\n"
        f"{modules[page]}\n"
        f"readiness = {readiness}\n"
        f"fixture = {fixture}\n"
        f"{setup}\n"
        f"{render}\n"
    )
    return script, page


@pytest.mark.parametrize("page", ["overview", "categories", "hypotheses", "segmentation"])
def test_dashboard_pages_smoke_render_with_fixture_data(tmp_path: Path, page: str) -> None:

    script, _page = _page_fixture(page)
    app_path = tmp_path / f"{_page}_smoke.py"
    app_path.write_text(script, encoding="utf-8")
    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert len(app.exception) == 0
