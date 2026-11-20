# Воспроизводимый запуск

## Настройка

Скопируйте `.env.example` в `.env`, укажите путь к набору данных и при
необходимости измените `DATABASE_URL`, лимит `DUCKDB_MEMORY_LIMIT`, число
потоков `DUCKDB_THREADS` и временный каталог `DUCKDB_TEMP_DIRECTORY`.
`make setup` устанавливает зависимости через uv, запускает PostgreSQL 16 с
постоянным именованным volume и применяет Alembic-миграции. Секреты `.env` не
коммитятся; Compose публикует порт только на localhost.

## Команды

| Команда | Действие |
|---|---|
| `make setup` | Установка зависимостей, запуск PostgreSQL и миграции |
| `make profile` | Профиль и проверки CSV в DuckDB |
| `make ingest` | Полный идемпотентный импорт |
| `make ingest FORCE=1` | Явная транзакционная пересборка |
| `make validate` | Проверки загруженной PostgreSQL-модели |
| `make lint` | Ruff lint и форматирование |
| `make test` | Unit- и доступные интеграционные тесты |
| `make check` | Ruff и pytest |
| `make db-down` | Остановить контейнер PostgreSQL без удаления volume |

Для отдельного пути можно выполнить `uv run instacart profile --dataset путь`
или `uv run instacart ingest --dataset путь --force`. В качестве пути принимается
каталог CSV или ZIP. Профиль и итоговые проверки записываются в JSON; пути
настраиваются переменными `PROFILE_REPORT` и `VALIDATION_REPORT`.

Полный набор эталонных строк проверяется отдельно от общих инвариантов, поэтому
те же команды работают с небольшими тестовыми CSV. Для integration-тестов задайте
`INTEGRATION_DATABASE_URL` на пустую отдельную базу PostgreSQL. Тесты применяют
миграцию и проверяют импорт, повторный запуск, откат сбойной пересборки и
сохранность прежнего набора.
