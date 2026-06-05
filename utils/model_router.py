"""
MODEL ROUTER — Deterministic Model Selection
--------------------------------------------
Maps task attributes (language, complexity_estimate, required_capabilities, tier)
to concrete model configuration.

This is a PURE deterministic function — NO LLM calls, NO heuristics.
Goodhart-proof: only reads from TaskInput, never sees rubric/verdicts.
"""

import os
from typing import Any

from contracts import TaskInput

# ─── Model Registry ─────────────────────────────────────────────────────────
# Each entry defines a model config with its strengths and constraints.
# The router selects the BEST match for the task's capabilities.

_MODEL_REGISTRY: list[dict[str, Any]] = [
    # ── Local Ollama Models ────────────────────────────────────────────────
    {
        "id": "local_small",
        "model": "qwen2.5-coder:3b-instruct",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "max_tokens": 2048,
        "cost_per_1k": 0.0,
        "provider": "ollama",
        "strengths": ["python", "basic", "fast", "low_complexity"],
        "languages": ["python", "bash", "yaml"],
    },
    {
        "id": "local_medium",
        "model": os.getenv("MODEL_LOCAL_MEDIUM", "codellama:7b-instruct"),
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "max_tokens": 4096,
        "cost_per_1k": 0.0,
        "provider": "ollama",
        "strengths": [
            "python", "javascript", "typescript", "php",
            "medium_complexity", "general",
        ],
        "languages": ["python", "javascript", "typescript", "php", "go", "rust"],
    },
    # ── Cloud Ollama Models ───────────────────────────────────────────────
    {
        "id": "cloud_general",
        "model": os.getenv("MODEL_CLOUD_GENERAL", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": 4096,
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "python", "javascript", "typescript", "php", "go", "rust",
            "medium_complexity", "high_complexity", "general",
        ],
        "languages": ["python", "javascript", "typescript", "php", "go", "rust"],
    },
    {
        "id": "cloud_large",
        "model": os.getenv("MODEL_CLOUD_LARGE", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": 8192,
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "python", "javascript", "typescript", "php", "go", "rust",
            "high_complexity", "architecture", "refactoring",
        ],
        "languages": ["python", "javascript", "typescript", "php", "go", "rust"],
    },
    {
        "id": "cloud_reasoning",
        "model": os.getenv("MODEL_CLOUD_REASONING", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": 8192,
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "python", "javascript", "typescript", "php", "go", "rust",
            "high_complexity", "reasoning", "architecture",
            "refactoring", "cross_language",
        ],
        "languages": ["python", "javascript", "typescript", "php", "go", "rust"],
    },
    {
        "id": "cloud_php",
        "model": os.getenv("MODEL_CLOUD_PHP", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": 8192,
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "php", "web", "laravel", "symfony",
            "high_complexity", "medium_complexity",
        ],
        "languages": ["php"],
    },
    {
        "id": "cloud_wordpress",
        "model": os.getenv("MODEL_CLOUD_WORDPRESS", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": 8192,
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "wordpress", "wp", "php", "cms",
            "wp_theme", "wp_plugin", "wp_rest_api",
            "high_complexity", "medium_complexity",
        ],
        "languages": ["php"],
    },
    {
        "id": "cloud_yii2",
        "model": os.getenv("MODEL_CLOUD_YII2", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": 8192,
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "yii2", "yii", "php", "mvc",
            "active_record", "rest_api",
            "high_complexity", "medium_complexity",
        ],
        "languages": ["php"],
    },
    # ── Cloud MAX — only as absolute last resort (expensive 480B, burns tokens) ──
    {
        "id": "cloud_max",
        "model": os.getenv("MODEL_CLOUD_MAX", "qwen3-coder:480b-cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "max_tokens": int(os.getenv("MODEL_CLOUD_MAX_TOKENS", "2048")),
        "cost_per_1k": 0.0,
        "provider": "cloud_ollama",
        "strengths": [
            "critical", "last_resort", "adversarial",
            "cross_language", "architecture",
        ],
        "languages": ["python", "javascript", "typescript", "php", "go", "rust"],
    },
    # ── OpenRouter Models (fallback for adversarial/swe_bench) ────────────
    {
        "id": "openrouter_fallback",
        "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"),
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "max_tokens": 4096,
        "cost_per_1k": 0.0,
        "provider": "openrouter",
        "strengths": [
            "general", "planning", "reasoning", "adversarial",
            "high_complexity", "swe_bench",
        ],
        "languages": ["python", "javascript", "typescript", "php", "go", "rust"],
    },
]

