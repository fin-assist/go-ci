# FinAssist Go CI

Единый источник CI и конфигураций линтеров 15 Go-репозиториев FinAssist ([FIN-372](https://finassist.youtrack.cloud/issue/FIN-372)). Отдельный репозиторий не является Go-сервисом, библиотекой или шаблоном и не содержит искусственного Go-модуля ради CI.

- `.github/workflows/shared-*.yml`: reusable Go/coverage, lint, SQL, API compatibility, image и service release workflows.
- `common/configs/`, `common/versions.json`: профили и закреплённые версии линтеров.
- `common/bootstrap.py`: единственный канонический bootstrap, побайтно копируемый потребителями.
- `common/tests/`, `common/probe_lint.py`: регрессионные тесты и реальные разрешённые/запрещённые lint-сценарии.
- `common/renovate/`: единый контроллер Renovate и его Python/JS-регрессии для 19 репозиториев; `renovate-base.json` остаётся локальными данными ([FIN-447](https://finassist.youtrack.cloud/issue/FIN-447)). [Подготовка, защиты и отдельная активация](docs/renovate-policy.md).

[Профили, параметры, локальные команды, доверие и порядок перехода](docs/shared-go-ci.md).

FIN-457: [тесты и coverage](docs/shared-quality.md), [stage-артефакты](docs/stage-artifacts.md),
[parser CHANGELOG](docs/changelog-tooling.md), [сервисные GitHub Release](docs/service-release.md).

FIN-517: [CI и приватная доставка при смене видимости сервиса](docs/visibility-independent-ci.md).

## Проверка источника

Нужны Python3.11+ с `pip install -r requirements-dev.txt`, Go1.27.4+ с `GOTOOLCHAIN=auto`, Node22/npm и Git. Первый запуск инструментов требует сети; offline-проверка дополнительно требует заполненных Go/npm-кэшей. У source нет production Go-кода: независимые unit/integration/API total80 проверяются в настоящих потребителях, а здесь проверяются policy/helpers и реальные инструменты.

```sh
make test
make policy-test
make lint
make probe
```

Локально используйте реальный внешний таймаут процесса. В CI job ограничен30минутами, вызовы helper subprocess также ограничены.

## Подключение и обновление

По [FIN-516](https://finassist.youtrack.cloud/issue/FIN-516) Go-сервисы, библиотеки и оба шаблона вызывают общий CI из защищённой ветки `main` (`@refs/heads/main`). Слияние PR в `go-ci` меняет версию CI для следующих запусков без PR, ревью и правок SHA у потребителей. В рамках запуска точный SHA reusable workflow записывается в отчёт и связывается с артефактом приватных зависимостей. В `.finassist-ci.json` остаются профиль и локальные исключения.

Обычные PR в `go-ci` проходят ревью и обязательные проверки, слияние остаётся за владельцем. Откат выпуска общего CI — обычный revert в `go-ci`. Потребители, ещё не прошедшие переход, могут использовать старый контракт с полным SHA.

После отдельной активации FIN-211 контроллер обрабатывает только подтверждённые bot-only обновления зависимостей после `shared-source`, `Renovate policy tests` и `Renovate runtime contract` последнего SHA. Сам source-код этим разрешением не наделяется. Обновление Go-компилятора/toolchain, включая `go-version` в reusable workflows, Docker и смешанные PR, остаётся ручным: оно запрещено baseline и отдельно проверяется по metadata подписанного Renovate commit. Наличие workflow в PR не означает, что контроллер уже включён; внешний bootstrap собственных presets и проверка защит должны завершиться до мержа конфигурации/активации.

## Профильное ревью (FIN-421)

Перед ревью применяются [профильные навыки](docs/review-skills/README.md) из проверяемой ревизии.
Обязательные требования, выбор и ограничения — в `AGENTS.md`, раздел `Code Review Rules`.

[Общий контроллер Renovate, immutable pins и границы доверия (FIN-456)](docs/shared-renovate.md).
