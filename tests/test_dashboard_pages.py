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
                'item_count': 5, 'reorder_count': 2, 'reorder_rate': 0.4, 'order_count': 3}],
            'top_products': [{'product_id': 12, 'product_name': 'Apples', 'aisle_id': 3,
                'aisle_name': 'Fresh Fruits', 'department_id': 1, 'department_name': 'Produce',
                'item_count': 5, 'reorder_count': 2, 'reorder_rate': 0.4, 'order_count': 3}],
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
            'position_stats': [
                {'cart_position': 1, 'item_count': 4, 'reorder_count': 2},
                {'cart_position': 10, 'item_count': 2, 'reorder_count': 1},
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
                {'cluster_id': cluster, 'customer_count': 4, 'feature_means': {'mean': {
                    'prior_order_count': 2.0 + cluster, 'avg_basket_size': 3.0 + cluster,
                    'reorder_rate': 0.2 + cluster / 10, 'avg_days_between_orders': 8.0 - cluster,
                    'unique_product_count': 4.0 + cluster, 'unique_aisle_count': 2.0 + cluster,
                    'unique_department_count': 1.0 + cluster}}}
                for cluster in range(5)
            ],
            'metrics': [
                {'k': 2, 'inertia': 10.0, 'silhouette': 0.4,
                 'silhouette_sample_size': 20, 'selected': False},
                {'k': 3, 'inertia': 7.0, 'silhouette': 0.3,
                 'silhouette_sample_size': 20, 'selected': False},
                {'k': 4, 'inertia': 5.0, 'silhouette': 0.2,
                 'silhouette_sample_size': 20, 'selected': False},
                {'k': 5, 'inertia': 3.0, 'silhouette': 0.5,
                 'silhouette_sample_size': 20, 'selected': True},
            ],
            'points': [
                {'cluster_id': cluster, 'pca_1': cluster / 10, 'pca_2': cluster / 5}
                for cluster in range(5)
            ],
            'population_size': 20, 'selected_k': 5, 'selection_basis': 'maximum silhouette',
            'pca_explained_variance_ratio': [0.4, 0.2], 'displayed_sample_size': 5,
            'sample_limit': 5,
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
    if page == "segmentation":
        cluster_cards = [metric for metric in app.metric if metric.label.startswith("Кластер")]
        assert len(cluster_cards) == 5
        profile_frame = next(
            frame.value for frame in app.dataframe if "Доля покупателей" in frame.value.columns
        )
        assert profile_frame["Доля покупателей"].dtype.kind == "f"
    if page == "hypotheses":
        paired_frame = next(
            frame.value for frame in app.dataframe if "Разность долей, п.п." in frame.value.columns
        )
        assert paired_frame["Разность долей, п.п."].dtype.kind == "f"


def test_category_filters_reach_the_data_loader(tmp_path: Path) -> None:
    script, _ = _page_fixture("categories")
    script = script.replace(
        "from instacart.dashboard.data import Readiness\n",
        "from instacart.dashboard.data import Readiness\nimport streamlit as st\n",
    )
    script = script.replace(
        "categories.load_category_data = lambda *args, **kwargs: fixture",
        "categories.load_category_data = lambda *args, **kwargs: "
        "(st.session_state.setdefault('loader_args', []).append(args) or fixture)",
    )
    app_path = tmp_path / "categories_filters.py"
    app_path.write_text(script, encoding="utf-8")
    app = AppTest.from_file(str(app_path), default_timeout=20).run()
    assert len(app.exception) == 0

    app.multiselect(key="category_departments").set_value([1])
    app.multiselect(key="category_aisles").set_value([3])
    app.text_input(key="category_product_search").set_value("Apple")
    app.run()

    assert len(app.exception) == 0
    last_call = app.session_state["loader_args"][-1]
    assert last_call[6] == (1,)
    assert last_call[7] == (3,)
    assert last_call[14] == "Apple"


def test_empty_category_activity_does_not_hide_search_results_and_rates_stay_numeric(
    tmp_path: Path,
) -> None:
    script, _ = _page_fixture("categories")
    script = script.replace(
        "'activity': [{'category_id': 1, 'category_name': 'Produce',\n"
        "                'item_count': 5, 'reorder_count': 2}],",
        "'activity': [],",
    )
    app_path = tmp_path / "categories_empty_activity.py"
    app_path.write_text(script, encoding="utf-8")
    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert len(app.exception) == 0
    product_frame = next(frame.value for frame in app.dataframe if "Товар" in frame.value.columns)
    assert product_frame.iloc[0]["Товар"] == "Apples"
    assert product_frame["Доля повторов"].dtype.kind == "f"
    assert any("Нет товарных позиций" in item.value for item in app.info)


def test_stale_publication_clears_rendered_page_content(tmp_path: Path) -> None:
    script = """\
import streamlit as st
import instacart.dashboard.app as dashboard_app
from instacart.dashboard.data import PublicationChanged, Readiness

readiness = Readiness(True, True, True, 'sha256:old', 'ingestion-1', 'marts-1', 'analysis-1')

def reject_changed_publication(_database_url, _readiness):
    raise PublicationChanged('Публикация изменилась')

dashboard_app.assert_publication_current = reject_changed_publication

def render_stale_page(_readiness, _database_url):
    st.markdown('Устаревшие показатели')

try:
    dashboard_app._render_consistent_page(render_stale_page, readiness, 'postgresql://fixture')
except PublicationChanged:
    st.error('Публикация изменилась')
"""
    app_path = tmp_path / "stale_publication.py"
    app_path.write_text(script, encoding="utf-8")
    app = AppTest.from_file(str(app_path), default_timeout=20).run()

    assert len(app.exception) == 0
    assert not any("Устаревшие показатели" in item.value for item in app.markdown)
    assert any("Публикация изменилась" in item.value for item in app.error)
