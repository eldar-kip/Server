# tAIres Server — Backend автоматизации закупок автомобильных шин

Асинхронный сервер управления каталогом шин, автоматического ML-сопоставления прайс-листов поставщиков, оптимизации распределения заказов (**SplitOptimizer**) и выгрузки спецификаций в **1С / ERP**.

## Стек технологий

- **Python 3.14**, пакетный менеджер **uv**
- **FastAPI** (асинхронный REST API + WebSocket уведомления)
- **SQLAlchemy 2.0 (async)** + **asyncpg** + **Alembic**
- **PostgreSQL 15** + расширение **pgvector** (индекс `HNSW` по косинусному расстоянию `<=>`)
- **Celery** + **Redis** (очереди задач `ml` и `celery_tasks`, брокер и `Pub/Sub` прогресса)
- **Sentence-Transformers** + **PyTorch** (`intfloat/multilingual-e5-large`, размерность `1024`, контрастивное дообучение в **tAIres-Trainer**)
- **MinIO / S3 (`boto3`)** — объектное хранилище обученных нейросетей, `metadata.json` и `telemetry.json`
- **SciPy** (`scipy.optimize.milp` для смешанно-целочисленной оптимизации сплита заказов)

---

## Двухконтурная архитектура и бизнес-процесс

### Контур 1: Обучение нейросетей (`tAIres-Trainer` -> `Хранилище S3`)
1. **Входные параметры обучения (`POST /api/v1/ml/train` и `/train/upload`)**: `batch_size`, `epochs`, `learning_rate`, `dataset` (явный список/файл или автоматический сбор пар из `FeedbackAuditLog` + `Tire`), `base_model` (с HuggingFace, локальная или `s3://...`), `val_split`, `model_version`, `model_name`.
2. **Обучение и автотест (`TAiresTrainerService`)**: дообучает Bi-Encoder контрастивной функцией потерь InfoNCE (`AdamW`), собирает живую телеметрию по каждой эпохе (`WS /api/v1/ws/ml/jobs/{job_id}`) и вычисляет точность на отложенной выборке (`Accuracy@1`, `Accuracy@5`, `MRR`).
3. **Публикация в S3 и горячее переключение (`S3ModelStorageService`)**: сохраняет веса ИИ-модели, `metadata.json` и `telemetry.json` в бакет `s3://taires-models/...`. По команде `POST /api/v1/ml/models/{job_id}/activate` сервер инференса переключается на новую модель из S3 без перезапуска контейнеров.

### Контур 2: Промышленное сопоставление и закупки (`Сайт / Модуль CRM` ↔ `Сервер` ↔ `Сервер ИИ`)
1. **Авторизация (`JWT` + роли `Admin` / `Buyer` + межсервисный `X-AI-Service-Token`)** — защита пользовательских и межсервисных эндпоинтов.
2. **Контракт «Две таблицы + токен» (`POST /api/v1/ai/match-tables` и `/match-files`)** — принимает таблицу номенклатуры предприятия, прайс-лист поставщика и токен `X-AI-Service-Token`, возвращает сопоставленную номенклатуру с точностью совпадения. При заданном `EXTERNAL_AI_SERVER_URL` основной сервер вызывает внешний ИИ-сервер по HTTP; по умолчанию использует локальный `pgvector` HNSW.
3. **Загрузка прайс-листов (`.xlsx` / `.csv`) и возврат таблицы для CRM (`GET /{session_id}/matched-table`)** — возвращает полную таблицу `Предприятие | Поставщик | Цена поставщика | Точность совпадения` в форматах `json`, `csv` или `xlsx`.
4. **Проверка и утверждение закупщиком (`TAI-106`)**:
   - Фильтрация сомнительных позиций `GET /matches?max_confidence=0.7`.
   - Ручная перепривязка `PATCH /items/{item_id}` и финальное утверждение `POST /{session_id}/approve` (`COMPLETED -> APPROVED`) с записью в `FeedbackAuditLog`.
5. **Оптимизация сплита закупки (`TAI-105`, `SplitOptimizer`)**:
   - Решает задачу MILP (`scipy.optimize.milp`, с резервной жадной эвристикой) с учетом цен, складских остатков, квантов поставки (`lot_size`), минимальных партий (`min_order_qty`) и минимальной суммы заказа на поставщика.
6. **Экспорт в 1С / ERP (`TAI-107`)**:
   - Генерация файлов **CommerceML 2.08** (`XML` / `JSON`) и отправка в ERP заказчика по Webhook (`httpx`) с подписью `HMAC-SHA256` (`X-Taires-Signature-256`).

---

## Быстрый старт (Docker Compose)

1. При необходимости скопируйте переменные окружения (по умолчанию `docker-compose.yml` уже содержит рабочие значения для локального запуска):
   ```bash
   cp .env.example .env
   ```
2. Поднимите все сервисы (`db`, `redis`, одноразовый мигратор `migrate`, `server`, `celery_worker`, `ml_worker`):
   ```bash
   docker compose up --build -d
   ```
