import asyncio
import csv
import io
import json
from typing import Any, Literal
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse
import pandas as pd
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.ml_worker.vector_search import get_configured_model_uri, set_active_model_uri
from src.server.api.deps import authenticate_ws_token, get_current_user, require_role
from src.server.core.config import settings
from src.server.core.database import AsyncSessionLocal, get_db
from src.server.models.ml_training import MLTrainingJob, TrainingStatusEnum
from src.server.models.user import RoleEnum, User
from src.server.schemas.ml_ops import (
    DatasetPairSchema,
    ModelActivateResponse,
    TrainingJobCreate,
    TrainingJobResponse,
)
from src.server.services.s3_storage import S3ModelStorageService
from src.server.tasks.training_tasks import collect_training_pairs_from_db, run_training_job

router = APIRouter(prefix="/api/v1/ml", tags=["tAIres-Trainer и хранилище моделей S3"])
ws_ml_router = APIRouter(tags=["WebSocket Realtime Notifications"])


@router.get("/dataset")
async def export_training_dataset(
    format: Literal["json", "csv"] = Query("json", description="Формат выгрузки датасета для tAIres-Trainer"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Выгружает обучающий датасет пар (сырое имя поставщика -> эталон Tire) из FeedbackAuditLog и каталога."""
    pairs = await collect_training_pairs_from_db(db)
    if format == "json":
        return [DatasetPairSchema(query=q, positive_passage=p) for q, p in pairs]

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["query", "positive_passage"])
    for q, p in pairs:
        writer.writerow([q, p])
    headers = {"Content-Disposition": 'attachment; filename="taires_training_dataset.csv"'}
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.post("/train", response_model=TrainingJobResponse, status_code=status.HTTP_201_CREATED)
async def start_training_job(
    payload: TrainingJobCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("Admin")),
) -> MLTrainingJob:
    """
    Запускает задачу обучения модели в tAIres-Trainer со всеми параметрами со схемы:
    размер батча, кол-во эпох, learning rate, датасет, предобученная модель, процент валидации,
    версия модели и название модели.
    """
    explicit_pairs = (
        [[item.query, item.positive_passage] for item in payload.dataset]
        if payload.dataset
        else None
    )

    job = MLTrainingJob(
        user_id=current_user.id,
        model_name=payload.model_name.strip(),
        model_version=payload.model_version.strip(),
        base_model=payload.base_model.strip(),
        batch_size=payload.batch_size,
        epochs=payload.epochs,
        learning_rate=payload.learning_rate,
        val_split=payload.val_split,
        dataset_size=len(explicit_pairs) if explicit_pairs else 0,
        status=TrainingStatusEnum.PENDING,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    run_training_job.delay(
        job.id,
        explicit_pairs=explicit_pairs,
        use_feedback_log=payload.use_feedback_log,
    )
    return job


@router.post("/train/upload", response_model=TrainingJobResponse, status_code=status.HTTP_201_CREATED)
async def start_training_job_with_file(
    dataset_file: UploadFile = File(..., description="Файл датасета (.csv или .xlsx) с колонками query и positive_passage"),
    model_name: str = Form("taires-bi-encoder"),
    model_version: str = Form("1.0.0"),
    base_model: str = Form("intfloat/multilingual-e5-large"),
    batch_size: int = Form(16),
    epochs: int = Form(3),
    learning_rate: float = Form(2e-5),
    val_split: float = Form(0.2),
    use_feedback_log: bool = Form(True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("Admin")),
) -> MLTrainingJob:
    """Запуск обучения в tAIres-Trainer с загрузкой файла датасета из веб-интерфейса."""
    fname = (dataset_file.filename or "").lower()
    raw_bytes = await dataset_file.read()
    if fname.endswith(".csv"):
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw_bytes.decode("cp1251", errors="replace")
        df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
    elif fname.endswith(".xlsx"):
        df = pd.read_excel(io.BytesIO(raw_bytes), engine="openpyxl")
    else:
        raise HTTPException(status_code=400, detail="Датасет должен быть в формате .csv или .xlsx")

    df = df.dropna(how="all")
    if len(df.columns) < 2:
        raise HTTPException(status_code=400, detail="В файле датасета должно быть минимум 2 колонки (запрос и эталон)")

    q_col, p_col = df.columns[0], df.columns[1]
    explicit_pairs = [
        [str(row[q_col]).strip(), str(row[p_col]).strip()]
        for _, row in df.iterrows()
        if pd.notna(row[q_col]) and pd.notna(row[p_col])
    ]
    if not explicit_pairs:
        raise HTTPException(status_code=400, detail="Файл датасета не содержит валидных пар")

    job = MLTrainingJob(
        user_id=current_user.id,
        model_name=model_name.strip(),
        model_version=model_version.strip(),
        base_model=base_model.strip(),
        batch_size=max(1, min(256, batch_size)),
        epochs=max(1, min(50, epochs)),
        learning_rate=learning_rate,
        val_split=max(0.05, min(0.5, val_split)),
        dataset_size=len(explicit_pairs),
        status=TrainingStatusEnum.PENDING,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    run_training_job.delay(job.id, explicit_pairs=explicit_pairs, use_feedback_log=use_feedback_log)
    return job


@router.get("/jobs", response_model=list[TrainingJobResponse])
async def list_training_jobs(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MLTrainingJob]:
    """Список всех запусков обучения tAIres-Trainer с телеметрией и результатами автотеста."""
    stmt = (
        select(MLTrainingJob)
        .order_by(MLTrainingJob.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.get("/jobs/{job_id}", response_model=TrainingJobResponse)
async def get_training_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MLTrainingJob:
    """Детальная карточка запуска tAIres-Trainer (телеметрия, точность автотеста, S3 URI, метадата)."""
    res = await db.execute(select(MLTrainingJob).where(MLTrainingJob.id == job_id))
    job = res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Задача обучения не найдена")
    return job


@router.get("/models")
async def list_registered_and_s3_models(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Возвращает текущую активную модель на Сервере ИИ, реестр обученных версий в БД и список объектов в S3."""
    completed_stmt = (
        select(MLTrainingJob)
        .where(MLTrainingJob.status == TrainingStatusEnum.COMPLETED)
        .order_by(MLTrainingJob.completed_at.desc())
    )
    db_models = list((await db.execute(completed_stmt)).scalars().all())
    s3_models = await asyncio.to_thread(S3ModelStorageService.list_models_in_s3)
    active_uri = await asyncio.to_thread(get_configured_model_uri)

    return {
        "active_model_uri": active_uri,
        "s3_configured": S3ModelStorageService.is_configured(),
        "s3_bucket": settings.S3_BUCKET_MODELS,
        "registry": [TrainingJobResponse.model_validate(m).model_dump() for m in db_models],
        "s3_objects": s3_models,
    }


@router.post("/models/{job_id}/activate", response_model=ModelActivateResponse)
async def activate_trained_model(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("Admin")),
) -> ModelActivateResponse:
    """
    Переключает 'Сервер, запускающий ИИ' на выбранную обученную модель из Хранилища S3.
    При следующем батче воркер автоматически скачает веса из S3 и применит их.
    """
    res = await db.execute(select(MLTrainingJob).where(MLTrainingJob.id == job_id))
    job = res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Модель (задача обучения) не найдена")
    if job.status != TrainingStatusEnum.COMPLETED or not job.s3_uri:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Можно активировать только успешно обученную (COMPLETED) модель с сохраненным артефактом",
        )

    await db.execute(update(MLTrainingJob).values(is_active=False))
    job.is_active = True
    await db.commit()

    await asyncio.to_thread(set_active_model_uri, job.s3_uri)

    return ModelActivateResponse(
        job_id=job.id,
        model_name=job.model_name,
        model_version=job.model_version,
        active_model_uri=job.s3_uri,
        message="Активная модель ИИ успешно переключена; воркеры подгрузят веса из S3 автоматически",
    )


