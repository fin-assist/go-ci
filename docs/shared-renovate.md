# Общий контроллер Renovate (FIN-456)

`common/renovate/renovate_policy.py` — единственный production-контроллер, а соседние
Python/JS-файлы — его общий набор регрессий. Общий источник обслуживает 19 репозиториев:
android, contracts, infra, go-ci, оба Go-шаблона, bank-bff, user, bank-statement-data,
analytics, assistant, go-identity, go-idempotency, go-outbox, go-tombstone, go-postgres,
go-grpc, go-migrate, go-worker. Специализированные проверки этих проектов остаются отдельными.
Наличие исходника не подтверждает миграцию, merge, активацию или успешный reconcile каждого потребителя.

## Код и данные

У потребителя остаются `.github/renovate-policy.json`, `renovate.json`, `renovate-base.json`,
`scripts/renovate-runtime/package.json` и lockfile. State, receipts и presets остаются на
существующей защищённой ветке `renovate-policy`; перенос не переписывает её содержимое.
`ciWorkflow` выбирает `ci.yml` либо infra `stage.yml`; required check names и `ciChecks`
сохраняют смысл. Shared-source CI самого go-ci остаётся `shared-source` вместе с двумя policy checks.

Одна версия controller/test actions закрепляется полным 40-символьным SHA `fin-assist/go-ci`.
15 Go-потребителей используют существующий `.finassist-ci.json.revision`; android/contracts/infra —
нейтральный `.github/renovate-source.json` с единственным полем `revision`. Go-профиль для них не вводится.
`source-revision` обоих test steps должен совпадать с manifest и всеми controller/test action refs.
Renovate обновляет эти значения одной группой; самостоятельный bump одного поля отклоняется contract test.
Версии Renovate runtime и зависимости не повышаются переносом.

## Привилегированное исполнение

Тонкий workflow потребителя сохраняет workflow_run/schedule/manual triggers и concurrency.
Отдельный `gate` без checkout, Environment и secrets выдаёт только literal true/false после
точного shell-сравнения `RENOVATE_POLICY_ENABLED` с `true`. Значения TRUE/True/false/отсутствие
не допускают привилегированный job. Для старых 13 wrappers это заменяет прежний baseline-sync
при выключенном флаге на явный no-op до Environment и любых API операций.

Job reconcile допускается только в main и получает прежний Environment `renovate-policy`.
Он читает main checkout как данные, затем вызывает
`fin-assist/go-ci/.github/actions/renovate-policy@<полный SHA>` с inputs `enabled`,
`repository-root`, `token`, `read-token`. Actions разрешения не расширяются; token передаётся
только этой action, обычным PR/test jobs он недоступен. Composite action не устанавливает
Environment: он принадлежит вызывающему job.

Action запускает `python3 -I` и контроллер из собственного `github.action_path`, а не scripts,
bootstrap, runtime или артефакты потребителя. Единственный дополнительный Python import path —
каталог рассмотренного source. `--root` задаёт только местоположение JSON main checkout;
config/bootstrap JSON symlinks отклоняются. События PR и ref вне main отклоняются до inputs/API.
Загрузка конфигурации выбранного release target по API остаётся чтением JSON с pinned target commit.
Код target/PR при наличии write credential не исполняется.

Сохранены App15368 bindings, strict/owner-only/no-force/no-delete, bot/signature metadata,
выбор единственного активного release target или main, exact head/run/attempt, CAS и durable retry
budget. Оба late-conflict guard из рассмотренного FIN-451 присутствуют: fresh перед policy write
после чтения quarantine/recovery и перед spent-retry UX/receipt repair. Post-policy/final-close
guards сохранены. Ручные Go compiler/toolchain updates, включая uses-with, Docker и mixed PR,
не допускаются к merge/retry/close. AndroidX/Compose и блокировка стека остаются ручными.

Сам go-ci не может включить в создаваемый коммит SHA этого же коммита. Только его собственные
workflows используют `./.github/actions/renovate-policy` после явного checkout main. Это
рассмотренный source main, а не изменяемый upstream main потребителя. Source PR tests выполняют
локальные actions без mutation credential. Исключение не разрешает remote `@main` у потребителей.

## Проверки и локальный запуск

`.github/actions/renovate-policy-tests` принимает `repository-root`, `mode=policy|runtime`,
`runtime-path` и `source-revision`. Caller сохраняет setup Python/Node и `npm ci --ignore-scripts`
с текущим runtime lock. Test action не получает write token, не устанавливает зависимости и
исполняет только canonical tests. Вызывать test runner с mutation credential запрещено.

```sh
python3 -I <common>/renovate/test.py --root <consumer> --mode policy --source-revision <SHA>
python3 -I <common>/renovate/test.py --root <consumer> --mode runtime --runtime <locked-runtime> --source-revision <SHA>
```

В source используется `make policy-test`, без remote self pin. У остальных локальный read-only
test shim получает `<common>` существующим raw-object-verified bootstrap. У специальных проектов
используется та же побайтная копия bootstrap и только `source_dir(root, revision)`, без Go-profile
rendering. `common/bootstrap.py` не меняется. Runtime JS получает caller manifest/config явно и
проверяет точную установленную версию; consumer scripts не импортируются.

Все процессы имеют timeout: Python suite90s, каждый actual Renovate fixture90s, test action300s,
production controller540s; job/внешние локальные лимиты также сохраняются. Npm install scripts
не исполняются. JavaScript RegExp fallback в локальных fixtures явно выводится; он не доказывает
hosted RE2 или загрузку presets в App. Полные required CI и реальные reconcile proofs остаются
отдельными доказательствами после разрешённых владельцем merges.

Явный bootstrap сохраняется как отдельная операция владельца, независимо от activation:
`python3 -I <common>/renovate/renovate_policy.py --root <consumer> --bootstrap`.
Он требует credential и не включается action или тестами автоматически.

## Обновление и откат

Source изменение проходит свой PR/required CI/Codex и отдельное разрешение merge владельца.
Сначала source SHA должен существовать и быть сохранён в рассмотренной main lineage, затем
потребители получают reviewable PR согласованного bump manifest/action/test inputs. Проверка
байтов SHA сама по себе не доказывает одобрение source или его main ancestry. Squash/rebase
требует repin итогового рассмотренного SHA и нового CI/review потребителей.

Откат возвращает прежний manifest, все refs/inputs и matching bootstrap одной согласованной
правкой. Состояния retry, presets и secrets при rollback не сбрасываются, теги не создаются.
Merge, activation, protections, secrets и иные внешние операции требуют своего разрешения владельца.