_DEFAULT_MODEL_CONFIG: dict[str, Any] = {
    "id": "fallback",
    "model": os.getenv("MODEL_NAME", "qwen3-coder-next:cloud"),
    "base_url": os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1"),
    "api_key": os.getenv("OPENAI_API_KEY", "ollama"),
    "max_tokens": 4096,
    "cost_per_1k": 0.0,
    "provider": "unknown",
    "strengths": [],
    "languages": [],
}


def _capabilities_for_complexity(complexity: str) -> list[str]:
    tag_map = {
        "low": ["low_complexity"],
        "medium": ["medium_complexity"],
        "high": ["high_complexity"],
    }
    return tag_map.get(complexity, ["medium_complexity"])


def _capabilities_for_tier(tier: str) -> list[str]:
    tag_map = {
        "standard": [],
        "adversarial": ["adversarial"],
        "swe_bench": ["swe_bench"],
    }
    return tag_map.get(tier, [])


def _score_model(task: TaskInput, candidate: dict[str, Any]) -> int:
    """
    Score a model candidate for a given task. Higher = better match.
    Pure deterministic scoring — no randomness.
    """
    score = 0
    lang = task.get("language", "python")
    complexity = task.get("complexity_estimate") or task.get("complexity", "medium")
    tier = task.get("tier", "standard")
    req_caps = task.get("required_capabilities", [])

    # Language match: critical
    if lang in candidate["languages"]:
        score += 100
    else:
        return -1  # disqualify: language not supported

    # Complexity match
    comp_tags = _capabilities_for_complexity(complexity)
    for tag in comp_tags:
        if tag in candidate["strengths"]:
            score += 50

    # Tier match
    tier_tags = _capabilities_for_tier(tier)
    for tag in tier_tags:
        if tag in candidate["strengths"]:
            score += 80

    # Required capabilities match
    for cap in req_caps:
        if cap in candidate["strengths"]:
            score += 30

    # Prefer cloud over local for high complexity
    if complexity == "high" and candidate["provider"] == "cloud_ollama":
        score += 20
    elif complexity == "low" and candidate["provider"] == "ollama":
        score += 10

    # Prefer higher max_tokens for complex tasks (proportional, breaks ties)
    if complexity == "high":
        score += min(candidate["max_tokens"] // 1024, 10)
    elif complexity == "medium" and candidate["max_tokens"] >= 4096:
        score += 5

    # Heavy penalty for cloud_max — only selected if NO other tier matches
    if candidate["id"] == "cloud_max":
        score -= 200

    return score


def select_model(task: TaskInput) -> dict[str, Any]:
    """
    Select the best model configuration for a given task.
    Deterministic: same task → same model (unless env vars change).

    Args:
        task: TaskInput with language, complexity_estimate, required_capabilities, tier

    Returns:
        Model configuration dict with keys: id, model, base_url, api_key, max_tokens, provider
    """
    tier = task.get("tier", "standard")

    # Special case: adversarial & swe_bench always use OpenRouter
    if tier in ("adversarial", "swe_bench"):
        or_config = next(
            (m for m in _MODEL_REGISTRY if m["id"] == "openrouter_fallback"),
            _DEFAULT_MODEL_CONFIG,
        )
        if or_config["api_key"]:
            return dict(or_config)
        return _fallback_with_log(or_config)

    # Score all candidates
    best = _DEFAULT_MODEL_CONFIG
    best_score = -1

    for candidate in _MODEL_REGISTRY:
        score = _score_model(task, candidate)
        if score > best_score:
            best_score = score
            best = candidate

    return dict(best)


def _fallback_with_log(failed: dict[str, Any]) -> dict[str, Any]:
    """Fallback to default config when OpenRouter is unavailable."""
    from utils.output import console
    console.print(
        f"[yellow]Model Router: {failed['id']} unavailable "
        f"(no OPENROUTER_API_KEY), falling back to default[/]"
    )
    return dict(_DEFAULT_MODEL_CONFIG)


def list_available_models() -> list[dict[str, Any]]:
    """Return all registered model configs (for diagnostics)."""
    return [dict(m) for m in _MODEL_REGISTRY]
