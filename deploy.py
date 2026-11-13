#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заливка marshrutka-watch на сервер по настройкам из deploy.env.

Зависимости: только стандартная библиотека + paramiko (pip install paramiko).
Пароль берётся из переменной окружения VPS_PASS — в файле настроек его нет.

Пример:
    export VPS_PASS=секрет            # PowerShell: $env:VPS_PASS="секрет"
    python deploy.py                  # залить код и перезапустить сервисы
    python deploy.py --config         # заодно залить config.json
    python deploy.py --units          # заодно залить systemd-юниты
    python deploy.py --dry-run        # только показать, что будет залито
    python deploy.py --check          # только проверить состояние сервера
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import paramiko
except ImportError:
    sys.exit("Нужен paramiko:  pip install paramiko")

ENV_FILE = "deploy.env"
LOCAL_FILES = ("marshrutka_watch.py", "webconfig.py", "config.json",
               "marshrutka-watch.service", "marshrutka-web.service")


def load_env(path: str, overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Читает deploy.env: KEY=VALUE, комментарии и пустые строки пропускаются."""
    if not os.path.exists(path):
        sys.exit("Нет файла настроек деплоя: %s (см. deploy.env.example)" % path)
    data: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            data[key.strip()] = value.strip()
    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})
    return data


def to_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def connect(cfg: Dict[str, str]) -> paramiko.SSHClient:
    password = os.environ.get("VPS_PASS", "")
    if not password:
        sys.exit("Не задан пароль: задайте переменную окружения VPS_PASS")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.get("HOST", "127.0.0.1"),
        port=int(cfg.get("SSH_PORT", "22") or 22),
        username=cfg.get("SSH_USER", "root"),
        password=password,
        timeout=30,
    )
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> Tuple[int, str]:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = (stdout.read().decode("utf-8", "replace") +
           stderr.read().decode("utf-8", "replace")).strip()
    return code, out


def upload(client: paramiko.SSHClient, local: str, remote: str) -> None:
    sftp = client.open_sftp()
    try:
        sftp.put(local, remote)
    finally:
        sftp.close()


def check_server(client: paramiko.SSHClient, cfg: Dict[str, str]) -> List[str]:
    problems: List[str] = []
    print("--- проверка сервера ---")
    code, out = run(client, "systemctl is-active %s %s" % (
        cfg.get("SERVICE_TRACKER", "marshrutka-watch"),
        cfg.get("SERVICE_WEB", "marshrutka-web")))
    print("сервисы:", out.replace("\n", " "))
    for name in (cfg.get("SERVICE_TRACKER", "marshrutka-watch"),
                 cfg.get("SERVICE_WEB", "marshrutka-web")):
        code, out = run(client, "systemctl is-active %s" % name)
        if out.strip() != "active":
            problems.append("сервис %s не активен (%s)" % (name, out.strip()))

    url = cfg.get("PUBLIC_URL", "")
    if url:
        # 301/302/303 — это редирект на форму входа, тоже норма
        code, out = run(client, "curl -s -o /dev/null -w '%%{http_code}' -m 30 '%s'" % url)
        print("панель:", code, out.strip())
        if out.strip() not in ("200", "301", "302", "303", "307", "308"):
            problems.append("панель отвечает %s" % out.strip())

    snapshot = cfg.get("SNAPSHOT_DIR", "/var/lib/marshrutka-watch")
    code, out = run(client, "ls -l %s 2>&1 | tail -2" % snapshot)
    print("снимок:", out.replace("\n", " | "))
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Деплой marshrutka-watch")
    ap.add_argument("--env", default=ENV_FILE, help="файл настроек (по умолчанию %s)" % ENV_FILE)
    ap.add_argument("--config", action="store_true", help="заодно залить config.json")
    ap.add_argument("--units", action="store_true", help="заодно залить systemd-юниты")
    ap.add_argument("--no-restart", action="store_true", help="не перезапускать сервисы")
    ap.add_argument("--dry-run", action="store_true", help="только показать план")
    ap.add_argument("--check", action="store_true", help="только проверить состояние")
    args = ap.parse_args(argv)

    overrides: Dict[str, str] = {}
    if args.config:
        overrides["DEPLOY_CONFIG"] = "1"
    if args.units:
        overrides["DEPLOY_UNITS"] = "1"
    if args.no_restart:
        overrides["RESTART"] = "0"
    cfg = load_env(args.env, overrides)

    local_dir = os.path.abspath(cfg.get("LOCAL_DIR", "."))
    remote_dir = cfg.get("REMOTE_DIR", "/opt/marshrutka-watch")
    user = cfg.get("SERVICE_USER", "marshrutka")
    restart = to_bool(cfg.get("RESTART", "1"))
    deploy_config = to_bool(cfg.get("DEPLOY_CONFIG", "0"))
    deploy_units = to_bool(cfg.get("DEPLOY_UNITS", "0"))

    files = list(dict.fromkeys((cfg.get("FILES") or "").split()))
    if deploy_config:
        files.append("config.json")
    if deploy_units:
        files += ["marshrutka-watch.service", "marshrutka-web.service"]

    plan = [(name, os.path.join(local_dir, name), "%s/%s" % (remote_dir, name))
            for name in files if os.path.exists(os.path.join(local_dir, name))]
    missing = [name for name in files if not os.path.exists(os.path.join(local_dir, name))]

    print("Настройки из %s:" % args.env)
    print("  сервер:      %s@%s" % (cfg.get("SSH_USER", "root"), cfg.get("HOST")))
    print("  каталог:     %s" % remote_dir)
    print("  владелец:    %s" % user)
    print("  залить:      %s" % ", ".join(name for name, _, _ in plan))
    if missing:
        print("  НЕ НАЙДЕНЫ:  %s" % ", ".join(missing))
    print("  перезапуск:  %s" % ("да" if restart else "нет"))

    if args.dry_run:
        print("\n--dry-run: файлы не заливались.")
        return 0

    client = connect(cfg)
    try:
        if args.check:
            problems = check_server(client, cfg)
        else:
            print("\n--- заливка ---")
            for name, local, remote in plan:
                upload(client, local, remote)
                print("  %s -> %s" % (name, remote))
            code, out = run(client, "chown -R %s:%s %s && rm -rf %s/__pycache__" % (
                user, user, remote_dir, remote_dir))
            if code != 0:
                print("  chown:", out)
            snap = cfg.get("SNAPSHOT_DIR", "/var/lib/marshrutka-watch")
            code, out = run(client, "mkdir -p %s && chown %s:%s %s" % (snap, user, user, snap))
            if code != 0:
                print("  snapshot dir:", out)
            if restart:
                code, out = run(client, "systemctl restart %s %s" % (
                    cfg.get("SERVICE_TRACKER", "marshrutka-watch"),
                    cfg.get("SERVICE_WEB", "marshrutka-web")))
                print("  перезапуск:", out or "ok")
            wait = int(cfg.get("WAIT_SECONDS", "5") or 5)
            if wait:
                time.sleep(wait)
            problems = check_server(client, cfg)
    finally:
        client.close()

    print()
    if problems:
        print("ВНИМАНИЕ:")
        for item in problems:
            print("  -", item)
        return 1
    print("Деплой завершён, проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
