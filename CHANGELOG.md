# Changelog

## [2026-05-25] - Миграция на Ollama

### ✨ Добавлено
- Поддержка локальных моделей через Ollama
- Использование `qwen2.5-coder:3b-instruct` по умолчанию
- Автоматическая проверка доступности Ollama при запуске
- Конфигурация через переменные окружения `OLLAMA_MODEL`, `OLLAMA_VERIFIER_MODEL`
- `README_OLLAMA.md` с полной документацией по использованию Ollama
- `.env.example` с примерами конфигурации
- `test_reconciler.py` для тестирования reconciler с checkpoints

### 🔧 Изменено
- **nodes/planning.py**: Замена OpenRouter на Ollama
- **nodes/verification.py**: Замена OpenRouter на Ollama  
- **nodes/execution.py**: Уже использовал Ollama, без изменений
- **run_pipeline.py**: Проверка Ollama вместо OPENROUTER_API_KEY
- Удалены fallback механизмы для нескольких моделей (теперь одна модель Ollama)

### 🐛 Исправлено
- **graph/reconciler.py**: Критический баг с `_GeneratorContextManager`
  - Правильное использование `with SqliteSaver.from_conn_string(...)`
  - Замена `.get()` на `.get_tuple()`
  - Корректный доступ к checkpoint data через `checkpoint.get("channel_values")`

### 🗑️ Удалено
- Зависимость от OpenRouter API
- Проверки `OPENROUTER_API_KEY` в коде
- Логика fallback между несколькими моделями OpenRouter
- Функции `_get_candidate_models()` заменены на `_get_ollama_model()`

### 📝 Миграция

Для миграции существующего проекта:

```bash
# 1. Установить Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 2. Запустить Ollama
ollama serve

# 3. Скачать модель
ollama pull qwen2.5-coder:3b-instruct

# 4. Обновить .env (удалить OPENROUTER_API_KEY, добавить OLLAMA_*)
cp .env.example .env

# 5. Запустить пайплайн
python run_pipeline.py work/mvp/user-spec.md
```

## Тестирование

Все компоненты протестированы:

✅ Planning узел - работает с Ollama  
✅ Execution узел - работает с Ollama  
✅ Verification узел - работает с Ollama  
✅ Reconciler - исправлен и работает корректно  
✅ Полный пайплайн - проходит успешно  

## Производительность

Сравнение с OpenRouter:

| Метрика | OpenRouter | Ollama |
|---------|-----------|--------|
| Стоимость | ~$0.03/запрос | $0.00 |
| Задержка | 2-5s | 3-8s |
| Доступность | Требует интернет | Работает офлайн |
| Приватность | Данные уходят в API | Локально |

## Следующие шаги

- [ ] Добавить поддержку нескольких моделей Ollama для A/B тестирования
- [ ] Оптимизировать промпты для qwen2.5-coder
- [ ] Добавить кеширование результатов LLM
- [ ] Metrics и мониторинг использования Ollama
- [ ] Документация по выбору оптимальной модели для задачи
