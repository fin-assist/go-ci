# Автоматизация Renovate (FIN-447)

В `go-ci` добавлен тот же локальный контроллер FIN-211/FIN-237, что в
[`go-postgres` на `1d02eff`](https://github.com/fin-assist/go-postgres/tree/1d02eff31276d9dbbb07755d7c9ee4f7cc0a9580).
Изменения source-кода проходят обычное ревью и merge владельца. После **отдельной активации**
контроллер может сливать только подтверждённые полностью ботские обновления зависимостей.
Наличие этих файлов в PR или `main` само по себе не подтверждает активацию.

## Область и проверки

- `renovate-base.json` сохраняет `config:recommended`; Go-компилятор и toolchain повышаются вручную.
  Это включает `go`/`toolchain` в `go.mod`, версии `actions/setup-go` в `with.go-version`
  (`packageName=actions/go-versions`), Go Docker-образы и смешанные PR с таким обновлением.
  Обновление самого action `actions/setup-go` остаётся допустимым обновлением зависимости.
- Native `automerge: false` сохраняется. Сливает контроллер обычным REST merge с ожидаемым head SHA,
  без административного обхода и без отложенной native auto-merge очереди.
- `.github/renovate-policy.json` требует `shared-source`, `Renovate policy tests` и
  `Renovate runtime contract`. `ciChecks` содержит `shared-source`; событие `workflow_run`
  слушает фактические имена `Shared CI self checks` и `Renovate policy tests`.
- Проверки должны закончиться `success` на последнем SHA и текущей попытке PR workflow.
  Отсутствующие, pending, skipped, cancelled, timed-out и неуспешные checks не разрешают merge.
  Дополнительные required checks из реальной защиты также учитываются.
- Контроллер исполняет только reviewed код `main`. Он не скачивает PR artifacts/cache и
  не выполняет PR-код с write credential. Обычные PR, чужой/fork head и человеческие коммиты
  не получают прав на автомерж. UI-прогоны, выпуск тегов и deployment этим не разрешаются.

Автор PR — Renovate bot ID29139614, тот же репозиторий и ветка `renovate/`. Каждый commit
имеет bot author, действительную подпись и разрешённого signer/committer (`web-flow` или Renovate).
Проверяется полный список коммитов, до250; неполные данные блокируют действия.
Metadata обновлений берутся только из проверенного подписанного head commit, не из редактируемого
PR body. `updates()` нормализует packageName. Go toolchain guard применяется до merge/retry/close,
включая ранее открытые и смешанные PR. Старые PR без подписанных metadata остаются владельцу;
их нельзя сделать допустимыми простым включением флага или зелёным CI.

## Цель и конфигурационная ветка

Единственная цель — самая новая `release/X.Y.Z`, для которой ещё нет одновременно смёрженного
PR из того же репозитория в `main` и подтверждённой предковости. При отсутствии активной release —
`main`. Только что созданная от main ветка остаётся активной до release merge PR.
Цель и PR head/commit/CI повторно проверяются перед действием. Конфликтующий либо ещё не рассчитанный mergeability PR остаётся владельцу до claim, quarantine и close; свежая mergeability повторно проверяется перед действием. Red CI со state blocked не считается конфликтом.
Смена ветки между последним
чтением и merge не атомарна: общей API-транзакции GitHub для этого нет.

`renovate.json` подключает только собственные presets в порядке baseline → generated policy:
`local>fin-assist/go-ci:base#renovate-policy` и `local>fin-assist/go-ci:policy#renovate-policy`.
В config-only ветке находятся только `base.json`, `policy.json` и `state.json`, без workflows/кода.
`base.json` копируется из рассмотренного `renovate-base.json` выбранной цели по commit SHA как JSON.
Если активная цель baseline не содержит, контроллер останавливается до записей.
Generated policy принимает только один target и строго ограниченные generated recovery/quarantine
rule shapes. Произвольные matcher/JSONata, native automerge и посторонние поля отклоняются.

Защиты `main` и `release/*`: strict required checks от GitHub Actions App15368 (runtime проверяет каждую привязку и отсутствие дубликатов),
`enforce_admins: true`, push только controllerLogin (`avtomon`), teams/apps пусты,
без force/delete (оба флага runtime требует явно false). Matching `release/*` дополнительно запрещает создание ветки другим actor.
Ветка `renovate-policy` защищается отдельно: owner-only push, enforce_admins, без force/delete,
без требований PR/CI. Существующие ограничения обычных PR сохраняются. Runtime проверяет защиты,
identity и inherited rulesets; active или неизвестные rulesets блокируют действия до проверки совместимости.

## Retry, закрытие и восстановление

Полностью ботский допустимый PR с завершённым failure обязательного CI получает один повтор
упавших jobs каждого нужного workflow. Budget сохраняется в защищённом `state.json` по PR+SHA
с optimistic file SHA **до** запроса retry. Комментарий только объясняет результат; его удаление
не сбрасывает budget. Отсутствующий/повреждённый state, CAS-конфликт, неизвестный run/attempt и ошибка API
блокируют дальнейшие действия. Старые SHA receipts сохраняются.

Только подтверждённый failure точной следующей попытки разрешает закрытие с одним комментарием:
SHA, проверки, ссылки на runs, результат повтора и путь восстановления. Pending, skipped,
cancelled, отсутствие CI, конфликт и человеческие изменения не служат причиной автозакрытия.
Если сохранённый claim не продвинулся в следующий attempt, workflow остаётся красным с сигналом
владельцу проверить Actions; слепого второго dispatch нет. Успешный retry возвращает PR к обычным
merge guards; новый SHA получает самостоятельную историю.

Перед закрытием generated preset сохраняет exact quarantine неудачной версии/digest,
Dashboard approval и новый retry prefix. Новые значения не скрываются навсегда:
scoped recovery действует только на зависимость+datasource+major (для digest — прежний tag).
Остальные зависимости/major сохраняют обычную семантику ignored. Для повтора неудачного значения
владелец отмечает Pending Approval/Closed в Dependency Dashboard. Повторный failure новой попытки
создаёт новую generation approval. Exact failed значения не пересоздаются бесконечно.

Recovery допускает повторное появление **вручную закрытого** обновления той же dependency family.
Чтобы окончательно отклонить family, владелец удаляет соответствующее recovery rule reviewable правкой;
для отдельной версии сохраняется exact quarantine. Глобальный `recreateWhen` не меняется.
Прямого удаления веток, автоматической очистки receipt history или сброса budget нет.
При достижении schema limits (1000 rules или1MB receipts) нужен отдельный разбор владельца.

## Подготовка и отдельная активация

1. `RENOVATE_POLICY_ENABLED` оставляют отсутствующим или не равным точному `true`.
   Непривилегированный shell gate без Environment/secrets/permissions сравнивает регистр и
   выдаёт только константу true/false; `TRUE`, `True`, пустое значение не допускают controller job.
   CLI тоже завершает работу до чтения config/event/credentials и API. В паузе нет baseline/target sync,
   merge/retry/close; сам hosted Renovate продолжает работать независимо.
2. **До merge `renovate.json`** явно bootstrap собственную config-only ветку:
   `GITHUB_REPOSITORY=fin-assist/go-ci python3 scripts/renovate_policy.py --bootstrap`.
   Команда требует уже установленный в окружении `RENOVATE_POLICY_TOKEN`, не выводит его и
   не включает автоматизацию. Существующая ветка никогда не перезаписывается.
   Настоящий resolver обоих опубликованных presets проверяется отдельно; PR этого не доказывает.
3. После ревью/CI и разрешённого владельцем merge сверяют обязательные checks и строгие защиты,
   config-only state, Environment `renovate-policy` с custom deployment branch ровно `main`, тип branch.
   Код PR не меняет эти внешние настройки и не активирует контроллер.
4. Используется существующий organization secret `RENOVATE_POLICY_TOKEN`; нового PAT, переноса
   секрета в Environment или вывода значения не требуется. Проверяются отдельно доступность org secret
   и фактические права существующего fine-grained PAT на репозиторий, identity controllerLogin.
   Права: Contents/Pull requests/Issues/Actions/Workflows write, Administration/Commit statuses/Metadata read.
   Check-runs читает отдельный `GITHUB_TOKEN` с checks:read, этот токен не выполняет mutations,
   поскольку события от него подавляют следующие Actions. Installation-token identity здесь не поддержана.
5. Только после отдельной команды владельца устанавливают точное `true`, проверяют выполненный
   `reconcile` на trusted main и фактическую обработку подходящего подписанного PR. Общий success
   с `reconcile=skipped` не подтверждает активацию; отсутствие подходящего PR не доказывает merge/retry/close.

Environment ограничивает controller job, но organization secret остаётся доступен другим workflows
в области выбранных репозиториев. Эта принятая в FIN-211 граница доверия сохраняется; Environment-only
secret может быть отдельным усилением, не обязательным условием. Токены reviewers/modules не переиспользуются,
native allow_auto_merge остаётся выключенным, bypass не добавляется.

## Проверки

`make policy-test` с внешним process timeout проверяет controller, CLI, реальный shell gate,
выбор target, подписи, SHA/attempt, native checks, retry/quarantine/receipts/CAS и manual Go,
включая mixed PR при success и failure. `make test`, `make lint`, `make probe` сохраняют
существующие source gates и совместимость15 потребителей. Production common code/bootstrap не меняется.

Node24 соответствует engines закреплённого Renovate. `scripts/renovate-runtime/package.json` и lock
закрепляют runtime и integrity транзитивных зависимостей. С process timeout600 выполнить
`npm ci --ignore-scripts --no-audit --no-fund` в этом каталоге; install scripts не исполняются.
`RENOVATE_RUNTIME=$PWD/scripts/renovate-runtime` задаёт расположение runtime для трёх fixtures:
`test_renovate_runtime.mjs`, `test_renovate_metadata_runtime.mjs` (Node с
`--experimental-test-module-mocks`) и `test_renovate_go_runtime.mjs`. Каждый процесс ограничивается90s.
Fixtures вызывают реальные branchify/processBranch/Dashboard/renderer/extractor/matcher;
внешние mutations/network adapters заменены фейками. Native RE2 install отсутствует — fixture явно
сообщает JavaScript RegExp fallback, а не имитирует проверку hosted RE2.

После bootstrap опубликованные presets проверяют настоящим resolver с внешним timeout180:
`RENOVATE_RUNTIME=<runtime prefix> node scripts/verify_renovate_presets.mjs`.
Необходимый read credential передаётся в RENOVATE_TOKEN, не выводится. Hosted App loading и
controller runtime — отдельные доказательства после настройки/активации.

Источники: [FIN-447](https://finassist.youtrack.cloud/issue/FIN-447),
[FIN-211](https://finassist.youtrack.cloud/issue/FIN-211),
[Renovate presets](https://docs.renovatebot.com/config-presets/),
[metadata templates](https://docs.renovatebot.com/templates/),
[closed PR lifecycle](https://docs.renovatebot.com/key-concepts/pull-requests/),
[Dashboard](https://docs.renovatebot.com/key-concepts/dashboard/),
[GitHub merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).
