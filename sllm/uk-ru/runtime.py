#!/usr/bin/env python3
"""
Ленивый запуск SearXNG в докере — метапоисковик без ключей и аккаунтов.

Поднимается только когда корпус не дал ответа, и убирается за собой:
контейнер гасится всегда, docker-рантайм — только если его запустил этот
скрипт И после остановки контейнера не осталось чужих. Контейнеры с
restart:always поднимаются вместе с демоном, и гасить его вслепую значит
убить чужую работу.
"""

import os
import shutil
import subprocess
import sys
import time

import requests

CONTAINER = os.environ.get("SLLM_SEARX_CONTAINER", "uk-ru-searxng")
IMAGE = os.environ.get("SLLM_SEARX_IMAGE", "searxng/searxng:latest")
PORT = int(os.environ.get("SLLM_SEARX_PORT", "18080"))
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "searxng-settings.yml")

DOCKER_START_TIMEOUT = 120   # сек на подъём рантайма
CONTAINER_START_TIMEOUT = 180  # сек на старт контейнера (первый раз тянется образ)
DOCKER_CMD_TIMEOUT = 30

# Рантаймы в порядке предпочтения: (человеческое имя, проверка наличия, старт, останов)
RUNTIMES = [
    ("OrbStack",
     lambda: shutil.which("orb") or os.path.exists("/Applications/OrbStack.app"),
     ["orb", "start"],
     ["orb", "stop"]),
    ("Docker Desktop",
     lambda: os.path.exists("/Applications/Docker.app"),
     ["open", "-a", "Docker"],
     ["osascript", "-e", 'quit app "Docker Desktop"']),
    ("colima",
     lambda: shutil.which("colima"),
     ["colima", "start"],
     ["colima", "stop"]),
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _run(cmd, timeout=DOCKER_CMD_TIMEOUT):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def docker_available() -> bool:
    r = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    return bool(r and r.returncode == 0 and r.stdout.strip())


def other_containers_running(exclude: str) -> list:
    r = _run(["docker", "ps", "--format", "{{.Names}}"])
    if not r or r.returncode != 0:
        return []
    return [n for n in r.stdout.split() if n and n != exclude]


def write_settings() -> str:
    """Дефолтный образ отдаёт только HTML; JSON-формат нужно включить явно.
    Лимитер выключаем — он рассчитан на публичные инстансы, а тут локальный
    однопользовательский."""
    settings = """# Генерируется uk-ru/runtime.py, правки перезапишутся.
use_default_settings: true
server:
  secret_key: "uk-ru-local-instance"
  limiter: false
  public_instance: false
  image_proxy: false
search:
  safe_search: 0
  formats:
    - html
    - json
"""
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write(settings)
    return SETTINGS_PATH


class SearxRuntime:
    """Владеет всем, что подняли: контейнером и, возможно, самим рантаймом."""

    def __init__(self):
        self.base_url = f"http://127.0.0.1:{PORT}"
        self.started_container = False
        self.started_runtime = None  # (имя, команда останова)

    # -- подъём ---------------------------------------------------------
    def _start_runtime(self) -> bool:
        for name, present, start_cmd, stop_cmd in RUNTIMES:
            if not present():
                continue
            log(f"[searx] docker не отвечает, запускаю {name}...")
            if _run(start_cmd, timeout=60) is None:
                log(f"[searx] не удалось выполнить {' '.join(start_cmd)}")
                continue
            deadline = time.time() + DOCKER_START_TIMEOUT
            while time.time() < deadline:
                if docker_available():
                    self.started_runtime = (name, stop_cmd)
                    log(f"[searx] {name} поднят")
                    return True
                time.sleep(2)
            log(f"[searx] {name} не поднялся за {DOCKER_START_TIMEOUT} с")
        return False

    def _container_running(self) -> bool:
        r = _run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"])
        return bool(r and CONTAINER in (r.stdout or ""))

    def _wait_ready(self) -> bool:
        deadline = time.time() + CONTAINER_START_TIMEOUT
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.base_url}/search",
                                 params={"q": "test", "format": "json"}, timeout=5)
                if r.ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(2)
        return False

    def ensure(self):
        """Поднимает SearXNG и возвращает базовый URL. None — если не удалось."""
        if not docker_available() and not self._start_runtime():
            log("[searx] docker недоступен: ни OrbStack, ни Docker Desktop, ни colima "
                "не запустились. Веб-поиск отключён")
            return None

        if self._container_running():
            log(f"[searx] контейнер {CONTAINER} уже запущен, переиспользую")
        else:
            settings = write_settings()
            log(f"[searx] поднимаю {IMAGE} на порту {PORT} "
                "(первый запуск тянет образ, это может занять пару минут)...")
            r = _run([
                "docker", "run", "-d", "--rm",
                "--name", CONTAINER,
                "-p", f"127.0.0.1:{PORT}:8080",
                "-v", f"{settings}:/etc/searxng/settings.yml:ro",
                IMAGE,
            ], timeout=CONTAINER_START_TIMEOUT)
            if not r or r.returncode != 0:
                log(f"[searx] не удалось запустить контейнер: "
                    f"{(r.stderr or '').strip() if r else 'таймаут'}")
                self.shutdown()
                return None
            self.started_container = True

        if not self._wait_ready():
            log(f"[searx] контейнер не ответил за {CONTAINER_START_TIMEOUT} с")
            self.shutdown()
            return None
        log(f"[searx] готов: {self.base_url}")
        return self.base_url

    # -- уборка ---------------------------------------------------------
    def shutdown(self) -> None:
        if self.started_container:
            log(f"[searx] останавливаю контейнер {CONTAINER}")
            _run(["docker", "stop", CONTAINER], timeout=60)
            self.started_container = False

        if not self.started_runtime:
            return
        name, stop_cmd = self.started_runtime
        others = other_containers_running(CONTAINER)
        if others:
            log(f"[searx] {name} оставляю запущенным: работают чужие контейнеры "
                f"({', '.join(others[:3])})")
        else:
            log(f"[searx] гашу {name} — я его поднял, других контейнеров нет")
            _run(stop_cmd, timeout=60)
        self.started_runtime = None


if __name__ == "__main__":
    rt = SearxRuntime()
    try:
        url = rt.ensure()
        print("base_url:", url)
        if url:
            r = requests.get(f"{url}/search",
                             params={"q": "постановление 491 общее имущество", "format": "json"},
                             timeout=30)
            data = r.json()
            print("результатов:", len(data.get("results", [])))
            for item in data.get("results", [])[:3]:
                print(" -", item.get("title", "")[:70], "|", item.get("url", "")[:60])
    finally:
        rt.shutdown()
