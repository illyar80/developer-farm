"""
MODEL ROUTER — LLM-Assisted Model Selection
-------------------------------------------
Iterates provider chain (ollama local → ollama cloud → openrouter) to select
the optimal execution model via LLM analysis of the actual task description.

Fallback: deterministic select_model() when all LLM providers fail.
"""

import asyncio
import json
import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from utils.output import console

from contracts import TaskInput
from utils.model_router import (
    _MODEL_REGISTRY,
    _DEFAULT_MODEL_CONFIG,
    select_model,
    list_available_models,
)

MODEL_ROUTER_TIMEOUT = int(os.getenv("MODEL_ROUTER_TIMEOUT", "15"))

# ─── Provider Configs ───────────────────────────────────────────────────────

_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "ollama_local",
        "model": os.getenv("MODEL_ROUTER_LOCAL_MODEL", "qwen2.5-coder:3b-instruct"),
        "base_url": os.getenv("OPENAI_API_BASE", "http://localhost:11434/v1"),
        "api_key": "ollama",
        "label": "local 3b",
    },
    {
        "name": "ollama_cloud",
        "model": os.getenv("MODEL_ROUTER_CLOUD_MODEL", "qwen3-coder-next:cloud"),
        "base_url": os.getenv("CLOUD_OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "api_key": os.getenv("CLOUD_API_KEY", "ollama"),
        "label": "cloud qwen3",
    },
    {
        "name": "openrouter",
        "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"),
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "label": "OpenRouter free",
    },
]


def _build_router_prompt(task: TaskInput) -> str:
    """Build a concise prompt for the router LLM."""

    models_desc = []
    for m in _MODEL_REGISTRY:
        if m["id"] == "cloud_max":
            continue  # don't expose 480B to LLM router — deterministic only
        strengths = ", ".join(m["strengths"])
        models_desc.append(
            f"- {m['id']} ({m['model']}, {m['provider']}): "
            f"max_tokens={m['max_tokens']}, languages={', '.join(m['languages'])}\n"
            f"  Best for: {strengths}"
        )

    registry_text = "\n".join(models_desc)
    caps = task.get("required_capabilities", [])
    complexity = task.get("complexity_estimate") or task.get("complexity", "medium")
    tier = task.get("tier", "standard")

    return f"""You select the best execution model for a code generation task.

Available models:
{registry_text}

Task:
- Description: {task['description'][:500]}
- Language: {task['language']}
- Complexity: {complexity}
- Tier: {tier}
- Required capabilities: {', '.join(caps) if caps else 'none'}
- Target path: {task['target_path']}

Select the best model. Think about language support, task complexity, and capability fit.
Respond with ONLY this JSON — no other text:
{{"model_id": "<id from list above>", "reason": "<one-line why>"}}"""


def _parse_router_response(raw: str) -> tuple[str, str] | None:
    """Parse LLM response, return (model_id, reason) or None."""
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    model_id = data.get("model_id", "")
    reason = data.get("reason", "")

    if not model_id:
        return None

    valid_ids = {m["id"] for m in _MODEL_REGISTRY}
    if model_id not in valid_ids:
        return None

    return model_id, reason


async def _try_provider(provider: dict[str, Any], prompt: str) -> str | None:
    """Try a single LLM provider, return raw response or None."""
    api_key = provider.get("api_key", "")
    if not api_key:
        return None

    try:
        llm = ChatOpenAI(
            base_url=provider["base_url"],
            api_key=SecretStr(api_key),
            model=provider["model"],
            temperature=0.1,
            max_tokens=256,
            timeout=MODEL_ROUTER_TIMEOUT,
        )
        response = await asyncio.to_thread(
            llm.invoke,
            [
                SystemMessage(content="You are a precise model router. Output only valid JSON."),
                HumanMessage(content=prompt),
            ],
        )
        return response.content.strip()
    except Exception as e:
        console.print(f"[dim]Router provider {provider['name']}: {e}[/]")
        return None


def _resolve_config(model_id: str) -> dict[str, Any]:
    """Look up full model config by ID from registry."""
    for m in _MODEL_REGISTRY:
        if m["id"] == model_id:
            return dict(m)
    return dict(_DEFAULT_MODEL_CONFIG)


async def select_model_llm(task: TaskInput) -> tuple[dict[str, Any], str, str]:
    """
    LLM-assisted model selection via provider chain.

    Returns:
        (model_config, provider_name, reason) on success
        (None, "deterministic_fallback", "") if all providers fail
    """
    prompt = _build_router_prompt(task)

    for provider in _PROVIDERS:
        cfg_name = provider["name"]

        # Skip openrouter if no API key
        if cfg_name == "openrouter" and not os.getenv("OPENROUTER_API_KEY"):
            continue

        console.print(f"[dim]Router: trying {cfg_name} ({provider['label']})...[/]")
        raw = await _try_provider(provider, prompt)
        if raw is None:
            continue

        parsed = _parse_router_response(raw)
        if parsed is None:
            console.print(f"[yellow]Router: {cfg_name} returned invalid response, skipping[/]")
            continue

        model_id, reason = parsed

        # Never select cloud_max (480B) via LLM — only deterministic as last resort
        if model_id == "cloud_max":
            console.print(f"[yellow]Router: {cfg_name} picked cloud_max (forbidden), falling through[/]")
            continue

        # Validate language compatibility (safety net)
        chosen = _resolve_config(model_id)
        lang = task.get("language", "python")
        if lang not in chosen.get("languages", []) and lang != "any":
            console.print(
                f"[yellow]Router: {cfg_name} picked {model_id} which doesn't support "
                f"{lang}, falling through[/]"
            )
            continue

        console.print(f"[green]Router: {cfg_name} → {model_id} ({reason})[/]")
        return chosen, cfg_name, reason

    return None, "deterministic_fallback", ""
