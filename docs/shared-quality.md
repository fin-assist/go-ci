# Общий runner тестов и coverage (FIN-457, первая итерация)

`common/scripts/quality.py` переносит механизм suite inventory, сбора и проверки evidence,
запуска закреплённого `go-test-coverage/v2@v2.23.0` и информационного combined.
Сервис сохраняет `go.mod`, `.testcoverage.yml`, `.finassist-ci.json`, доменные fixtures,
команды build/run/dev/migrate, tidy/vet и пока локальные script/CHANGELOG проверки.
Эта итерация не переносит stage/release tooling и не подключает библиотеки неявно.

## Подключение и совместимость

Вызов `shared-go.yml` получает boolean `quality_runner` со значением по умолчанию `false`.
Только шесть сервисных потребителей первой итерации явно передают `quality_runner: true`:
`go-service-template`, `bank-bff`, `user`, `bank-statement-data`, `analytics`, `assistant`.
Существующие девять библиотек с отсутствующим/false входом продолжают выполнять прежние Make targets.
Контракт потребителя отвергает строковый/числовой flag и opt-in неизвестного module identity/библиотеки.
Формат `.finassist-ci.json` и побайтная копия bootstrap не меняются.

Workflow получает точный SHA `fin-assist/go-ci` из `job.workflow_sha` (или совместимого
`inputs.revision`) без сохранения credentials. Local/CI profile и revision должны совпадать.
При opt-in вызывается общий runner напрямую; legacy-библиотеки используют свои Make targets.
PR-код не получает секретов/write token. Итоговый required `go`, summary и пути файлов coverage
сохраняются. Внешние настройки/защиты эта миграция не меняет.

CLI всегда получает явный корень потребителя:

```sh
python3 "$common/scripts/quality.py" --root "$consumer" test-names
python3 "$common/scripts/quality.py" --root "$consumer" test --test-timeout 8m
python3 "$common/scripts/quality.py" --root "$consumer" collect unit
python3 "$common/scripts/quality.py" --root "$consumer" check unit
python3 "$common/scripts/quality.py" --root "$consumer" combined
```

`collect` и `check` принимают только `unit`, `integration`, `api`. `test` запускает прежний общий
Go `-race -count=1` прогон, а не script/release checks; локальный `user.TEST_TIMEOUT` передаётся ему явно.
Разрешён положительный Go duration, например `8m`, `90s`, `1m30s`, `500ms`; нулевой неограниченный
таймаут и shell-текст отвергаются. Inventory и collectors всегда сохраняют `-race` и `-timeout=8m`.
Inventory и collectors ограничены также процессным timeout 45 минут, включая компиляцию:
это прежний бюджет всего Go CI job, а не timeout одного тестового бинарника.
`test` использует максимум из 45 минут и своего положительного timeout +120s.
Analyzer сохраняет отдельный timeout 600s. Timeout и отмена завершают всю группу процессов,
включая потомков.

## Параллельные suites (FIN-515)

`shared-go.yml` выполняет unit, integration и API в отдельных jobs одной матрицы
с `fail-fast: false`. Каждый job получает собственный GitHub-hosted runner, рабочий каталог,
Go-кэш и PostgreSQL testcontainers. Соседняя ошибка не отменяет остальные suites. Tidy, vet,
script tests и CHANGELOG проверяются отдельным параллельным job `checks`.
Все jobs разрешают один и тот же фактический SHA reusable workflow; на PR восстанавливают
подготовленные приватные входы для этого SHA и head PR. Состав inventory, инструментируемых
пакетов, `-race`, `-count=1`, пороги и точное N/A-исключение шаблона не меняются.

Каждый suite всегда пытается сохранить имеющиеся файлы `.out`, `.jsonl`, `.coverage.txt` и
`.report.txt` в отдельный `coverage-suite-<run_id>-<run_attempt>-<suite>`. Финальный `check`
загружает только артефакты текущего run и attempt; переносит из них только известные имена
файлов coverage, удаляя прежние файлы checkout. Исходники и исполняемые файлы из этих артефактов
не восстанавливаются. Объединённый отчёт строит прежний analyzer после проверки всех трёх
профилей и JSONL. При низком пороге отдельного suite combined остаётся информационным и
не может сделать итог зелёным. Отсутствующий, пустой или непрошедший suite блокирует `go`.
N/A combined возможен только для трёх валидных N/A-профилей исходного библиотечного шаблона.

Финальный `check` запускается с `always()` и требует успеха `checks`, всех matrix jobs и
восстановления трёх профилей; failure, skipped и cancelled не принимаются. `checks` передаёт
номер своей attempt через job output; успешный результат старой попытки также отклоняется. Сводка и общий
artifact `coverage` сохраняют прежние имена файлов. При повторе он заменяет прежний сводный
artifact, а отдельные suite artifacts прежних попыток сохраняются для диагностики.
Неуспешный upload/download также блокирует reusable workflow и consumer `go`.

