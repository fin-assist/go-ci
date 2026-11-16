# CI при смене видимости сервиса (FIN-517)

Новый путь выполняет проверки сервисов в существующем приватном `fin-assist/infra`.
Репозиторий сервиса передаёт идентификатор своего запуска и получает результаты;
в его runner не исполняется код PR и не попадают приватные зависимости, бинарники
или архивы образов. Это позволяет сохранить приватность сборок при публичном
исходном репозитории. Наличие этого кода не означает, что потребители уже переведены.
Первый caller — `go-service-template`. FIN-524 расширяет consumer contract на
`assistant`, `user`, `analytics`, `bank-bff` и `bank-statement-data`; legacy callers
остаются допустимы до их отдельной миграции и приёмки. Библиотеки не получают relay-профиль.

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
| `mode` | `service` по умолчанию; `qase90` только у bank-bff |
| `qase90_revisions` | Пусто у service; JSON с ровно двумя полными SHA `bsd` и `analytics` у Qase 90 |

Приватный workflow самостоятельно проверяет эти значения через GitHub API,
историю Git и фактические `referenced_workflows` точной попытки источника. Payload
dispatch и имя запуска сами по себе не являются доказательством происхождения.
Имя запуска — `Service CI <source_repository> <source_run_id>/<source_run_attempt>`.

Relay ищет ровно один такой запуск с ожидаемыми repository, workflow path,
`workflow_dispatch`, `main` и первой попыткой. Он получает jobs через endpoint
`/runs/<id>/attempts/1/jobs` и требует ровно один завершённый job с каждым именем
`go`, `lint`, `image`, `migrations`, с тем же run ID и SHA приватного workflow.
Для PR все четыре результата должны быть `success`, кроме stateless bank-bff:
его `migrations` всегда `skipped`. На push `migrations` также должен быть `skipped`. Поле `accepted` становится `true` только при этих результатах
и успешном завершении всего приватного CI. Relay также возвращает
`source_run_attempt`; исходные wrappers сверяют его с `github.run_attempt`, чтобы
частичный повтор не принял сохранённые outputs прежней попытки. Wrappers продолжают
создавать обязательные проверки с прежними именами от GitHub Actions.
У bank-bff отдельный исходный workflow `Qase 90 cross-service` (`qase90.yml`)
вызывает тот же relay с `mode: qase90` и двумя закреплёнными соседними ревизиями.
Репозитории соседей фиксированы в infra; caller не может выбрать другой репозиторий.
Broker проверяет их происхождение и подготавливает общий cache и bundles приватно.
Имя приватного запуска — `Qase 90 CI fin-assist/bank-bff <run>/<attempt>`; relay
проверяет ровно один job `qase90` и возвращает одноимённый output. Исходный facade
сохраняет имя `Qase 90 cross-service` и те же accepted/current-attempt guards.
Qase нельзя заменить успешными go/lint/image или опубликовать как production build.

Для всех шести relayed callers consumer contract запрещает старые private-inputs
и production-image workflows и любой source upload-artifact (включая merge).
Из reusable workflows go-ci разрешены только remote relay и GitHub Release на main.
У bank-bff сохраняются отдельный Qase 90 и последовательные unit/integration/API
на одном runner по FIN-515; у остальных сервисов остаются параллельные suites.
Пороги, обязательные contexts и данные политики Renovate не меняются.

Если приватные проверки не прошли, relay сохраняет их результаты и ссылку в
сводке, затем завершает job ошибкой. Поэтому повтор упавших jobs, которым
пользуется Renovate, заново запускает relay и получает результат новой попытки.
Если упал только wrapper после успешного relay, запустите **Re-run all jobs**.
Автоматический retry такого wrapper не получает новой попытки relay: guard остаётся
красным, Renovate сохраняет потраченный retry без merge, закрытия или нового комментария.
Renovate распознаёт relay по `referenced_workflows` source run и принимает только
facades его текущей попытки. Это не позволяет использовать прежний зелёный wrapper
после частичного rerun relay. Штатный failed-jobs retry повторяет relay и все красные
facades, а уже успешный отдельный workflow policy tests остаётся пригодным. Legacy
callers сохраняют прежнюю семантику повторного использования успешных jobs.

