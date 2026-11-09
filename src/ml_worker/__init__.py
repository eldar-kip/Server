from .trainer import TAiresTrainerService, TrainingConfig, TrainingResult
from .vector_search import (
    build_knn_query,
    build_tire_search_text,
    embed_passages,
    embed_queries,
    get_configured_model_uri,
    get_embedding_model,
    match_two_tables_in_memory,
    resolve_model_source,
    search_by_vector,
    search_top_candidates,
    search_top_candidates_batch,
    set_active_model_uri,
)

__all__ = [
    "TAiresTrainerService",
    "TrainingConfig",
    "TrainingResult",
    "build_knn_query",
    "build_tire_search_text",
    "embed_passages",
    "embed_queries",
    "get_configured_model_uri",
    "get_embedding_model",
    "match_two_tables_in_memory",
    "resolve_model_source",
    "search_by_vector",
    "search_top_candidates",
    "search_top_candidates_batch",
    "set_active_model_uri",
]
