# Developer Farm - Работа с Ollama

Этот проект теперь использует **локальную модель Ollama** вместо OpenRouter для генерации и проверки кода.

## 🚀 Быстрый старт

### 1. Установка Ollama

```bash
# Linux
curl -fsSL https://ollama.com/install.sh | sh

# macOS
brew install ollama

# Или скачайте с https://ollama.com/download
```

### 2. Запуск Ollama и загрузка модели

```bash
# Запустить Ollama сервер
ollama serve

# В другом терминале: скачать модель qwen2.5-coder
ollama pull qwen2.5-coder:3b-instruct
```

### 3. Проверка что Ollama работает

```bash
curl http://localhost:11434/api/tags
```

Должен вернуть список доступных моделей, включая `qwen2.5-coder:3b-instruct`.

### 4. Запуск пайплайна

```bash
# Активировать venv
source venv/bin/activate

# Запустить пайплайн
python run_pipeline.py work/mvp/user-spec.md
```

## 📋 Конфигурация

Создайте файл `.env` (или используйте `.env.example` как шаблон):

```bash
# Ollama настройки
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5-coder:3b-instruct
OLLAMA_VERIFIER_MODEL=qwen2.5-coder:3b-instruct

# Dashboard (опционально)
DASHBOARD_PORT=8081
DASHBOARD_HOST=0.0.0.0
```

## 🔧 Доступные модели

Вы можете использовать другие модели из Ollama:

```bash
# Модели для кодирования
ollama pull qwen2.5-coder:7b-instruct    # Больше, точнее
ollama pull qwen2.5-coder:14b-instruct   # Еще больше
ollama pull deepseek-coder:6.7b          # Альтернатива
ollama pull codellama:13b                # CodeLlama

# Обновите .env файл
OLLAMA_MODEL=qwen2.5-coder:7b-instruct
```

## 🧪 Тестирование компонентов

### Тест Planning узла

```bash
python -m nodes.planning
```

### Тест Execution узла

```bash
python -m nodes.execution
```

### Тест Verification узла

```bash
python -m nodes.verification
```

## 📊 Reconciler (мониторинг)

Reconciler теперь работает корректно с checkpoints:

```bash
# Создать тестовые checkpoints
python test_reconciler.py --create-test-data

# Запустить reconciler
python test_reconciler.py --reconciler

# Симулировать пайплайн с heartbeats
python test_reconciler.py --run-pipeline
```

## 🐛 Исправленные баги

### ✅ Reconciler bug (2026-05-25)

**Проблема**: `'_GeneratorContextManager' object has no attribute 'get'`

**Исправление**: 
- Использование `with SqliteSaver.from_conn_string(...)` вместо прямого присваивания
- Замена `.get()` на `.get_tuple()` 
- Правильный доступ к checkpoint data через `checkpoint_tuple.checkpoint.get("channel_values")`

## 🔄 Миграция с OpenRouter

Если у вас был старый код с OpenRouter:

1. **Удалить** `OPENROUTER_API_KEY` из `.env`
2. **Установить** Ollama (см. выше)
3. **Запустить** `ollama serve`
4. **Скачать** модель `ollama pull qwen2.5-coder:3b-instruct`
5. **Готово!** Код автоматически использует Ollama

## 💡 Преимущества Ollama

- ✅ **Бесплатно** - нет затрат на API
- ✅ **Локально** - работает без интернета
- ✅ **Приватно** - данные не уходят во внешние сервисы
- ✅ **Быстро** - нет задержек сети
- ✅ **Контроль** - можно выбрать любую модель

## 📈 Производительность

| Модель | Размер | RAM | Скорость | Качество |
|--------|--------|-----|----------|----------|
| qwen2.5-coder:3b | 1.9GB | ~4GB | ⚡⚡⚡ | ⭐⭐⭐ |
| qwen2.5-coder:7b | 4.7GB | ~8GB | ⚡⚡ | ⭐⭐⭐⭐ |
| qwen2.5-coder:14b | 8.9GB | ~16GB | ⚡ | ⭐⭐⭐⭐⭐ |

## 🔗 Полезные ссылки

- [Ollama Documentation](https://github.com/ollama/ollama)
- [Qwen2.5-Coder Models](https://ollama.com/library/qwen2.5-coder)
- [LangChain Ollama Integration](https://python.langchain.com/docs/integrations/llms/ollama)