@ws_ml_router.websocket("/api/v1/ws/ml/jobs/{job_id}")
async def websocket_training_telemetry(
    websocket: WebSocket,
    job_id: int,
    token: str | None = Query(default=None),
) -> None:
    """WebSocket трансляции живой телеметрии эпох обучения из tAIres-Trainer в веб-интерфейс."""
    await websocket.accept()

    async with AsyncSessionLocal() as auth_db:
        user = await authenticate_ws_token(token, auth_db)
        if user is None or user.role != RoleEnum.ADMIN:
            await websocket.send_text(
                json.dumps({"error": "Требуется JWT-токен администратора в ?token="}, ensure_ascii=False)
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    redis_conn = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis_conn.pubsub()
    channel_name = f"training:{job_id}:telemetry"

    try:
        await pubsub.subscribe(channel_name)

        async with AsyncSessionLocal() as db:
            job = (
                await db.execute(select(MLTrainingJob).where(MLTrainingJob.id == job_id))
            ).scalar_one_or_none()
            if job is None:
                await websocket.send_text(
                    json.dumps({"error": f"Задача обучения {job_id} не найдена"}, ensure_ascii=False)
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            if job.status in (TrainingStatusEnum.COMPLETED, TrainingStatusEnum.FAILED):
                await websocket.send_text(
                    json.dumps(
                        {
                            "job_id": job.id,
                            "status": job.status.value,
                            "s3_uri": job.s3_uri,
                            "autotest_accuracy": job.autotest_accuracy,
                            "telemetry": job.telemetry,
                            "error": job.error_message,
                        },
                        ensure_ascii=False,
                    )
                )
                return

        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") == "message":
                raw_data = msg["data"]
                await websocket.send_text(raw_data)
                try:
                    parsed = json.loads(raw_data)
                except (TypeError, json.JSONDecodeError):
                    parsed = {}
                if parsed.get("status") in ("COMPLETED", "FAILED"):
                    break
            await asyncio.sleep(0.01)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(channel_name)
            await pubsub.close()
        except Exception:
            pass
        await redis_conn.aclose()
        try:
            await websocket.close()
        except RuntimeError:
            pass