Опрос ограничен 75 минутами, сетевой timeout — 30 секундами, объём и число страниц
ответов ограничены. Timeout каждого запроса учитывает оставшееся время общего дедлайна.
Временные ошибки GET (сеть, 5xx, 429 и явно обозначенный rate limit) повторяются
до этого дедлайна с учётом `Retry-After`; ошибки identity и формата прекращают опрос.
API redirect запрещён. Потерянный ответ dispatch не вызывает
повторную отправку: исходный CI заканчивается ошибкой, а для повтора используется
**Re-run all jobs** источника. Это создаёт новую source attempt. Повтор одного
приватного запуска отдельно не принимается исходным relay.

## Доступы

- Секрет `CI_BROKER_TOKEN` caller передаёт из существующего
  `RENOVATE_POLICY_TOKEN`. В этом workflow он используется для dispatch и опроса
  `infra`: Actions write уже включает чтение. Он не передаётся jobs исполнения сервиса.
- Секрет `GO_MODULES_TOKEN` имеет Contents read, Actions read и Pull requests read
  в выбранных исходных репозиториях: `go-ci`, сервисах и их приватных зависимостях.
  Relay использует его только для checkout доверенного `go-ci`; приватный workflow —
  в доверенной подготовке и для чтения provenance исходного CI. Доступ этого токена
  к `infra` не требуется: собственные API infra обслуживает его `GITHUB_TOKEN`, а
  источник stage по-прежнему читает отдельный `STAGE_ARTIFACTS_TOKEN` Environment.
  По решению владельца от 16.11.2026 текущий scope токена сохраняет доступ к `infra`:
  держатели токена могут читать его Actions logs и artifacts, хотя код relay этого
  права не использует. Разделение jobs не ограничивает сам предоставленный scope.
- Доступность organization secrets должна задаваться списком выбранных
  репозиториев либо уже действующей подходящей политикой. Режим «только private»
  не подходит для caller, который меняет видимость.
- Публичному сервису не предоставляется Actions grant на приватные пакеты GHCR.
  GitHub предупреждает, что такой grant может дать доступ fork-репозиториям:
  [права GitHub Packages](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#ensuring-workflow-access-to-your-package).

Оба токена — organization secrets. Автор ветки с write-доступом к репозиторию
может использовать доступные ему secrets в своём workflow; caller не ограничивает
их только relay. Actions write действует на весь `infra`, поэтому каждый его
dispatch workflow самостоятельно проверяет inputs и сохраняет свои Environment
и admission guards. В частности, доступ к stage credentials остаётся за отдельным
main-only workflow и Environment. Существующий RENOVATE-токен также сохраняет
свои прежние права записи; FIN-517 не создаёт для relay отдельный ограниченный PAT.

Два токена внутри одного job **не изолированы друг от друга**. Поэтому relay
не требует расширять GO_MODULES_TOKEN до чтения логов и артефактов `infra`:
используется уже имеющийся доступ dispatch-токена. Jobs с PR-кодом не получают
ни одного из этих токенов. Безопасность этой схемы предполагает доверие
к write-collaborators репозиториев, которым доступны organization secrets.

Удаление `.netrc` или переменной после скачивания зависимостей не делает job
с переданным секретом безопасным для недоверенного PR-кода. Подготовка и исполнение
остаются разными jobs; см. [GitHub Security Lab](https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/).

## Переход потребителя

1. Подготовить и проверить private `infra` workflow, его allowlist, изоляцию jobs
   и доступы. Новый dispatch workflow должен быть в default branch до его вызова.
2. После ревью сделать общий `go-ci` доступным публичным callers. До смены видимости
   проверить историю, артефакты и настройки общего репозитория на приватные данные.
3. В CI сервиса заменить исполнение и передачу private-input artifacts на вызов
   `shared-remote-ci.yml@main`. Wrappers требуют одновременно `accepted == 'true'`,
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

## Порядок FIN-524

Сначала infra принимает прежние восемь полей dispatch как mode service и новый закрытый
mode/Qase payload. Затем обновляется go-ci, затем каждый caller. Обычный service relay
продолжает отправлять ровно прежние восемь полей, поэтому template работает и со старым
broker при другом порядке merge или откате infra. Только Qase отправляет десять полей
и требует обновлённый infra. В relayed caller
не оставляют старый source producer или publisher. Для каждого сервиса нужны PR CI,
реальный Renovate reconcile, приватная публикация из прошедшего CI и изолированный
stage по digest; локальные contract/runtime fixtures не заменяют эту приёмку.
