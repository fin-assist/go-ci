# FinAssist Go CI

Единый источник CI и конфигураций линтеров 15 Go-репозиториев FinAssist ([FIN-372](https://finassist.youtrack.cloud/issue/FIN-372)). Отдельный репозиторий не является Go-сервисом, библиотекой или шаблоном и не содержит искусственного Go-модуля ради CI.

- `.github/workflows/shared-*.yml`: reusable Go/coverage, lint, SQL, API compatibility и image workflows.
- `common/configs/`, `common/versions.json`: профили и закреплённые версии линтеров.
- `common/bootstrap.py`: единственный канонический bootstrap, побайтно копируемый потребителями.
- `common/tests/`, `common/probe_lint.py`: регрессионные тесты и реальные разрешённые/запрещённые lint-сценарии.
- `scripts/renovate_policy.py`, `renovate-base.json`: локальный контроллер обновлений и рассмотренный baseline ([FIN-447](https://finassist.youtrack.cloud/issue/FIN-447)). [Подготовка, защиты и отдельная активация](docs/renovate-policy.md).

[Профили, параметры, локальные команды, доверие и порядок перехода](docs/shared-go-ci.md).

## Проверка источника

Нужны Python3.11+ с `pip install -r requirements-dev.txt`, Go1.26+ с `GOTOOLCHAIN=auto`, Node22/npm и Git. Первый запуск инструментов требует сети; offline-проверка дополнительно требует заполненных Go/npm-кэшей. У source нет production Go-кода: независимые unit/integration/API total80 проверяются в настоящих потребителях, а здесь проверяются policy/helpers и реальные инструменты.

```sh
make test
make policy-test
make lint
make probe
```

Локально используйте реальный внешний таймаут процесса. В CI job ограничен30минутами, вызовы helper subprocess также ограничены.

## Подключение и обновление

Потребитель закрепляет полный SHA `fin-assist/go-ci` в `.finassist-ci.json`, всех workflow `uses:` и входных `revision`. Профили `service` и `library` сохраняют свои правила; N/A исходного `go-library-template` не наследуется другими библиотеками. Изменяемая ветка не исполняется напрямую.

Renovate обновляет pins одним PR. Если `common/bootstrap.py` изменился, в том же PR синхронизируется `scripts/common_ci.py` выбранного SHA; byte-equality contract блокирует неполный переход. Человеческий commit переводит Renovate PR в ручное сопровождение по FIN-211. Откат возвращает matching pins и bootstrap; откат первоначального переноса также возвращает прежние координаты источника.

Обычные source PR проходят ревью и требуют команды владельца на merge. Сначала source merge-коммитом сохраняется в `main`, затем подключаются потребители; squash/rebase требует согласованного repin и повторного CI. Теги автоматически не создаются.

После отдельной активации FIN-211 контроллер обрабатывает только подтверждённые bot-only обновления зависимостей после `shared-source`, `Renovate policy tests` и `Renovate runtime contract` последнего SHA. Сам source-код этим разрешением не наделяется. Обновление Go-компилятора/toolchain, включая `go-version` в reusable workflows, Docker и смешанные PR, остаётся ручным: оно запрещено baseline и отдельно проверяется по metadata подписанного Renovate commit. Наличие workflow в PR не означает, что контроллер уже включён; внешний bootstrap собственных presets и проверка защит должны завершиться до мержа конфигурации/активации.

## Профильное ревью (FIN-421)

Перед ревью применяются [профильные навыки](docs/review-skills/README.md) из проверяемой ревизии.
Обязательные требования, выбор и ограничения — в `AGENTS.md`, раздел `Code Review Rules`.
