# Общая упаковка stage-артефактов (FIN-457)

`common/scripts/stage_artifact.py` — общий упаковщик шести сервисных профилей. Локальная
команда и `shared-image.yml` используют код одного полного source SHA, выбранного потребителем.
Образы не публикуются и deployment не выполняется.

```sh
python3 <common>/scripts/stage_artifact.py --root <consumer-root> <service> <image-id> <output>
```

`--root` обязателен; относительный output считается от этого каталога. Из его `go.mod`
берутся версии модулей `fin-assist/*`, а source SHA/repository/event/ref/run/attempt — из
контекста Actions. API: `package(service, image, output, environ, *, root)`; manifest `schema_version: 1`,
имя бинарника и `SHA256SUMS` сохраняются. Существующий output не перезаписывается.
Извлечение допускает только linux/amd64 image с content ID и непустой regular binary;
symlink, directory, отсутствие или пустой файл отклоняются. Временный контейнер удаляется
при success и copy failure; ошибка cleanup не скрывает первоначальную ошибку копирования.

`image_smoke.py` захватывает immutable ID после build и выполняет smoke по этому ID.
`build_images` сохраняет прежний возврат тега; необязательный `image_ids` получает
`image_id` и, для assistant, `fake_image_id` только после успешного smoke всех образов.
CLI пишет эти IDs и прежний `image_tag` в `GITHUB_OUTPUT`. Упаковка получает именно ID,
использованный smoke; последующий перенос тега не подменяет извлекаемый бинарник.

Профили не меняются: user собирает `service:ci`, но извлекает `/user`; bank-bff сохраняет
production refusal, разделение public/operational listeners и проверку shutdown; assistant
по `fake_llm` проверяет и упаковывает отдельный `/fake-llm`. Artifact names остаются
`stage-<service>-<source SHA>-linux-amd64` и `stage-fake-llm-<source SHA>-linux-amd64`,
retention 30 дней, отсутствие файлов — ошибка.

## Переход потребителя

Сохранить `scripts/common_ci.py` побайтно, закрепить новый source SHA согласованно с
workflow `uses`/`revision` и `.finassist-ci.json`. `scripts/stage_artifact.py` можно оставить
тонким совместимым CLI: определить root по своему пути, получить common через существующий
resolver и передать `--root` общему скрипту. Импорт/исполнение verified cache выполняются с
отключённым bytecode. Общие сценарии `scripts/test_stage_artifact.py` заменены
`common/tests/test_stage_artifact.py`; domain smoke и fake-LLM tools остаются сервису.

Общие tests используют реальные временные файлы и подменяют только Docker boundary;
они не подтверждают настоящий Docker smoke/создание Actions artifact. Для приёмки нужны
обязательные source и consumer CI. Локальные tests запускать с process timeout.


## Выкладка и откат на системном stage (FIN-540)

В Actions пяти сервисов доступны **Deploy stage** и **Rollback stage** без параметров.
Оба вызывают `shared-stage.yml` на защищённой `main`. Общий workflow передаёт в `infra`
идентификатор вызова и операцию, ждёт результата и добавляет ссылку на отчёт в summary.
Артефакт выбирает `infra` из существующей приватной публикации CI. Для подключения нужен
`STAGE_DISPATCH_TOKEN` в `stage-request` каждого сервиса. Порядок активации, миграции и
восстановление описаны в [инструкции stage](https://github.com/fin-assist/infra/tree/main/stage#service-actions-fin-540).
