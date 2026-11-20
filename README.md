# Instacart Analytics

Фундамент аналитической системы и воспроизводимый конвейер переноса исходного
датасета Instacart в PostgreSQL 16. Проект хранит нормализованные сущности
заказов и товарных позиций, проверяет контракты данных до загрузки и сохраняет
результаты каждой попытки импорта.

## Источник данных

Исходный датасет опубликован для соревнования Kaggle
[Instacart Market Basket Analysis](https://www.kaggle.com/competitions/instacart-market-basket-analysis/data).
Получите архив на странице соревнования и вручную поместите шесть CSV в
`data/raw/` либо укажите путь к ZIP-файлу. Нужны `orders.csv`, `products.csv`,
`aisles.csv`, `departments.csv`, `order_products__prior.csv` и
`order_products__train.csv`. Датасет не распространяется вместе с кодом.

Локальные исходники исключены из Git. SHA-256 каждого CSV и детерминированный
fingerprint набора записываются в JSON-отчёт и журнал загрузок.

## Архитектура

- `src/instacart/config.py` читает подключение, пути отчётов и лимиты DuckDB из окружения.
- `dataset.py` обнаруживает каталог или ZIP, проверяет CRC, состав файлов,
  заголовки и контрольные суммы.
- `profiling.py` типизирует CSV и проверяет ограничения и связи через DuckDB;
  лимиты памяти, потоков и временного диска настраиваются.
- `ingestion.py` передаёт большие CSV в PostgreSQL через потоковый `COPY`,
  нормализует справочники и регистрирует запуск.
- `validation.py` проверяет количества, последовательности и целостность после загрузки.
- Alembic управляет схемами `core`, `ops` и пустой схемой `mart`.

Сводная модель и детали ETL описаны в [архитектуре](docs/architecture.md),
[словаре данных](docs/data-dictionary.md) и [запуске](docs/operations.md).

## Быстрый запуск

Требуются Python 3.11, `uv` и Docker Compose. На macOS ARM64 и Linux используется
образ PostgreSQL 16 с поддержкой обеих архитектур.

```sh
cp .env.example .env
make setup
make profile
make ingest
make validate
make check
```

Для входного ZIP или другого каталога задайте `DATASET_PATH` в `.env` либо
передайте `--dataset` команде `uv run instacart`. Пересборка, которая заменяет
текущие данные только после успешной загрузки и проверок:

```sh
uv run instacart ingest --dataset data/raw --force
```

Отчёты JSON по умолчанию создаются в `reports/`. Интеграционные тесты используют
отдельную пустую PostgreSQL через `INTEGRATION_DATABASE_URL`; CI поднимает её
сам и работает на синтетических CSV.
