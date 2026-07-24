<p align="center">
  <img src="assets/banner.png" alt="Hermes Unlocked — Cloak Edition" width="100%">
</p>

# Hermes Unlocked — Cloak Edition 0.19

Сборка Hermes Agent 0.19 со встроенным браузерным backend-ом **CloakBrowser**. Cloak Edition добавляет изолированные профили, защищённый маршрут CDP, пул прокси, humanized-ввод, маршрутизацию captcha и отдельную панель управления — при этом Hermes остаётся полноценным агентом для CLI, gateway, навыков и автоматизаций.

[![Лицензия: MIT](https://img.shields.io/badge/Лицензия-MIT-green.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-Cloak%20Edition-181717?logo=github)](https://github.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19)

> Cloak Edition не обещает «невидимость» или обход правил сайтов. Это локальная инфраструктура для законной работы с профилями и браузером на ресурсах, аккаунтах и сетях, для которых у вас есть разрешение.

## Что именно добавляет Cloak Edition

- Провайдер **cloak** для browser-инструментов Hermes: он создаёт и запускает профили через CloakBrowser-Manager.
- Изолированные CDP-сессии с task-scoped lease: параллельные задачи не должны подменять активный профиль друг друга.
- Локальный авторизующий CDP-маршрут: на Linux используется Nginx proxy, на Windows — Python bridge. Токен Manager остаётся на локальной стороне и не передаётся браузерному инструменту в URL.
- Пул прокси, humanized mouse/keyboard input, captcha router и idle reaper — когда установлены дополнительные зависимости.
- Веб-панель **/cloak**: статус Manager, прокси и настройки без API для показа токена.
- Fail-closed для явно выбранного **cloak**: при авторизованном Manager без живого CDP-маршрута Hermes не должен тихо переключиться на локальный Chromium.

Рабочая цепочка:

~~~
browser-инструменты Hermes → provider cloak → auth bridge / proxy → CloakBrowser-Manager → профиль → CDP
~~~

## Быстрый старт

### Одна команда

Bootstrap сам ставит Hermes, поднимает CloakBrowser-Manager, настраивает защищённый CDP-маршрут, включает provider cloak и останавливается с ошибкой, если обязательный этап не готов.

Linux server (Debian/Ubuntu) / WSL:

~~~
curl -fsSL https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/main/scripts/bootstrap_cloak.sh | sudo bash
~~~

Автоматическая установка недостающих пакетов поддерживается на Debian/Ubuntu. На другом Linux сначала установите Docker, Nginx, curl и openssl, затем используйте ручной режим.

Windows PowerShell от имени обычного пользователя:

~~~
$u = "https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/main/scripts/bootstrap_cloak.ps1?cache=$([guid]::NewGuid().ToString('N'))"; iwr $u -UseBasicParsing | iex
~~~

Bootstrap предложит выбор:

- `native` — Hermes работает в Windows, а Docker Desktop запускает только CloakBrowser-Manager и CDP bridge. Это прежний путь, совместимый с локальным `HERMES_HOME`.
- `compose` — отдельный Docker Compose-проект: `hermes` (шлюз и дашборд в одном контейнере), `bridge` и `manager`. Для него создаются свои volumes с данными Hermes и профилями Cloak; существующий проект `hermes-cloak` не используется и не меняется.

Чтобы сразу выбрать полный Docker-вариант и явно назвать новый независимый проект:

~~~powershell
$u = "https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/main/scripts/bootstrap_cloak.ps1?cache=$([guid]::NewGuid().ToString('N'))"
& ([scriptblock]::Create((iwr $u -UseBasicParsing).Content)) -Mode compose -ComposeProjectName hermes-cloak-edition
~~~

Первый Compose-запуск собирает локальный образ Hermes и обычно занимает 15–45 минут; Docker Desktop может запросить UAC, WSL или перезагрузку. После сборки bootstrap проверяет Manager, защищённый CDP bridge и доступность панели. Все опубликованные порты привязаны только к `127.0.0.1`.
После завершения добавьте учётные данные модели только локально:

- `native`: `%LOCALAPPDATA%\hermes\.env` (или заданный `HERMES_HOME`).
- `compose`: откройте `http://127.0.0.1:9119`. Логин — `admin`; случайный пароль лежит только в `.cloak-compose.env` (`Get-Content .cloak-compose.env | Select-String HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`). Либо выполните в каталоге установки:

~~~powershell
docker compose --project-name hermes-cloak-edition --env-file .cloak-compose.env -f docker-compose.cloak.yml exec hermes hermes setup
~~~

Токен CloakBrowser-Manager генерируется bootstrap-ом в `.cloak-compose.env` для Compose или хранится в `%USERPROFILE%\.hermes\cloak\manager.env` для native. Оба файла остаются только на машине; bootstrap не печатает секреты.

Остановить полный стек можно так:

~~~powershell
docker compose --project-name hermes-cloak-edition --env-file .cloak-compose.env -f docker-compose.cloak.yml down
~~~
### Ручной режим

Если Hermes уже установлен, можно развернуть только Cloak-стек из checkout проекта.

Linux server (Debian/Ubuntu) / WSL:

~~~
sudo bash scripts/install_cloak.sh --configure-provider --strict
~~~

Windows с Docker Desktop:

~~~
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_cloak.ps1 -ConfigureProvider -Strict
~~~

При режиме strict установщик публикует адрес CDP bridge только после HTTP- и WebSocket-проверки временного профиля. Отдельный локальный Chromium для Cloak не скачивается: provider подключается к браузеру, которым управляет Manager, по CDP.

## Возможности и режимы

| Компонент | Что делает | Условие |
| --- | --- | --- |
| plugins/browser/cloak/provider.py | Лёгкий provider cloak | Всегда регистрируется |
| plugins/browser/cloak/_impl/ | Профили, proxy pool, humanized input, captcha, idle reaper | Нужны cloakbrowser, Playwright и связанные зависимости |
| scripts/install_cloak.* | Manager, CDP proxy/bridge и readiness-проверка | Debian/Ubuntu/WSL или Windows |
| /cloak | Статус и управление Cloak из веб-панели | Запущен Hermes web server |
| skills/cloak-* | Операционные инструкции для профилей и прокси | Запускаются по запросу пользователя |

Отсутствие тяжёлых опциональных зависимостей не должно ломать discovery Hermes: базовый provider остаётся доступен, а богатый набор cloak-инструментов подключается best-effort.

## Проверка после изменений

В checkout проекта:

~~~
python -m pytest tests/cloak -q
~~~

Для проверки shell-установщика без запуска Docker:

~~~
bash -n scripts/install_cloak.sh
~~~

Полная реальная проверка требует доступного CloakBrowser-Manager, Docker (если Manager локальный) и разрешённого тестового окружения.

## Структура Cloak Edition

~~~
plugins/browser/cloak/        provider, профили, proxy pool, humanize и tools
scripts/install_cloak.*       установка Linux/Windows
scripts/cloak/                CDP bridge, HTTP/WS probes и readiness
hermes_cli/cloak_dashboard.py панель /cloak
tests/cloak/                  регрессионные тесты интеграции
skills/cloak-*/               встроенные навыки для операций с профилями и прокси
~~~

## Документация и поддержка

- [Issues этого форка](https://github.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/issues)
- [Исходный Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [CloakBrowser-Manager](https://github.com/CloakHQ/CloakBrowser-Manager)

## Лицензия

MIT — см. [LICENSE](LICENSE).

Hermes Unlocked — Cloak Edition основан на Hermes Agent; изменения этой редакции сосредоточены на интеграции Cloak и её безопасной эксплуатации.
