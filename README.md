# FinAssist Go CI

Единый источник CI и конфигураций линтеров 15 Go-репозиториев FinAssist ([FIN-372](https://finassist.youtrack.cloud/issue/FIN-372)). Отдельный репозиторий не является Go-сервисом, библиотекой или шаблоном и не содержит искусственного Go-модуля ради CI.

- `.github/workflows/shared-*.yml`: reusable Go/coverage, lint, SQL, API compatibility и image workflows.
- `common/configs/`, `common/versions.json`: профили и закреплённые версии линтеров.
- `common/bootstrap.py`: единственный канонический bootstrap, побайтно копируемый потребителями.
- `common/tests/`, `common/probe_lint.py`: регрессионные тесты и реальные разрешённые/запрещённые lint-сценарии.

[Профили, параметры, локальные команды, доверие и порядок перехода](docs/shared-go-ci.md).

## Проверка источника

Нужны Python3.11+ с `pip install -r requirements-dev.txt`, Go1.26+ с `GOTOOLCHAIN=auto`, Node22/npm и Git. Первый запуск инструментов требует сети; offline-проверка дополнительно требует заполненных Go/npm-кэшей. У source нет production Go-кода: независимые unit/integration/API total80 проверяются в настоящих потребителях, а здесь проверяются policy/helpers и реальные инструменты.

```sh
make test
make lint
make probe
```

Локально используйте реальный внешний таймаут процесса. В CI job ограничен30минутами, вызовы helper subprocess также ограничены.

## Подключение и обновление

Потребитель закрепляет полный SHA `fin-assist/go-ci` в `.finassist-ci.json`, всех workflow `uses:` и входных `revision`. Профили `service` и `library` сохраняют свои правила; N/A исходного `go-library-template` не наследуется другими библиотеками. Изменяемая ветка не исполняется напрямую.

Renovate обновляет pins одним PR. Если `common/bootstrap.py` изменился, в том же PR синхронизируется `scripts/common_ci.py` выбранного SHA; byte-equality contract блокирует неполный переход. Человеческий commit переводит Renovate PR в ручное сопровождение по FIN-211. Откат возвращает matching pins и bootstrap; откат первоначального переноса также возвращает прежние координаты источника.

Обычные source PR проходят ревью и требуют команды владельца на merge. Сначала source merge-коммитом сохраняется в `main`, затем подключаются потребители; squash/rebase требует согласованного repin и повторного CI. Теги автоматически не создаются.
