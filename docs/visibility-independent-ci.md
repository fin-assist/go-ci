# CI при смене видимости сервиса (FIN-517)

Новый путь выполняет проверки сервисов в существующем приватном `fin-assist/infra`.
Репозиторий сервиса передаёт идентификатор своего запуска и получает результаты;
в его runner не исполняется код PR и не попадают приватные зависимости, бинарники
или архивы образов. Это позволяет сохранить приватность сборок при публичном
исходном репозитории. Наличие этого кода не означает, что потребители уже переведены.

## Контракт запуска

`shared-remote-ci.yml@main` исполняет только `common/scripts/ci_broker_relay.py`
из разрешённого GitHub `job.workflow_sha`. Relay принимает push в `main` либо PR
из того же репозитория; внешний fork не получает доверенный job. Проверки самого
сервиса исполняются отдельно, в jobs приватного `infra` с `permissions: {}`,
без секретов подготовки и публикации. Их входы передаются артефактами того же
приватного запуска через штатный runtime-доступ Actions.

Relay один раз вызывает `.github/workflows/service-ci.yml` в `infra/main` и передаёт:

| Поле | Значение |
|---|---|
| `source_repository` | Один из шести разрешённых сервисных репозиториев |
| `source_run_id`, `source_run_attempt` | Точный запуск и попытка CI сервиса |
| `source_sha` | Коммит, который проверяет source CI; для PR — merge commit |
| `source_head_sha`, `source_base_sha` | Исходный head и base PR; у push base пуст |
| `source_pr_number` | Номер PR, пустой у push |
| `go_ci_sha` | Разрешённый SHA reusable workflow из `job.workflow_sha` |

Приватный workflow самостоятельно проверяет эти значения через GitHub API,
историю Git и фактические `referenced_workflows` точной попытки источника. Payload
dispatch и имя запуска сами по себе не являются доказательством происхождения.
Имя запуска — `Service CI <source_repository> <source_run_id>/<source_run_attempt>`.

Relay ищет ровно один такой запуск с ожидаемыми repository, workflow path,
`workflow_dispatch`, `main` и первой попыткой. Он получает jobs через endpoint
`/runs/<id>/attempts/1/jobs` и требует ровно один завершённый job с каждым именем
`go`, `lint`, `image`, `migrations`, с тем же run ID и SHA приватного workflow.
Для PR все четыре результата должны быть `success`; на push `migrations` должен
быть `skipped`. Поле `accepted` становится `true` только при этих результатах
и успешном завершении всего приватного CI. Relay также возвращает
`source_run_attempt`; исходные wrappers сверяют его с `github.run_attempt`, чтобы
частичный повтор не принял сохранённые outputs прежней попытки. Wrappers продолжают
создавать обязательные проверки с прежними именами от GitHub Actions.

Опрос ограничен 75 минутами, HTTP-запросы — 30 секундами, объём и число страниц
ответов ограничены. API redirect запрещён. Потерянный ответ dispatch не вызывает
повторную отправку: исходный CI заканчивается ошибкой, а для повтора используется
**Re-run all jobs** источника. Это создаёт новую source attempt. Повтор одного
приватного запуска отдельно не принимается исходным relay.

## Доступы

- Секрет `CI_BROKER_TOKEN` caller передаёт из существующего
  `RENOVATE_POLICY_TOKEN`. Он нужен только доверенному relay для dispatch в
  `infra`, с правом Actions write. Он не передаётся jobs исполнения сервиса.
- Секрет `GO_MODULES_TOKEN` имеет Contents read, Actions read и Pull requests read
  в выбранных репозиториях, включая `infra`, `go-ci`, сервисы и их приватные
  зависимости. Relay использует его для чтения доверенного общего кода и API;
  приватный workflow — только в доверенной подготовке.
- Доступность organization secrets должна задаваться списком выбранных
  репозиториев либо уже действующей подходящей политикой. Режим «только private»
  не подходит для caller, который меняет видимость.
- Публичному сервису не предоставляется Actions grant на приватные пакеты GHCR.
  GitHub предупреждает, что такой grant может дать доступ fork-репозиториям:
  [права GitHub Packages](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#ensuring-workflow-access-to-your-package).

Удаление `.netrc` или переменной после скачивания зависимостей не делает job
с переданным секретом безопасным для недоверенного PR-кода. Подготовка и исполнение
остаются разными jobs; см. [GitHub Security Lab](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/).

## Переход потребителя

1. Подготовить и проверить private `infra` workflow, его allowlist, изоляцию jobs
   и доступы. Новый dispatch workflow должен быть в default branch до его вызова.
2. После ревью сделать общий `go-ci` доступным публичным callers. До смены видимости
   проверить историю, артефакты и настройки общего репозитория на приватные данные.
3. В CI сервиса заменить исполнение и передачу private-input artifacts на вызов
   `shared-remote-ci.yml@main`. Wrappers требуют одновременно `accepted == 'true'`
   успех соответствующего результата и равенство `source_run_attempt` текущей
   `github.run_attempt`; для push проверку migrations можно не
   создавать, как в прежнем caller. Сохранить другие обязательные checks.
4. Отключить прежний producer private inputs и прежнюю публикацию образа для
   переведённого сервиса. До публикации исходного репозитория убрать его ранее
   сохранённые Actions artifacts с приватными зависимостями, stage-бинарниками
   и полными архивами образов. Смена видимости до этого шага недопустима.
5. Выполнить на одном существующем сервисе полный цикл `private → public → private`
   без изменений workflow и прав между состояниями: PR CI, Renovate reconcile,
   private GHCR publication, изолированная проверка запуска по digest, отказ
   внешнему читателю. Зафиксировать source/broker/publish run ID и attempt, SHA,
   digest и результаты; подготовленная конфигурация не заменяет эту приёмку.

Публикация запускается отдельно после завершения private CI. Она также проверяет
успех точной исходной попытки и её обязательных wrappers, затем использует архив
того самого проверенного образа, без повторной сборки. Это исключает взаимное
ожидание исходного relay и publisher. Пакет связан с постоянно приватным `infra`;
stage читает неизменяемый digest и проверяет provenance. Детали принадлежат
`infra/stage/README.md` и коду доставки, а не этому relay.

Старый путь и его интерфейсы сохраняются для ещё не переведённых потребителей.
Source PR, infra PR и миграция caller проходят отдельные ревью и сливаются только
по команде владельца. Эти действия не разрешают удаление production-ресурсов.
