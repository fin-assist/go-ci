# Общий runner тестов и coverage (FIN-457, первая итерация)

`common/scripts/quality.py` переносит механизм suite inventory, сбора и проверки evidence,
запуска закреплённого `go-test-coverage/v2@v2.20.1` и информационного combined.
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

Для opt-in workflow делает checkout фиксированного `fin-assist/go-ci` по полному `inputs.revision`,
без сохранения credentials, и вызывает общий runner напрямую. Local/CI profile и revision должны
совпадать. PR-код не получает секретов/write token. Имена steps, collect/check outcomes, итоговый required
`go`, summary и пути coverage artifacts сохраняются. Внешние настройки/защиты эта миграция не меняет.

CLI всегда получает явный корень потребителя:

```sh
python3 "$common/scripts/quality.py" --root "$consumer" test-names
python3 "$common/scripts/quality.py" --root "$consumer" test --test-timeout 8m
python3 "$common/scripts/quality.py" --root "$consumer" collect unit
python3 "$common/scripts/quality.py" --root "$consumer" check unit
python3 "$common/scripts/quality.py" --root "$consumer" combined
```

`collect` и `check` принимают только `unit`, `integration`, `api`. `test` запускает прежний общий
Go race/count1 прогон, а не script/release checks; локальный `user.TEST_TIMEOUT` передаётся ему явно.
Разрешён положительный Go duration, например `8m`, `90s`, `1m30s`, `500ms`; нулевой неограниченный
таймаут и shell-текст отвергаются. Inventory и collectors всегда сохраняют race и timeout8m.
Inventory и collectors ограничены также процессным timeout 45 минут, включая компиляцию:
это прежний бюджет всего Go CI job, а не timeout одного тестового бинарника.
`test` использует максимум из 45 минут и своего положительного timeout +120s.
Analyzer сохраняет отдельный timeout 600s. Timeout и отмена завершают всю группу процессов,
включая потомков.

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
Обновляйте source pin, `uses` и `with.revision` вместе после review/merge source по команде владельца.
Откат возвращает согласованные pins и Make targets; bootstrap остаётся той же проверенной копией.

## Сохранённые гарантии

- Inventory использует `go test -race -list` и отклоняет нераспределённые Test/Fuzz, включая race-only.
  Service Example запрещён; library Example исполняется в API suite.
- Collectors используют race/count1/timeout8m и `CI=true`, выполняют `./...` и удаляют stale evidence.
  Service знаменатель — `./...`, без исключений. Library исключает только точный
  `<module>/internal/pgtest` из instrumentation; вложенные/соседние пакеты остаются, тесты всех пакетов выполняются.
- JSONL должен содержать реально прошедший Test/Fuzz/Example, не только package pass. Test skip, любой fail,
  пустые/повреждённые evidence и профили, header-only/zero-statement profiles отклоняются.
- N/A допускается только при library profile, точном `mode: atomic\n`, успешном suite и identity исходного
  `fin-assist/go-library-template`: `GITHUB_REPOSITORY` в Actions или один из исходных точных Git origins локально.
  Похожее имя/module path/fork/new library не дают N/A. Mixed applicability не даёт combined.
- Каждый check использует локальную `.testcoverage.yml` и официальный закреплённый анализатор; своего
  калькулятора процентов нет. Его согласованное округление1599/2000 проходит80%,1598/2000 не проходит.
  Combined передаёт три профиля нативному union и не заменяет ни один независимый gate.
- Compiler inventory/collect сохраняет окружение setup-go и `go.mod`; только анализатор получает
  `GOTOOLCHAIN=auto`. Локальные instrumented profiles/reports и CI artifacts сохраняют прежние имена.

## CHANGELOG и thin delegates после миграции (FIN-457)

При `quality_runner: true` `shared-go.yml` проверяет CHANGELOG parser из того же
`.finassist-ci/quality-source`, передавая явный consumer root. При false сохраняется
`python3 scripts/changelog.py check`: существующие сервисные и библиотечные callers не
переводятся неявно. Оба пути передают ненулевой exit как failure.

Для шести явно подключённых сервисов общий consumer contract дополнительно связывает
release caller `uses`/`revision` с manifest и проверяет поведение локальных changelog/artifact
wrappers. Изолированный subprocess исполняет настоящий wrapper с fake resolver и shared
probe, из чужого cwd: проверяются явный consumer root, передача аргументов без потерь,
правильный shared script и отключённый bytecode. Настоящий bootstrap, network и новые
инструменты этим probe не запускаются. Legacy false и библиотечные fixtures сохраняются.
