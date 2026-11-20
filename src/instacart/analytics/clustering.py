"""Customer segmentation from SQL-prepared prior-order features."""

from __future__ import annotations

import uuid
from typing import Any

import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

FEATURES = (
    "prior_order_count",
    "avg_basket_size",
    "reorder_rate",
    "avg_days_between_orders",
    "unique_product_count",
    "unique_aisle_count",
    "unique_department_count",
)
LOG_FEATURES = tuple(name for name in FEATURES if name != "reorder_rate")


def _json_number(value: Any) -> float | None:
    return float(value) if pd.notna(value) else None


def _raw_feature_summary(frame: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for name in FEATURES:
        series = frame[name]
        summary = series.describe(percentiles=[0.25, 0.5, 0.75, 0.9, 0.99])
        result[name] = {
            key: _json_number(summary.get(key))
            for key in ("mean", "std", "min", "25%", "50%", "75%", "90%", "99%", "max")
        }
        result[name]["skew"] = _json_number(series.skew())
        result[name]["missing_count"] = float(series.isna().sum())
    return result


def cluster_customers(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    *,
    random_seed: int = 2026,
    silhouette_sample_size: int = 5_000,
) -> dict[str, Any]:
    """Fit and persist K-Means results; input contains one row per customer."""
    rows = connection.execute(
        "SELECT user_id, prior_order_count, avg_basket_size, reorder_rate, "
        "avg_days_between_orders, unique_product_count, unique_aisle_count, "
        "unique_department_count FROM mart.customer_features ORDER BY user_id"
    ).fetchall()
    if len(rows) < 3:
        raise ValueError(
            "Для кластеризации нужны данные как минимум трёх покупателей с заказами prior"
        )
    frame = pd.DataFrame.from_records(rows, columns=("user_id", *FEATURES))
    raw_summary = _raw_feature_summary(frame)
    raw = frame.loc[:, list(FEATURES)].astype("float64").copy()
    imputer = SimpleImputer(strategy="median", keep_empty_features=True)
    imputed = imputer.fit_transform(raw)
    transformed = pd.DataFrame(imputed, columns=FEATURES)
    for feature in LOG_FEATURES:
        transformed[feature] = np.log1p(np.clip(transformed[feature].to_numpy(), 0, None))
    scaler = StandardScaler()
    scaled = scaler.fit_transform(transformed)

    count = len(frame)
    score_size = min(count, max(2, silhouette_sample_size))
    sample_indices = np.random.default_rng(random_seed).choice(
        count, size=score_size, replace=False
    )
    candidates = range(2, min(8, count - 1) + 1)
    models: dict[int, KMeans] = {}
    metrics: list[dict[str, Any]] = []
    with threadpool_limits(limits=2):
        for k in candidates:
            model = KMeans(n_clusters=k, n_init=10, random_state=random_seed, algorithm="lloyd")
            labels = model.fit_predict(scaled)
            sampled_labels = labels[sample_indices]
            try:
                silhouette = float(silhouette_score(scaled[sample_indices], sampled_labels))
            except ValueError:
                silhouette = -1.0
            sizes = np.bincount(labels, minlength=k)
            models[k] = model
            metrics.append(
                {
                    "k": k,
                    "inertia": float(model.inertia_),
                    "silhouette": silhouette,
                    "silhouette_sample_size": int(score_size),
                    "cluster_sizes": {str(index): int(size) for index, size in enumerate(sizes)},
                    "minimum_cluster_size": int(sizes.min()),
                }
            )
    min_size = max(20, int(np.ceil(count * 0.005)))
    eligible = [item for item in metrics if item["minimum_cluster_size"] >= min_size]
    selection_basis = "maximum sampled silhouette with every cluster >= 0.5% of customers or 20"
    if not eligible:
        eligible = metrics
        selection_basis = "maximum sampled silhouette; no candidate met the minimum cluster size"
    selected = max(eligible, key=lambda item: (item["silhouette"], -item["k"]))
    selected_k = int(selected["k"])
    selected["selected"] = True
    model = models[selected_k]
    labels = model.labels_.astype(np.int16, copy=False)

    pca = PCA(n_components=2, random_state=random_seed)
    coordinates = pca.fit_transform(scaled)
    segments = pd.DataFrame(
        {
            "user_id": frame["user_id"].astype("int64"),
            "cluster_id": labels,
            "pca_1": coordinates[:, 0],
            "pca_2": coordinates[:, 1],
        }
    )
    with connection.cursor().copy(
        "COPY mart._customer_segments "
        "(run_id, dataset_fingerprint, user_id, cluster_id, pca_1, pca_2) FROM STDIN"
    ) as copy:
        for user_id, cluster_id, pca_1, pca_2 in segments.itertuples(index=False, name=None):
            copy.write_row(
                (run_id, fingerprint, int(user_id), int(cluster_id), float(pca_1), float(pca_2))
            )

    for metric in metrics:
        connection.execute(
            "INSERT INTO mart._cluster_metrics "
            "(run_id, dataset_fingerprint, k, inertia, silhouette, silhouette_sample_size, "
            "cluster_sizes, selected) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                fingerprint,
                metric["k"],
                metric["inertia"],
                metric["silhouette"],
                metric["silhouette_sample_size"],
                Jsonb(metric["cluster_sizes"]),
                metric.get("selected", False),
            ),
        )

    scaled_centers = model.cluster_centers_
    log_centers = scaler.inverse_transform(scaled_centers)
    original_centers = pd.DataFrame(log_centers, columns=FEATURES)
    for feature in LOG_FEATURES:
        original_centers[feature] = np.expm1(original_centers[feature]).clip(lower=0)
    for cluster_id in range(selected_k):
        members = frame.loc[labels == cluster_id, list(FEATURES)]
        means = {name: _json_number(members[name].mean()) for name in FEATURES}
        transformed_center = {
            name: float(scaled_centers[cluster_id, position])
            for position, name in enumerate(FEATURES)
        }
        original_center = {
            name: _json_number(original_centers.iloc[cluster_id][name]) for name in FEATURES
        }
        connection.execute(
            "INSERT INTO mart._cluster_profiles "
            "(run_id, dataset_fingerprint, cluster_id, customer_count, feature_means, "
            "transformed_center) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                run_id,
                fingerprint,
                cluster_id,
                int(len(members)),
                Jsonb({"mean": means, "geometric_center": original_center}),
                Jsonb(transformed_center),
            ),
        )

    return {
        "customer_count": count,
        "selected_k": selected_k,
        "selection_basis": selection_basis,
        "metrics": metrics,
        "profiles": [
            {
                "cluster_id": cluster_id,
                "customer_count": int(np.sum(labels == cluster_id)),
                "feature_means": {
                    name: _json_number(frame.loc[labels == cluster_id, name].mean())
                    for name in FEATURES
                },
                "geometric_center": {
                    name: _json_number(original_centers.iloc[cluster_id][name]) for name in FEATURES
                },
            }
            for cluster_id in range(selected_k)
        ],
        "feature_distributions": raw_summary,
        "transform": {
            "imputation": "median",
            "imputation_values": {
                name: float(value)
                for name, value in zip(FEATURES, imputer.statistics_, strict=True)
            },
            "log1p_features": list(LOG_FEATURES),
            "standard_scaler_mean": {
                name: float(value) for name, value in zip(FEATURES, scaler.mean_, strict=True)
            },
            "standard_scaler_scale": {
                name: float(value) for name, value in zip(FEATURES, scaler.scale_, strict=True)
            },
            "pca_explained_variance_ratio": [
                float(value) for value in pca.explained_variance_ratio_
            ],
            "features": list(FEATURES),
        },
    }
