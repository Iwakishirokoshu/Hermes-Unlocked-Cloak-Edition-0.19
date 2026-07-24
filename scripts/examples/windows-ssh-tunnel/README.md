# Windows SSH tunnels for Hermes + Cloak

Скопируйте `config.bat.example` в `config.bat`, укажите IP сервера, пользователя и путь к приватному SSH-ключу. `config.bat` не коммитится и не должен содержать токены.

- `open_everything.bat` открывает SSH-туннели к Hermes Dashboard (`9119`), Cloak Manager (`8180`) и CDP proxy (`8081`). Окно должно оставаться открытым.
- `open_manager.bat` открывает только Cloak Manager.
- `ssh_console.bat` открывает интерактивную SSH-консоль.
- `test_connection.bat` проверяет Dashboard, Docker, nginx и Manager.
- `repair_key_permissions.bat` ограничивает доступ к ключу текущей учётной записью Windows.

Скрипты предполагают loopback-порты сервера: Dashboard `9119`, Manager `8080`, CDP proxy `8081`. Они не публикуют сервисы в интернет.