3. Заполните справочники, поставщиков, тестовые шины и пользователей (`admin@taires.com` / `admin123`, `buyer@taires.com` / `buyer123`):
   ```bash
   docker compose exec server uv run --no-dev python scripts/seed_db.py
   ```
4. Сгенерируйте эмбеддинги эталонного каталога шин `Tire` (идемпотентно; флаг `--rebuild` пересчитывает все записи):
   ```bash
   docker compose exec ml_worker uv run --no-dev python scripts/build_tire_embeddings.py
   ```

Swagger UI доступен по адресу: http://localhost:8000/docs (кнопка **Authorize** работает через `/auth/token`).
Веб-консоль S3-хранилища моделей **MinIO** доступна по адресу: http://localhost:9001 (`minioadmin` / `minioadmin123`).

---

## Локальная разработка и миграции

Установка зависимостей (включая `dev`-группу с `pytest`):
```bash
uv sync
```

Применение и проверка отката миграций Alembic:
```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Генерация дополнительных 50 позиций шин и расчет их векторов:
```bash
uv run python -m src.scripts.seed_db
uv run python -m src.scripts.starting_data
uv run python scripts/build_tire_embeddings.py
```

Запуск автотестов:
```bash
uv run pytest -v
```

Ручной e2e-скрипт проверки загрузки файла и WebSocket-прогресса на запущенном сервере:
```bash
uv run python scripts/e2e_realtime.py
```

---

## Основные эндпоинты API

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/auth/register` | Регистрация пользователя |
| `POST` | `/auth/login` | Получение JWT по JSON `{email, password}` |
| `POST` | `/auth/token` | Получение JWT по форме OAuth2 (для Swagger UI) |
| `POST` | `/api/v1/procurement/suppliers` | Создание контрагента-поставщика |
| `GET` | `/api/v1/procurement/suppliers` | Список поставщиков |
| `POST` | `/api/v1/procurement/upload` | Создание сессии из `.xlsx`/`.csv` и старт ML-таски |
| `POST` | `/api/v1/procurement/{session_id}/upload` | Дозагрузка прайса другого поставщика в сессию |
| `GET` | `/api/v1/procurement/sessions` | Список сессий текущего закупщика (`Admin` видит все) |
| `GET` | `/api/v1/procurement/{session_id}` | Детали сессии и список позиций |
| `WS` | `/api/v1/ws/procurement/{session_id}?token=...` | Realtime-прогресс обработки прайс-листа |
| `GET` | `/api/v1/procurement/{session_id}/matched-table` | Полная таблица сопоставленной номенклатуры для CRM (`?format=json\|csv\|xlsx`) |
| `GET` | `/api/v1/procurement/{session_id}/matches` | Позиции с низкой уверенностью (`< max_confidence`) |
| `PATCH` | `/api/v1/procurement/items/{item_id}` | Ручная привязка эталонной шины (`FeedbackAuditLog`) |
| `POST` | `/api/v1/procurement/{session_id}/approve` | Утверждение сопоставлений сессии (`APPROVED`) |
| `POST` | `/api/v1/procurement/{session_id}/demand` | Загрузка потребности по каталожным шинам |
| `POST` | `/api/v1/procurement/{session_id}/optimize` | Запуск `SplitOptimizer` (MILP / greedy) |
| `GET` | `/api/v1/procurement/{session_id}/export/1c` | Скачивание CommerceML 2.08 (`?format=xml\|json`) |
| `POST` | `/api/v1/procurement/{session_id}/export/webhook` | Отправка спецификации в ERP по Webhook (HMAC) |
| `POST` | `/api/v1/ai/match-tables` | Межсервисное сопоставление «Две таблицы + токен» (`X-AI-Service-Token`) |
| `POST` | `/api/v1/ai/match-files` | Файловое сопоставление двух таблиц `.xlsx`/`.csv` + токен |
| `GET` | `/api/v1/ml/dataset` | Выгрузка обучающего датасета (`?format=json\|csv`) |
| `POST` | `/api/v1/ml/train` | Запуск обучения в `tAIres-Trainer` (`Admin`) |
| `POST` | `/api/v1/ml/train/upload` | Запуск обучения с загрузкой файла датасета `.csv`/`.xlsx` |
| `GET` | `/api/v1/ml/jobs` | Список запусков обучения, телеметрии и автотестов |
| `GET` | `/api/v1/ml/jobs/{job_id}` | Детали задачи обучения (`telemetry`, `autotest_accuracy`, `s3_uri`, `model_metadata`) |
| `WS` | `/api/v1/ws/ml/jobs/{job_id}?token=...` | Живая WebSocket-телеметрия эпох обучения |
| `GET` | `/api/v1/ml/models` | Реестр обученных моделей в БД и S3 + текущая активная модель |
| `POST` | `/api/v1/ml/models/{job_id}/activate` | Горячее переключение Сервера ИИ на выбранную модель из S3 |
