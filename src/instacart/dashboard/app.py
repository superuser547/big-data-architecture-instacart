"""Streamlit entry point; reads the published PostgreSQL analytics only."""

from __future__ import annotations

import psycopg
import streamlit as st

from instacart.config import Settings
from instacart.dashboard.data import Readiness, get_readiness
from instacart.dashboard.pages import categories, hypotheses, overview, segmentation


def _page_context() -> tuple[Readiness, str]:
    readiness = st.session_state.get("analytics_readiness")
    database_url = st.session_state.get("dashboard_database_url")
    if not readiness or not database_url:
        st.error("Состояние подключения PostgreSQL не инициализировано. Обновите страницу.")
        st.stop()
    return readiness, database_url


def _overview_page() -> None:
    readiness, database_url = _page_context()
    overview.render(readiness, database_url)


def _categories_page() -> None:
    readiness, database_url = _page_context()
    categories.render(readiness, database_url)


def _hypotheses_page() -> None:
    readiness, database_url = _page_context()
    hypotheses.render(readiness, database_url)


def _segmentation_page() -> None:
    readiness, database_url = _page_context()
    segmentation.render(readiness, database_url)


def main() -> None:
    st.set_page_config(
        page_title="Instacart Analytics",
        page_icon="🛒",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("Instacart Analytics")
    st.caption("Интерактивный обзор опубликованных аналитических витрин PostgreSQL")
    settings = Settings.from_env()
    st.session_state["dashboard_database_url"] = settings.database_url
    try:
        readiness = get_readiness(settings.database_url)
    except (psycopg.OperationalError, OSError) as exc:
        st.error(
            "Не удалось подключиться к PostgreSQL. Проверьте DATABASE_URL и доступность сервера."
        )
        st.caption(f"Техническая причина: {type(exc).__name__}")
        return
    except psycopg.Error as exc:
        st.error("Не удалось прочитать ops.analytics_readiness в PostgreSQL.")
        st.caption(f"Техническая причина: {type(exc).__name__}")
        return
    st.session_state["analytics_readiness"] = readiness

    with st.sidebar:
        st.header("Состояние данных")
        if readiness.dashboard_ready:
            st.success("Опубликованная аналитика актуальна")
            st.caption(f"Fingerprint: `{readiness.dataset_fingerprint}`")
            st.caption(f"Импорт: `{readiness.ingestion_run_id}`")
            st.caption(f"Витрины: `{readiness.marts_run_id}`")
            st.caption(f"Анализ: `{readiness.analysis_run_id}`")
        else:
            st.warning("Актуальные витрины и результаты анализа не опубликованы полностью.")
            st.caption(f"Витрины готовы: {'да' if readiness.marts_ready else 'нет'}")
            st.caption(f"Анализ готов: {'да' if readiness.analysis_ready else 'нет'}")

    if not readiness.dashboard_ready:
        st.info(
            "Разделы появятся после подготовки импортированных данных, витрин и анализа. "
            "Приложение не запускает ingest, построение витрин, кластеризацию "
            "или статистические расчёты."
        )
        st.code(
            "make setup → make ingest → make marts → make analyze → "
            "make validate-analytics → make dashboard"
        )
        return

    pages = {
        "Аналитика": [
            st.Page(_overview_page, title="Обзор данных", icon="📊", default=True),
            st.Page(_categories_page, title="Категории и товары", icon="🧺"),
            st.Page(_hypotheses_page, title="Проверка гипотез", icon="🧪"),
            st.Page(_segmentation_page, title="Сегментация покупателей", icon="👥"),
        ]
    }
    navigation = st.navigation(pages, position="sidebar")
    try:
        navigation.run()
    except psycopg.OperationalError:
        st.error(
            "Соединение с PostgreSQL потеряно во время чтения. "
            "Обновите страницу после восстановления сервера."
        )
    except psycopg.Error as exc:
        st.error("Не удалось прочитать опубликованную аналитику PostgreSQL.")
        st.caption(f"Техническая причина: {type(exc).__name__}")


main()
