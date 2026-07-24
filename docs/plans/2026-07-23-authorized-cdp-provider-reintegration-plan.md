# План повторной интеграции безопасного CDP-провайдера в Hermes 0.19

**Статус:** planning only — этот файл не является реализацией.  
**Дата:** 2026-07-23  
**Целевая ревизия:** `48486c7cea058071f4d370907a9654ecabc38f2d` (`Hermes Agent 0.19 CLOAK/hermes-agent-main`).  
**Старые планы:** не изменяются.

## 1. Решение и границы

Переносить следует только надёжный слой подключаемого, авторизованного CDP-провайдера для браузера, которым оператор вправе управлять. Он должен использовать штатный контракт Hermes `BrowserProvider`, а не legacy monkeypatch.


## 2. Проверенные факты

| Область | Наблюдение | Последствие для переноса |
| --- | --- | --- |
| Текущая интеграция браузеров | Hermes 0.19 уже имеет `agent/browser_provider.py`, `agent/browser_registry.py`, регистрацию через `hermes_cli/plugins.py` и выбор через `hermes_cli/tools_config.py`. | Новый провайдер можно подключить нативно, без патча `tools/browser_tool.py`. |
| Старый провайдер | `hermes-unlock-cloak-edition/plugins/browser/cloak/provider.py` наследует `BrowserProvider`, но записывает `BROWSER_CDP_URL` и ID профиля в process-global `os.environ`. | Параллельные задачи могут получить чужой CDP-сеанс или закрыть чужой профиль. Нужен изолированный state по session/task. |
| Старый CDP proxy | `scripts/cloak/nginx/cloak-upgrade-map.conf` задаёт map-переменную с именем `$http_connection`, а template использует её для WebSocket upgrade. | Конфигурация конфликтна/хрупка и должна быть заменена проверенной конфигурацией с отдельной именованной переменной и контрактными тестами. |
| Readiness proxy | `scripts/install_cloak.sh` записывает `CLOAK_CDP_PROXY_BASE` до готовности nginx; `nginx -t` и запуск сервиса проверяются недостаточно строго. Provider затем безусловно переписывает WebSocket URL на proxy. | Ошибка запуска proxy превращается в непрозрачный отказ CDP. Публиковать proxy endpoint разрешается только после настоящего HTTP+WebSocket smoke-test. |
| Маршрутизация при ошибке | В `tools/browser_tool.py` присутствует legacy fallback к локальному Chromium после ошибки cloud provider. | Для явно выбранного провайдера надо зафиксировать fail-closed поведение и тест: ошибка провайдера не должна незаметно открыть иной браузер. |
| Секреты | Старый install script подставляет токен в конфигурацию proxy, а прежняя dashboard-панель предусматривала раскрытие токена. | Нужны минимальные права на файлы, redaction логов, отсутствие endpoint-а раскрытия секрета и ротация без правки кода. |
| Платформы | Старое развёртывание рассчитано на Linux (`bash`, `apt`, `systemctl`, nginx, Docker), а рабочая среда проекта — Windows. | Сначала определить поддерживаемые режимы Windows/Linux; не добавлять псевдоподдержку через Linux-only script. |

## 3. Целевой результат

После выполнения реализации должно быть возможно выбрать **явно сконфигурированный** provider для разрешённого локального/операторского CDP-сервиса. Hermes создаёт, проверяет и закрывает только связанный с конкретной задачей сеанс; при ошибке выдаёт диагностируемую ошибку и не переключается молча на другой browser backend.

Минимальные свойства результата:

1. Провайдер является небольшим bundle-плагином и соответствует текущему `BrowserProvider` API.
2. Manager/CDP endpoint и credential берутся из конфигурации/secret store, не из исходного кода.
3. CDP URL валидируется и допускается только из явно разрешённого списка хостов/схем.
4. Все manager/profile операции имеют ограниченные timeout, typed errors, correlation/session ID и безопасное логирование.
5. Любой reverse proxy является локальным, opt-in, проходит readiness и WebSocket contract test до использования.
6. UI показывает только состояние подключения и masked secret metadata; не значение токена.
7. Нормальный путь и rollback одинаково работают без удаления пользовательских профилей.

## 4. План работ

