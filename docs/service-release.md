# Общий service release workflow (FIN-457)

`shared-release.yml` подключается отдельной итерацией после [общего parser](changelog-tooling.md).
Он содержит только `workflow_call` с обязательным string input `tag` (существующий `vX.Y.Z`). `revision` допустим для прежних закреплённых вызовов; `checkout_version` — необязательный закрытый selector
`v4|v7` с default `v4`. Новые secrets, Environment или права не добавляются:
отдельный release job сохраняет `contents: write`, качество PR выполняется другими jobs.

Тонкий caller сохраняет свои `push.tags: ['v*']`, `workflow_dispatch` с input `tag`,
manual-only-main condition и прежнее имя release job. Caller сохраняет только
`permissions: {contents: write}` и не передаёт `secrets` общему workflow. После FIN-516 его `uses` выбирает `@refs/heads/main`. Точный SHA workflow записывается в отчёт. `with.tag` передаёт
`${{ inputs.tag || github.ref_name }}` как данные; source проверяет точный event/ref:
только matching tag push либо manual `refs/heads/main`. PR, workflow_run, другой manual ref
и несогласованный tag/ref не допускаются. Case-sensitive Python проверка применяется до
checkout; Actions equality сама по себе этой гарантии не даёт.

GitHub загружает composite action и parser из того же commit, что и reusable workflow. Читается CHANGELOG из checkout запрошенного тега. Локальный код тега не исполняется с write token.
Checkout потребителя сохраняет прежний major: BFF явно передаёт `checkout_version: v7`,
остальные пять используют default `v4`. Значение проверяется case-sensitive до checkout;
только одна из двух conditional steps использует соответствующий полный immutable SHA.
Все checkout steps отключают persist-credentials;
версии сервисных checkout и образов этим переносом не повышаются.

До публикации tag commit должен быть предком origin/main из начального full checkout, формат CHANGELOG должен
пройти check, а его newest version — совпасть с тегом. Manual run использует metadata тега,
даже если workflow main уже продвинулся. Поэтому tag manifest не сравнивается с текущим
версией CI: parser выбирается ревизией reusable workflow, а релизные notes — историческим тегом.
Тег создаёт владелец после release PR; workflow не создаёт и не перемещает теги.
Checkout `fetch-depth: 0` получает все heads/tags, в том числе origin/main, пока доступен
его собственный read credential. Затем ref проверяется без нового network fetch:
`persist-credentials: false` не оставляет credential для private origin. Отсутствующий
origin/main блокирует публикацию; дополнительный секрет или auth header не нужен.

`gh release view` сохраняет существующий release целиком. Новый release создаётся только
с `--verify-tag` и notes указанной секции. Если create вернул ошибку, повторное чтение
подтверждает concurrent/неоднозначную публикацию; без подтверждения job падает. Команд
edit/upload/clobber/delete нет: notes и assets существующего release не перезаписываются.
Публикация образов и deployment остаются отдельными механизмами.

## Миграция и проверка

Consumer `scripts/changelog.py` остаётся тонким совместимым CLI wrapper с прежними
check/latest/notes командами и неизменённым bootstrap resolver. Локальный `release-check`
продолжает проверять подготовленную newest CHANGELOG version до создания тега.
Общие дубли `scripts/test_changelog.py` и `scripts/test_release_workflow.py` переходят
в `common/tests/`; тонкий caller, pin agreement и локальные wrappers проверяет consumer CI.
Source regression suite сохраняет service/library contract fixtures без переноса library
release workflows. Исходная область FIN-457 — только пять сервисов и сервисный шаблон.

Tests исполняют actual release shell на temporary Git/filesystem fixtures и stateful fake
GitHub CLI с process timeouts; настоящие GitHub tags/releases не создаются. Positive, отказ,
повтор и concurrent create покрыты. Source tests и actionlint не подтверждают реальный
release: для него нужны owner-created tag и отдельно разрешённая публикация.