После частичного повтора нельзя объединять новый профиль с профилями предыдущей попытки.
Если в текущем attempt отсутствует хотя бы один suite artifact, используйте **Re-run all jobs**.
[Правила GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#behavior-of-reusable-workflows-when-re-running-jobs)
различают полный повтор и повтор упавших/отдельных jobs: полный заново разрешает branch ref,
частичный сохраняет исходный SHA reusable workflow. FIN-516 также проверила полный повтор
на неизменных коммитах `analytics` и `go-worker`; ссылки на обе попытки — в [итоге задачи](https://finassist.youtrack.cloud/issue/FIN-516).

Исключение по решению владельца — ровно `fin-assist/bank-bff`: существующий `checks` job
выполняет unit, integration и API последовательно, после общих проверок. Отдельные matrix
и aggregate jobs пропускаются. Каждый collector и total80 остаётся обязательным; ошибка
одного уровня не мешает попытке выполнить следующие. Combined строится только после
успешного сбора всех трёх suites и не заменяет независимые пороги. Прежние файлы coverage
удаляются после checkout; summary и общий artifact `coverage` сохраняются даже при ошибке.
Все suites живут в одном runner и повторяются вместе с этим job, поэтому для bank-bff
нет переноса suite evidence между jobs или смешивания разных attempts. Caller остаётся
на `@main`, private inputs привязаны к фактическому SHA этого reusable workflow.

При исходном rollout FIN-515 изменение автоматически получили все 15 Go-потребителей,
включая девять legacy-библиотек. До merge проверяются исходный `go-library-template` (N/A) и библиотека с
`internal/pgtest` на временных ветках с полным SHA source PR: независимые Make targets и
`cover-combined` должны проходить в разных runner’ах. Приёмка пяти сервисов после merge
сравнивает прогоны на одинаковых коммитах: полную длительность CI, сумму времени runner’ов,
состав тестов, отдельные coverage и публикацию по consumer checks `go`, `lint`, `image`.

## Тонкие совместимые Make targets

Уберите локальные `GO_TEST_COVERAGE_VERSION`/`COVERAGE` и замените реализацию targets ниже.
В `user` сохраните `TEST_TIMEOUT ?= 8m`: оно не обязано стоять рядом с удаляемыми строками.

```make
# test сохраняет tidy/vet, затем вызывает общий Go test и локальные script/CHANGELOG проверки.
# В user используйте --test-timeout "$(TEST_TIMEOUT)" вместо литерала "8m".
# @common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" test --test-timeout "8m"

test-names:
	@common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" test-names

collect-unit collect-integration collect-api:
	@common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" collect "$(@:collect-%=%)"

check-unit check-integration check-api:
	@common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" check "$(@:check-%=%)"

cover-unit: collect-unit
	$(MAKE) check-unit
cover-integration: collect-integration
	$(MAKE) check-integration
cover-api: collect-api
	$(MAKE) check-api

cover-combined:
	@common=$$(python3 scripts/common_ci.py) && python3 -B "$$common/scripts/quality.py" --root "$(CURDIR)" combined

cover: collect-unit collect-integration collect-api
	@status=0; for level in unit integration api; do $(MAKE) check-$$level || status=1; done; \
	$(MAKE) cover-combined || status=1; exit "$$status"
```

Local bootstrap получает выбранный полный SHA, проверяет его байты и печатает путь `common/`.
Потребителю не нужен ещё один download/execute wrapper. После замены удаляются только дубли
`check-suite.sh`, `test_suite_inputs.py`, `test_suite_selection.py`, `test_coverage_policy.py`:
их положительные/отрицательные сценарии перенесены в `common/tests/test_quality.py`.
После FIN-516 потребители следуют `go-ci/main`: новые source-правки приходят после review/merge
по команде владельца без обновления pins. Откат общего изменения — revert в `go-ci`;
старый интерфейс с согласованными `revision` и `uses` остаётся совместимым.

## Сохранённые гарантии

- Inventory использует `go test -race -list` и отклоняет нераспределённые Test/Fuzz, включая race-only.
  Service Example запрещён; library Example исполняется в API suite.
- Collectors используют `-race -count=1 -timeout=8m` и `CI=true`, выполняют `./...` и удаляют stale evidence.
  Service знаменатель — `./...`, без исключений. Library исключает только точный
  `<module>/internal/pgtest` из instrumentation; вложенные/соседние пакеты остаются, тесты всех пакетов выполняются.
- JSONL должен содержать реально прошедший Test/Fuzz/Example, не только package pass. Test skip, любой fail,
  пустые/повреждённые evidence и профили, header-only/zero-statement profiles отклоняются.
- N/A допускается только при library profile, точном `mode: atomic\n`, успешном suite и identity исходного
  `fin-assist/go-library-template`: `GITHUB_REPOSITORY` в Actions или один из исходных точных Git origins локально.
  Похожее имя/module path/fork/new library не дают N/A. Mixed applicability не даёт combined.
- Каждый check использует локальную `.testcoverage.yml` и официальный закреплённый анализатор; своего
  калькулятора процентов нет. Его согласованное округление: 1599/2000 проходит 80%, 1598/2000 не проходит.
  Combined передаёт три профиля нативному union и не заменяет ни один независимый gate.
- Compiler inventory/collect сохраняет окружение setup-go и `go.mod`; только анализатор получает
  `GOTOOLCHAIN=auto`. Локальные instrumented profiles/reports и CI artifacts сохраняют прежние имена.

## CHANGELOG и thin delegates после миграции (FIN-457)

При `quality_runner: true` `shared-go.yml` проверяет CHANGELOG parser из того же
`.finassist-ci/<resolved SHA>`, передавая явный consumer root. При false сохраняется
`python3 scripts/changelog.py check`: существующие сервисные и библиотечные callers не
переводятся неявно. Оба пути передают ненулевой exit как failure.

Для шести явно подключённых сервисов общий consumer contract дополнительно связывает
release caller `uses`/`revision` с manifest и проверяет поведение локальных changelog/artifact
wrappers. Изолированный subprocess исполняет настоящий wrapper с fake resolver и shared
probe, из чужого cwd: проверяются явный consumer root, передача аргументов без потерь,
правильный shared script и отключённый bytecode. Настоящий bootstrap, network и новые
инструменты этим probe не запускаются. Legacy false и библиотечные fixtures сохраняются.