| Шаг | Действие | Исполнитель / оценка | Проверка и exit condition | Rollback |
| --- | --- | --- | --- | --- |
| 0 | Зафиксировать рамки: есть разрешение на управление конкретным CDP/manager; security guardrails Hermes остаются включены. | Владелец, 10 мин | Письменно определены разрешённые manager hosts, OS и модель credential. | Не начинать реализацию. |
| 1 | Снять baseline целевой ревизии: чистый worktree, версии Python/Node, текущие browser tests и пути plugin discovery. | Разработчик, 20–30 мин, low resource | Сохранён read-only inventory; подтверждено, что `BrowserProvider`/registry/picker используют текущие интерфейсы. | Удалить только временный worktree. |
| 2 | Создать отдельный минимальный provider-plugin без vendored `_impl`, monkeypatch и unsafe tool overrides. Реализовать только lifecycle: availability, create, close, emergency cleanup, setup schema. | Разработчик, 0.5–1 день | Unit-тесты подтверждают регистрацию, explicit selection и корректную диагностику недоступного manager. | Удалить новый plugin/catalog entry; конфиг вернуть на прежний provider. |
| 3 | Спроектировать session-scoped state: один task/session → один lease/profile ID/CDP URL; никаких записей в process-global `BROWSER_CDP_URL`. Добавить cleanup при success, cancellation и process shutdown. | Разработчик, 0.5–1 день | Concurrency test с двумя сессиями доказывает отсутствие пересечения ID, URL и cleanup. | Отключить provider feature flag, не удаляя профили. |
| 4 | Реализовать строгий manager/CDP contract: проверка запуска профиля, polling готовности с ограничением времени, явный error taxonomy, allowlist endpoint-ов и redacted diagnostics. | Разработчик, 0.5–1 день | Mock manager покрывает `create`, `already running`, delayed readiness, 401/403, 404, 409, timeout, malformed CDP JSON. | Feature flag off; сохраняется читаемый диагноз. |
| 5 | Если CDP-клиент действительно не поддерживает нужную auth-модель, выделить локальный bridge как отдельный компонент. Не публиковать endpoint до успешных HTTP и WebSocket probe; не хранить токен в world-readable файле; не подменять URL при failed probe. | Разработчик + security review, 1 день | Integration-test проверяет normal upgrade, отсутствие upgrade, bad credential, restart и закрытие туннеля. | Не задавать bridge URL; provider соединяется только с прямым разрешённым endpoint-ом либо завершается ошибкой. |
| 6 | Исправить selection semantics: explicit provider должен работать fail-closed. Local fallback разрешён только когда явно выбран `local`/`auto`, а не после ошибки явно указанного provider. | Разработчик, 0.5 дня | Регрессионный тест наблюдает, что ошибка explicit provider не запускает local Chromium. | Вернуть прежнюю feature-flagged policy только после согласованного изменения UX. |
| 7 | Добавить configuration/secret policy: masked status, no reveal API, redaction headers/query strings, файл с минимальными правами, documented rotation path. | Разработчик + security review, 0.5 дня | Лог/HTTP/UI tests не находят test token; rotation test не требует rebuild. | Отозвать токен и выключить provider. |
| 8 | Определить платформенную поставку. Для Windows — нативный поддерживаемый путь и smoke-test; для Linux — idempotent installer с явными prereq checks. Скрипты `.sh`, `.ps1` и `.cmd` не должны обещать одинаковое, если backend различается. | Разработчик, 1 день | Clean-machine dry run для каждой заявленной ОС; документированы prerequisites и cleanup. | Uninstall только созданных сервисов/конфигов; профили не удалять по умолчанию. |
| 9 | Прогнать verification ladder: unit → mock manager/CDP → isolated local browser smoke → Windows/Linux install smoke → regression browser suite. Тяжёлые browser builds запускать по отдельному согласию с оценкой ресурсов. | QA/разработчик, 0.5–1 день | Все заявленные тесты green; известные ограничения указаны в release note. | Disable feature flag, восстановить предыдущий config и проверить, что agent запускается. |
| 10 | Выпустить small, reversible change: отдельный commit, changelog, migration note и one-command rollback. Не смешивать с unlock-изменениями, UI-переписыванием или vendor blobs. | Владелец, 30 мин | Clean install + explicit provider smoke подтверждены. | Revert отдельного commit-а или feature flag off. |

## 5. Обязательные тесты CDP bridge

1. **WebSocket upgrade contract:** корректный upgrade, обычный HTTP request без upgrade, закрытие upstream и таймаут.
2. **Authentication boundary:** valid credential, missing credential и invalid credential; секрет не попадает в URL/логи/ошибки.
3. **Readiness:** manager запускается позже provider, browser endpoint появляется с задержкой, bridge/service перезапускается.
4. **URL safety:** относительный/абсолютный CDP URL, неподдерживаемая схема, неразрешённый host, malformed JSON.
5. **Concurrency:** два одновременных task ID, независимые lease и cleanup; один task не может остановить другой.
6. **Fallback policy:** explicit provider failure не создаёт local session.
7. **Platform:** тесты пути конфигурации и отсутствие Linux-only assumptions на Windows.

## 6. Риски, которые надо закрыть до merge

- Контракт manager API может отличаться от старого `/api/profiles` интерфейса — сначала подтвердить контракт mock/test fixture, затем кодировать client.
- Нельзя доверять URL, который вернул manager, без validation и redaction.
- `latest`-образ container-а не годится для воспроизводимого релиза: закрепить образ/версию и записать процедуру обновления.
- Dashboard не является secret manager: status-only UI, никаких `reveal` endpoint-ов.
- Не запускать browser build, Docker pull или Playwright download без оценки времени/CPU/RAM/disk и отдельного разрешения, если нагрузка заметная.

## 7. Критерии готовности

- Плагин грузится через штатный registry/picker Hermes 0.19.
- При explicit выборе provider-а отсутствует silent fallback на локальный browser.
- CDP lifecycle безопасен при параллельных задачах, отмене и рестарте.
- Bridge, если нужен, локальный, opt-in и реально проверенный до включения.
- Тестовые токены не видны в source, log, UI, error text или artifact.
- Все стандартные защиты Hermes сохранены.
- Старые планы и код старого fork-а не менялись.

## 8. Следующий build gate

Перед реализацией требуется отдельное подтверждение владельца на ограниченный scope из раздела 1, выбор поддерживаемых ОС и подтверждение, что CDP manager/браузеры используются с разрешения владельца. После этого можно подготовить отдельную ветку и начать шаг 1.
