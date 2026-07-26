<p align="center">
  <img src="assets/banner.png" alt="Hermes Unlocked — Cloak Edition" width="100%">
</p>

# Hermes Unlocked — Cloak Edition 0.19

Сборка Hermes Agent 0.19 со встроенным браузерным backend-ом **CloakBrowser**. Cloak Edition добавляет изолированные анти-детект профили, защищённый маршрут CDP, пул прокси, humanized-ввод, распознавание и решение captcha и отдельную панель управления. Hermes при этом остаётся полноценным агентом: CLI, gateway, навыки и автоматизации работают как обычно.

[![GitHub](https://img.shields.io/badge/GitHub-Cloak%20Edition-181717?logo=github)](https://github.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19)

> Cloak Edition не обещает «невидимость» и не гарантирует обход правил сайтов или детектирования. Это локальная инфраструктура для законной работы с браузером на ресурсах, аккаунтах и сетях, которыми вы вправе управлять.

## Что добавляет Cloak Edition

- **Провайдер `cloak`** для browser-инструментов Hermes: создаёт и запускает профили через CloakBrowser-Manager. Подключается штатным контрактом `BrowserProvider`, без патчей ядра.
- **Изоляция сессий**: у каждой задачи свой lease на профиль. Провайдер проверяет, жив ли профиль, прежде чем переиспользовать привязку.
- **Локальный авторизующий маршрут CDP**: на Linux — Nginx, на Windows — Python bridge. Токен Manager остаётся на локальной стороне и не уходит в URL браузерного инструмента.
- **Пул прокси** с межпроцессной блокировкой: резервация закрепляется за именем профиля и возвращается при остановке.
- **Humanized-ввод**: настоящие события клавиш с человеческим разбросом пауз и кривая траектория мыши вместо телепорта.
- **Captcha**: детектор обходит все вкладки и все фреймы, различает «нет / грузится / загрузилась», решение через CapSolver, 2Captcha или Anti-Captcha.
- **Fail-closed**: при явно выбранном `cloak` Hermes не переключается молча на локальный Chromium.

Рабочая цепочка:

~~~
browser-инструменты Hermes → provider cloak → auth bridge → CloakBrowser-Manager → профиль → CDP
~~~

## Быстрый старт

Bootstrap ставит Hermes, поднимает CloakBrowser-Manager, настраивает защищённый маршрут CDP, включает провайдер `cloak` и завершается ошибкой, если обязательный этап не готов.

**Linux (Debian/Ubuntu) / WSL:**

~~~bash
curl -fsSL https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/main/scripts/bootstrap_cloak.sh | sudo bash
~~~

**Windows PowerShell** (от обычного пользователя):

~~~powershell
$u = "https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/main/scripts/bootstrap_cloak.ps1?cache=$([guid]::NewGuid().ToString('N'))"; iwr $u -UseBasicParsing | iex
~~~

Bootstrap предложит выбор:

- `native` — Hermes работает в Windows, Docker Desktop запускает только Manager и CDP bridge;
- `compose` — отдельный Docker Compose-проект: `hermes` (шлюз и панель в одном контейнере), `bridge` и `manager`, со своими volumes.

Первая сборка Compose занимает 15–45 минут. Все порты публикуются только на `127.0.0.1`.

Токен Manager генерируется в `.cloak-compose.env` (Compose) или в `%USERPROFILE%\.hermes\cloak\manager.env` (native). Оба файла остаются на машине, bootstrap секреты не печатает.

Остановить стек:

~~~powershell
docker compose --project-name hermes-cloak-edition --env-file .cloak-compose.env -f docker-compose.cloak.yml down
~~~

### Ручной режим

Если Hermes уже установлен, разворачивается только Cloak-стек:

~~~bash
sudo bash scripts/install_cloak.sh --configure-provider --strict
~~~

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_cloak.ps1 -ConfigureProvider -Strict
~~~

В режиме `strict` адрес CDP bridge публикуется только после HTTP- и WebSocket-проверки на временном профиле. Отдельный Chromium для Cloak не скачивается: провайдер подключается по CDP к браузеру, которым управляет Manager.

## Как этим пользоваться

Скажите агенту, что нужно сделать — он вызовет инструменты сам. Ниже то, что стоит знать оператору.

### Профили

Профиль — это отдельный браузер со своим отпечатком, прокси и cookies. Один профиль = одна личность.

Имя обязательно и должно быть устойчивым (`shop-de`, `reg-figma-001`): по нему профиль находится позже и по нему же закрепляется прокси из пула. Переключение между профилями внутри задачи включается настройкой `CLOAK_ALLOW_PROFILE_SWITCH=1`.

### Прокси

Два равноправных пути, выбор за вами:

- прислать прокси в чат и попросить посадить профиль на него — уйдёт как есть;
- загрузить пул (`host:port`, `host:port:user:pass`, `user:pass@host:port`, `scheme://...`) и брать из него.

Явный прокси всегда сильнее пула. `socks4` не поддерживается — CloakBrowser его не умеет.

### Captcha

Навигация сама сообщает о challenge, отдельно просить проверку не нужно. Ответ детектора различает три состояния:

| Состояние | Значение |
| --- | --- |
| `kind: null`, `pending: false` | captcha нет |
| `pending: true` | заявлена, но ещё не отрисована — нужно подождать |
| `kind` + `site_key` | загрузилась, можно решать |

Различие важное: сразу после отправки формы страница часто пишет «captcha loading», и дальше возможны оба исхода — виджет дорисуется либо исчезнет сам. Детектор смотрит **все вкладки** профиля: формы регистрации нередко открываются в новой.

Ключи солверов задаются в панели `/cloak` или в `manager.env`.

## Настройки

`manager.env` перечитывается на лету — переключатели применяются без перезапуска gateway.

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `CLOAK_MANAGER_URL` | адрес CloakBrowser-Manager | `http://127.0.0.1:8080` |
| `CLOAK_AUTH_TOKEN` | Bearer-токен Manager | — |
| `CLOAK_CDP_PROXY_BASE` | адрес локального CDP bridge | — |
| `CLOAK_ALLOWED_HOSTS` | дополнительные разрешённые хосты Manager | только localhost |
| `CLOAK_USE_PROXY_POOL` | брать прокси из пула автоматически | `0` |
| `CLOAK_REQUIRE_PROXY` | не создавать профиль без прокси | `0` |
| `CLOAK_ALLOW_PROFILE_SWITCH` | разрешить задаче менять профиль | `0` |
| `CLOAK_MISTYPE_CHANCE` | частота опечаток humanized-ввода, `0` отключает | пресет (`0.02`) |
| `CLOAK_AUTODETECT_CAPTCHA` | сообщать о captcha в результате навигации | `1` |
| `CLOAK_HUMAN_PRESET` | `default` или `careful` | `default` |
| `CLOAK_IDLE_TIMEOUT_MIN` | авто-закрытие простаивающих профилей, `0` отключает | `0` |
| `CLOAK_ENABLE_GMAIL_FACTORY` | включить набор `gmail_factory_*` | `0` |
| `CAPTCHA_PROVIDER` | `auto`, `capsolver`, `2captcha`, `anticaptcha` | `auto` |
| `CAPSOLVER_API_KEY` · `TWO_CAPTCHA_API_KEY` · `ANTICAPTCHA_API_KEY` | ключи солверов | — |

## Инструменты

| Инструмент | Что делает |
| --- | --- |
| `cloak_list_profiles` | список профилей: имя, статус, прокси (маскированный) |
| `cloak_create_profile` | создать профиль; `use_pool` или явный `proxy` |
| `cloak_launch` | запустить профиль по имени или UUID |
| `cloak_set_active` | найти-или-создать и запустить одним вызовом |
| `cloak_stop` | остановить профиль, вернуть прокси в пул |
| `cloak_proxy_pool` | загрузить, посмотреть или очистить пул |
| `cloak_detect_captcha` | найти challenge во всех вкладках; `wait_ms` ждёт исхода |
| `cloak_solve_captcha` | решить через подключённого провайдера |

Плюс 7 переопределений `browser_*` с humanized-вводом. Поле адресуется ref-ом из снапшота (`ref="e5"`) или CSS-селектором.

## Компоненты

| Компонент | Условие |
| --- | --- |
| `plugins/browser/cloak/provider.py` | всегда |
| `plugins/browser/cloak/_impl/` | нужны `cloakbrowser` и Playwright |
| `scripts/install_cloak.*` | Debian/Ubuntu/WSL или Windows |
| `/cloak` | запущен веб-сервер Hermes |
| `skills/cloak-*` | по запросу пользователя |

Отсутствие тяжёлых зависимостей не ломает discovery: базовый провайдер остаётся доступен, богатый набор подключается best-effort.

## Структура

~~~
plugins/browser/cloak/         провайдер, профили, пул прокси, humanize, captcha
scripts/install_cloak.*        установка Linux/Windows
scripts/cloak/                 CDP bridge, HTTP/WS probes, readiness
hermes_cli/cloak_dashboard.py  панель /cloak
skills/cloak-profiles/         работа в профиле целиком
skills/cloak-proxy-pool/       хранение и выдача прокси
tests/cloak/                   регрессионные тесты интеграции
~~~

## Проверка

~~~bash
python -m pytest tests/cloak -q
~~~

Синтаксис установщика без запуска Docker:

~~~bash
bash -n scripts/install_cloak.sh
~~~

Полная проверка требует работающего Manager, Docker и разрешённого тестового окружения.

## Известные ограничения

- Панель `/cloak` умеет показать токен Manager по явному запросу (`?reveal=1`). Панель закрыта basic-auth и слушает только localhost, но это не хранилище секретов — не публикуйте порт наружу.
- CDP bridge подставляет токен Manager в любой запрос, который до него дошёл, и не ограничивает путь. Держите его на localhost.
- Скриншот целой страницы иногда не снимается на страницах с кросс-доменными iframe (challenge-виджеты) — ограничение Chromium, не интеграции.
- `plugins/browser/cloak/vendor/gmail_factory/` — форк стороннего проекта под проприетарной лицензией (см. `NOTICE` в этом каталоге). Набор выключен по умолчанию и включается только через `CLOAK_ENABLE_GMAIL_FACTORY=1`.

## Документация и поддержка

- [Issues этого форка](https://github.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/issues)
- [Исходный Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [CloakBrowser-Manager](https://github.com/CloakHQ/CloakBrowser-Manager)

## Лицензия

Код Hermes и изменения этой редакции — MIT, см. [LICENSE](LICENSE). Исключение — вендорённый каталог `plugins/browser/cloak/vendor/gmail_factory/`, который распространяется на условиях исходного проекта; его лицензия описана в `NOTICE` рядом с кодом и под MIT не подпадает.

Hermes Unlocked — Cloak Edition основан на Hermes Agent; изменения этой редакции сосредоточены на интеграции Cloak и её безопасной эксплуатации.
