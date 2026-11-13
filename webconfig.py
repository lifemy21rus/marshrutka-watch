#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Веб-панель настроек marshrutka-watch.

Только стандартная библиотека. Запускается на 127.0.0.1 и проксируется nginx'ом
по HTTPS. Изменения пишутся в config.json, демон подхватывает их без перезапуска.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import marshrutka_watch as mw

APP = "marshrutka-watch"
SERVICE = "marshrutka-watch"
AUTH_ITERATIONS = 200_000
MAX_BODY = 64 * 1024
CHIPS = [20, 15, 10, 5, 2]

_cache: Dict[str, Any] = {"ts": 0.0, "rows": [], "error": "", "meta": "", "tz": None, "status": {}, "diagnostics": {}, "effective": {}, "stale": False, "reason": ""}
_cache_lock = threading.Lock()

# Метка снимка прогноза: панель читает только «свои» файлы.
SNAPSHOT_KIND = "marshrutka-watch/1"
_fetch_lock = threading.Lock()
_status_cache: Dict[str, Any] = {"ts": 0.0, "value": ""}
_status_lock = threading.Lock()
_refresh_event = threading.Event()

# Сессии держим в файле: перезапуск сервиса не должен выкидывать из аккаунта.
SESSIONS: Dict[str, float] = {}
SESSIONS_PATH = ""
_sessions_lock = threading.Lock()


def _sessions_load(path: str) -> Dict[str, float]:
    now = time.time()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: Dict[str, float] = {}
    for key, value in (data or {}).items():
        try:
            expires = float(value)
        except (TypeError, ValueError):
            continue
        if expires > now:
            out[str(key)] = expires
    return out


def _sessions_save(path: str, data: Dict[str, float]) -> None:
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Пароль
# --------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_password(path: str, user: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    digest = _hash_password(password, salt, AUTH_ITERATIONS)
    data = {
        "user": user,
        "iterations": AUTH_ITERATIONS,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_auth(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def check_password(auth: Dict[str, Any], user: str, password: str) -> bool:
    if user != auth.get("user"):
        return False
    try:
        salt = bytes.fromhex(auth["salt"])
        expected = bytes.fromhex(auth["hash"])
        iterations = int(auth.get("iterations", AUTH_ITERATIONS))
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(_hash_password(password, salt, iterations), expected)


# --------------------------------------------------------------------------
# Конфиг
# --------------------------------------------------------------------------

MANAGED = ("window_from", "window_to", "weekdays_only", "window_by_weekday", "notify_at", "notify_repeat_minutes", "notify_mode", "walk_minutes", "interval", "route_settings")


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_config(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def validate(form: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    wf = (form.get("window_from") or "").strip()
    wt = (form.get("window_to") or "").strip()
    for title, value in (("начало окна", wf), ("конец окна", wt)):
        try:
            mw.parse_hhmm(value)
        except Exception:  # noqa: BLE001
            errors.append("Некорректное %s: нужен формат ЧЧ:ММ" % title)

    raw = (form.get("notify_at") or "").strip()
    thresholds: List[int] = []
    try:
        thresholds = sorted({int(x) for x in re.split(r"[,\s;]+", raw) if x}, reverse=True)
    except ValueError:
        errors.append("Пороги должны быть целыми числами минут")
    if not thresholds:
        errors.append("Укажите хотя бы один порог, например 20,10,5")
    elif any(v < 0 or v > 180 for v in thresholds):
        errors.append("Пороги должны быть от 0 до 180 минут")

    try:
        walk = int(form.get("walk_minutes") or 0)
    except ValueError:
        walk = -1
    if not (0 <= walk <= 120):
        errors.append("«Пора выходить» — число от 0 до 120 (0 выключает)")

    try:
        interval = int(form.get("interval") or 60)
    except ValueError:
        interval = 0
    if not (30 <= interval <= 600):
        errors.append("Интервал опроса — от 30 до 600 секунд")

    try:
        repeat = int(form.get("notify_repeat_minutes") or 1)
    except ValueError:
        repeat = 0
    if not (1 <= repeat <= 60):
        errors.append("Частота повторов — число от 1 до 60 минут (1 = каждую минуту)")

    notify_mode = (form.get("notify_mode") or "repeat").strip().lower()
    if notify_mode not in ("repeat", "once"):
        errors.append("Режим уведомлений: выберите «повторять» или «один раз на порог»")

    route_settings = {}
    raw_rs = (form.get("route_settings") or "").strip()
    if raw_rs:
        try:
            parsed_rs = json.loads(raw_rs)
            if not isinstance(parsed_rs, dict):
                raise ValueError("ожидается объект JSON")
            route_settings = parsed_rs
        except Exception:
            errors.append("Переопределения по маршрутам: нужен корректный JSON-объект (или пусто)")

    weekdays_only = (form.get("weekdays_only") or "").strip().lower() in ("1", "true", "yes", "on")

    # Окна по дням недели: {"1": ["08:30", "09:10"], ...} — ключ номер дня
    # (0=пн … 6=вс) или название (mon..sun). Необязательно.
    window_by_weekday: Dict[str, Any] = {}
    raw_wd = (form.get("window_by_weekday") or "").strip()
    if raw_wd:
        try:
            parsed_wd = json.loads(raw_wd)
            if not isinstance(parsed_wd, dict):
                raise ValueError("ожидается объект JSON")
            for dk, dv in parsed_wd.items():
                idx = mw.day_index(dk)
                if idx is None:
                    errors.append("window_by_weekday: день «%s» не распознан" % str(dk))
                    continue
                pair = None
                if isinstance(dv, dict):
                    pair = [dv.get("from") or dv.get("start") or dv.get("с"),
                            dv.get("to") or dv.get("end") or dv.get("до")]
                elif isinstance(dv, (list, tuple)):
                    pair = [dv[0] if len(dv) > 0 else None, dv[1] if len(dv) > 1 else None]
                if not pair or not str(pair[0] or "").strip() or not str(pair[1] or "").strip():
                    continue
                try:
                    mw.parse_hhmm(str(pair[0]))
                    mw.parse_hhmm(str(pair[1]))
                except Exception:  # noqa: BLE001
                    errors.append("window_by_weekday: «%s» — нужно ЧЧ:ММ для начала и конца" % str(dk))
                    continue
                window_by_weekday[str(idx)] = [str(pair[0]).strip(), str(pair[1]).strip()]
        except ValueError:
            errors.append("window_by_weekday: нужен корректный JSON-объект вида {\"1\": [\"08:30\", \"09:10\"]}")

    return {
        "window_from": wf,
        "window_to": wt,
        "weekdays_only": weekdays_only,
        "window_by_weekday": window_by_weekday,
        "notify_at": thresholds,
        "notify_repeat_minutes": repeat,
        "notify_mode": notify_mode,
        "walk_minutes": walk,
        "interval": interval,
        "route_settings": route_settings,
    }, errors


# --------------------------------------------------------------------------
# Прогноз и статус
# --------------------------------------------------------------------------

def _snapshot_rows(config_path: str, max_age: float = 420.0) -> Optional[Dict[str, Any]]:
    """Прогноз, который уже посчитал трекер (лежит в last_arrivals.json).

    Так панель не делает второй запрос к Яндексу — вдвое меньше нагрузки и вдвое быстрее.
    """
    cfg_path = config_path
    override = ""
    try:
        with open(config_path, "r", encoding="utf-8-sig") as fh:
            override = str(json.load(fh).get("snapshot_path") or "").strip()
    except (OSError, ValueError):
        pass
    path = override or os.path.join(os.path.dirname(os.path.abspath(cfg_path)),
                                    "last_arrivals.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or time.time() - ts > max_age:
        return None
    # снимок должен быть нашим: иначе это чужие или битые данные
    if data.get("kind") != SNAPSHOT_KIND:
        return None
    tz = data.get("tz")
    rows = []
    now = time.time()
    for row in data.get("rows") or []:
        row = dict(row)
        try:
            row["mins"] = max(0, int(round((float(row.get("ts") or 0) - now) / 60.0)))
        except (TypeError, ValueError):
            pass
        rows.append(row)
    out = {"ts": ts, "rows": rows, "error": "", "meta": data.get("meta") or "", "tz": tz}
    for k in ("status", "diagnostics", "effective"):
        if k in data:
            out[k] = data.get(k)
    return out


def _cache_reason(error: str, rows: List[Dict[str, Any]], status: Optional[Dict[str, Any]] = None) -> str:
    """Почему сейчас нет строк в прогнозе — чтобы панель говорила конкретно."""
    if error:
        return "Яндекс сейчас не ответил: %s" % error
    if not rows:
        st = status or {}
        if st.get("in_window") is False:
            return "Вне окна слежения — трекер сейчас ничего не проверяет."
        if st.get("last_error"):
            return "Ошибка последнего опроса: %s" % str(st.get("last_error"))[:160]
        return "Машины в эфире пока нет — попробуйте обновить."
    return ""


def _fetch_forecast(config_path: str) -> None:
    """Один опрос Яндекса. Долгий (5–20 с) — поэтому вызывается из фона."""
    snap = _snapshot_rows(config_path)
    if snap is not None:
        with _cache_lock:
            _cache.update(ts=snap.get("ts"), rows=snap.get("rows") or [], error="",
                          meta=snap.get("meta") or "", tz=snap.get("tz"),
                          status=snap.get("status") or {}, diagnostics=snap.get("diagnostics") or {},
                          effective=snap.get("effective") or {}, stale=False,
                          reason=_cache_reason("", snap.get("rows") or [], snap.get("status") or {}))
        return
    rows: List[Dict[str, Any]] = []
    error = ""
    meta = ""
    tz = None
    stale = False
    try:
        cfg = mw.Config()
        mw.apply_settings(cfg, load_config(config_path))
        mw.normalize(cfg)
        cfg.timeout = min(cfg.timeout or 20, 20)
        tr = mw.Tracker(cfg)
        arrivals = tr.poll()
        tz = tr.yandex.tz_offset
        meta = tr.yandex.stop_name
        for a in sorted(arrivals, key=lambda x: x.ts):
            rows.append({
                "route": a.route,
                "when": mw.city_time(a.ts, tz).strftime("%H:%M"),
                "mins": max(0, int(round(a.minutes))),
                "side": a.side or "",
                "dest": a.dest or "",
                "live": bool(a.live),
                "source": "прогноз" if a.source == "Estimated" else "расписание",
            })
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        rows = list(_cache.get("rows") or [])   # лучше старые данные, чем ничего
        meta = _cache.get("meta") or ""
        tz = _cache.get("tz")
        stale = bool(rows)
    with _cache_lock:
        extras = {"status": _cache.get("status") or {}, "diagnostics": _cache.get("diagnostics") or {},
                  "effective": _cache.get("effective") or {}} if stale else {"status": {}, "diagnostics": {}, "effective": {}}
        _cache.update(ts=time.time(), rows=rows, error=error, meta=meta, tz=tz,
                       stale=stale, reason=_cache_reason(error, rows, _cache.get("status") or {}),
                       **extras)


def _warmer_loop(config_path: str) -> None:
    """Обновляет прогноз по запросу: как только кто-то открыл панель — один опрос.

    Без этого Яндекс опрашивался бы круглосуточно, даже когда панель никто не смотрит.
    """
    while True:
        _refresh_event.wait(timeout=120.0)
        _refresh_event.clear()
        try:
            _fetch_forecast(config_path)
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write("%s [WEB] фон: не удалось обновить прогноз: %s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), exc))
            sys.stdout.flush()


def forecast(config_path: str, force: bool = False) -> Dict[str, Any]:
    """Отдаёт кэш прогноза. Никогда не блокирует страницу на сети.

    Если данных нет или они старые — ставим фоновый запрос и отдаём что есть.
    При force — ждём, пока фон подхватит обновление, и отдаём свежие данные
    (иначе кнопка «Обновить» возвращает мгновенно то, что лежит в кэше, и
    пользователь видит то же самое «ничего не показывает»).
    """
    with _cache_lock:
        cached = dict(_cache)
    age = time.time() - (cached.get("ts") or 0)
    if force or age > 45.0:
        _refresh_event.set()
        if force:
            deadline = time.time() + 45.0
            old_ts = float(cached.get("ts") or 0)
            while time.time() < deadline:
                time.sleep(0.2)
                with _cache_lock:
                    cur_ts = float(_cache.get("ts") or 0)
                if cur_ts > old_ts:
                    break
            with _cache_lock:
                cached = dict(_cache)
    return cached


def service_status() -> str:
    """systemctl is-active кэшируем на 5 секунд — иначе каждый рендер жмёт процесс."""
    now = time.time()
    with _status_lock:
        if _status_cache["value"] and now - _status_cache["ts"] < 5.0:
            return _status_cache["value"]
    try:
        res = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True,
                             text=True, timeout=5)
        value = res.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        value = "unknown"
    with _status_lock:
        _status_cache.update(ts=now, value=value)
    return value


def city_now(tz_offset: Optional[int]) -> str:
    if tz_offset is None:
        return datetime.now().strftime("%H:%M")
    return mw.city_time(time.time(), tz_offset).strftime("%H:%M")


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def render_forecast(fc: Dict[str, Any]) -> str:
    """HTML блока с прибытиями. Используется и страницей, и автообновлением."""
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    rows: List[str] = []
    if fc.get("error"):
        rows.append('<p class="warning">Яндекс сейчас не ответил: %s%s</p>' % (
            esc(fc["error"]),
            " — показаны данные прошлого опроса" if fc.get("rows") else ""))
    if fc.get("rows"):
        for r in fc["rows"]:
            bits = ['<span class="mins">%s</span>' % esc(r["mins"]),
                    '<span class="when">в %s</span>' % esc(r["when"])]
            detail = " · ".join(x for x in (r["side"], r["dest"]) if x)
            source = esc(r.get("source") or "") if r.get("live", True) else (
                '<span class="muted-src">%s, без GPS</span>' % esc(r.get("source") or ""))
            rows.append(
                '<div class="arrival"><div class="left">%s <span class="route">№%s</span></div>'
                '<div class="right">%s<span class="src">%s</span></div></div>' % (
                    " ".join(bits), esc(r.get("route") or ""),
                    ('<span class="detail">%s</span>' % esc(detail)) if detail else "",
                    source,
                ))
    elif not fc.get("error"):
        reason = fc.get("reason") or ""
        if reason:
            rows.append('<p class="empty">%s</p>' % esc(reason))
        else:
            rows.append('<p class="empty">Сейчас машин в эфире нет — попробуйте обновить.</p>')
    return "".join(rows)


def render_observability(fc: Dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)
    def fmt_ts(ts):
        try:
            ts = float(ts)
            if ts <= 0:
                return "—"
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except Exception:
            return "—"
    st = fc.get("status") or {}
    diag = fc.get("diagnostics") or {}
    parts = []
    parts.append('<div class="obs-grid">')
    parts.append('<div><span class="obs-k">В окне слежения</span><span class="obs-v">%s</span></div>' % ("да" if st.get("in_window") else "нет/неизвестно"))
    parts.append('<div><span class="obs-k">Последний опрос</span><span class="obs-v">%s</span></div>' % esc(fmt_ts(st.get("last_poll_ts"))))
    parts.append('<div><span class="obs-k">Последний успех</span><span class="obs-v">%s</span></div>' % esc(fmt_ts(st.get("last_ok_ts"))))
    err = st.get("last_error") or ""
    parts.append('<div><span class="obs-k">Последняя ошибка</span><span class="obs-v">%s</span></div>' % (esc(err[:120]) if err else "—"))
    parts.append('</div>')
    ln = diag.get("last_notify") or {}
    if ln:
        parts.append('<p class="hint"><strong>Последнее уведомление:</strong> %s, №%s, %s мин, порог %s, %s</p>' % (
            esc(fmt_ts(ln.get("ts"))), esc(ln.get("route") or "?"), esc(ln.get("mins")), esc(ln.get("threshold")), esc(ln.get("when") or "")))
    reason = diag.get("last_skip_reason") or ""
    if reason:
        parts.append('<p class="hint"><strong>Состояние/антиспам:</strong> %s</p>' % esc(reason))
    events = diag.get("recent_events") or []
    if events:
        items = []
        for ev in reversed(events[-5:]):
            t = fmt_ts(ev.get("ts"))
            typ = ev.get("type") or ""
            if typ == "notify":
                items.append('<li>%s — уведомление №%s, %s мин, порог %s</li>' % (esc(t), esc(ev.get("route")), esc(ev.get("mins")), esc(ev.get("threshold"))))
            elif typ == "notify_error":
                items.append('<li>%s — ошибка отправки №%s</li>' % (esc(t), esc(ev.get("route"))))
            elif typ == "skip":
                items.append('<li>%s — пропуск №%s: %s</li>' % (esc(t), esc(ev.get("route")), esc(ev.get("reason") or "")))
        if items:
            parts.append('<details><summary>Последние события</summary><ul class="obs-events">%s</ul></details>' % "".join(items))
    if not parts or len(parts) <= 2:
        parts.append('<p class="empty">Данные наблюдаемости появятся после очередного опроса трекера.</p>')
    return "".join(parts)


def page(config: Dict[str, Any], saved: bool, errors: List[str],
         values: Dict[str, Any], tz_offset: Optional[int], fc: Dict[str, Any],
         token: str = "", test_token: str = "", tested: Optional[bool] = None) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    status = service_status()
    status_ok = status == "active"
    rows = render_forecast(fc)

    chips = "".join(
        '<button type="button" data-min="%d">%d</button>' % (m, m) for m in CHIPS)

    err_html = ""
    if errors:
        err_html = '<div class="banner error"><strong>Проверьте настройки:</strong><ul>%s</ul></div>' % (
            "".join("<li>%s</li>" % esc(e) for e in errors))
    ok_html = ""
    if saved:
        ok_html = ('<div class="banner ok">Сохранено. Демон применит настройки '
                   'в течение минуты, перезапуск не нужен.</div>')
    test_html = ""
    if tested is True:
        test_html = ('<div class="banner ok">Тестовое уведомление отправлено. '
                     'Обычно пуш приходит в течение 5–10 секунд. Если его нет — '
                     'проверьте, что телефон подписан на топик в приложении ntfy.</div>')
    elif tested is False:
        test_html = ('<div class="banner error"><strong>Тестовое уведомление не ушло.</strong> '
                     'Причина в журнале: <code>journalctl -u marshrutka-watch -n 20</code>.</div>')

    raw_notify = values.get("notify_at")
    if isinstance(raw_notify, (list, tuple)):
        notify_value = ",".join(str(x) for x in raw_notify)
    else:
        notify_value = str(raw_notify or "")
    weekdays_val = values.get("weekdays_only", config.get("weekdays_only", False))
    if isinstance(weekdays_val, str):
        weekdays_val = weekdays_val.strip().lower() in ("1", "true", "yes", "on")
    weekdays_checked = "checked" if weekdays_val else ""

    raw_wd_values = values.get("window_by_weekday", config.get("window_by_weekday") or {})
    if isinstance(raw_wd_values, str):
        wd_text = raw_wd_values
    else:
        try:
            wd_text = json.dumps(raw_wd_values or {}, ensure_ascii=False, default=str) if raw_wd_values else ""
        except Exception:  # noqa: BLE001
            wd_text = ""

    try:
        repeat_val = int(values.get("notify_repeat_minutes", config.get("notify_repeat_minutes", 1)) or 1)
    except (TypeError, ValueError):
        repeat_val = 1
    rep_sel = {v: ('selected' if repeat_val == v else '') for v in (1,2,3,5,10)}

    notify_mode_val = str(values.get("notify_mode", config.get("notify_mode", "repeat")) or "repeat").strip().lower()
    if notify_mode_val not in ("repeat", "once"):
        notify_mode_val = "repeat"
    mode_repeat_sel = 'selected' if notify_mode_val == 'repeat' else ''
    mode_once_sel = 'selected' if notify_mode_val == 'once' else ''

    rs_val = values.get("route_settings", config.get("route_settings", {}))
    if isinstance(rs_val, str):
        route_settings_text = rs_val
    else:
        try:
            route_settings_text = json.dumps(rs_val or {}, ensure_ascii=False, indent=2) if rs_val else ""
        except Exception:
            route_settings_text = ""
    obs_html = render_observability(fc)

    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Маршрутка 5 — настройки</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1b1f24;--muted:#68707a;--line:#e3e6ea;--accent:#2f6fed;--ok:#1a7f37;--err:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1d2126;--text:#e8eaed;--muted:#9aa3ad;--line:#2c3238;--accent:#6f9bff;--ok:#4ac26b;--err:#ff7b72}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:640px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:26px;margin:0 0 2px}
h2{font-size:17px;margin:0}
.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%%;margin-right:6px;vertical-align:middle}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--err)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px}
.card-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px}
.card-head a{color:var(--accent);text-decoration:none;font-size:14px}
.arrival{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.arrival:first-of-type{border-top:0}
.mins{font-size:22px;font-weight:700}
.when,.route{color:var(--muted);font-size:14px}
.detail{display:block;font-size:14px;color:var(--text)}
.src{display:block;font-size:12px;color:var(--ok)}
.muted-src{color:var(--muted)}
.empty{color:var(--muted);margin:6px 0}
.warning{color:var(--err);font-size:14px;margin:4px 0 8px}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
label.check{display:flex;align-items:center;gap:8px;font-weight:500;margin:12px 0 4px;cursor:pointer}
label.check input{width:auto;margin:0}
.row{display:flex;gap:10px}
.row>div{flex:1}
input[type=time],input[type=number],input[type=text],select{width:100%%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
.hint{color:var(--muted);font-size:13px;margin:6px 0 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.chips button{border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:999px;padding:7px 14px;font:inherit;cursor:pointer}
.chips button.on{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary{width:100%%;margin-top:20px;padding:14px;border:0;border-radius:12px;background:var(--accent);color:#fff;font:inherit;font-weight:700;cursor:pointer}
textarea{width:100%%;min-height:110px;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.obs-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;margin:6px 0 8px}
.obs-k{display:block;color:var(--muted);font-size:12px}
.obs-v{display:block;font-size:14px;font-weight:600;word-break:break-word}
.obs-events{margin:6px 0 0;padding-left:18px;font-size:13px;color:var(--muted)}
button.primary:active{opacity:.85}
button.ghost{width:100%%;margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:12px;background:transparent;color:var(--text);font:inherit;font-weight:600;cursor:pointer}
button.ghost:active{opacity:.7}
.banner{border-radius:10px;padding:11px 13px;margin:0 0 14px;font-size:14px}
.banner ul{margin:6px 0 0;padding-left:18px}
.banner.ok{background:rgba(26,127,55,.12);color:var(--ok)}
.banner.error{background:rgba(180,35,24,.12);color:var(--err)}
details{margin-top:16px}
summary{cursor:pointer;color:var(--muted);font-size:14px}
footer{color:var(--muted);font-size:13px;text-align:center;margin-top:8px}
</style>
</head>
<body>
<main>
<header>
  <h1>Маршрутка 5</h1>
  <p class="sub"><span class="dot %s"></span>%s · сервис: %s · сейчас %s</p>
</header>
%s
<section class="card" id="forecast-card">
  <div class="card-head"><h2>Сейчас на остановке</h2><a href="#" id="refresh-btn">Обновить</a></div>
  <div id="forecast">%s</div>
  <p class="hint">Показаны только маршруты: %s. Остальные маршруты остановки скрыты.<br>
     «прогноз» — время по данным Яндекса, «без GPS» — машины ещё нет на карте (Яндекс считает по расписанию).</p>
</section>
<section class="card" id="observability-card">
  <div class="card-head"><h2>Наблюдаемость и антиспам</h2></div>
  %s
  <p class="hint">Статус берётся из снимка, который пишет трекер (`last_arrivals.json`). Обновляется вместе с прогнозом.</p>
</section>
<form method="post" action="save">
  <input type="hidden" name="csrf" value="%s">
  <section class="card">
    <h2>Когда следить</h2>
    <div class="row">
      <div><label for="window_from">С</label><input type="time" id="window_from" name="window_from" value="%s" required></div>
      <div><label for="window_to">До</label><input type="time" id="window_to" name="window_to" value="%s" required></div>
    </div>
    <label class="check"><input type="checkbox" name="weekdays_only" value="1" %s> Не следить по выходным (сб, вс)</label>
    <p class="hint">Время чебоксарское. Можно задать окно через полночь, например 22:00–07:00.</p>
    <label for="window_by_weekday">Окна по дням недели (JSON, необязательно)</label>
    <textarea id="window_by_weekday" name="window_by_weekday" spellcheck="false" placeholder='{"1": ["08:30", "09:10"], "5": ["08:30", "09:10"]}'>%s</textarea>
    <p class="hint">Необязательно. Окно слежения по конкретным дням недели вместо общего. Ключи — номера дней недели (0=пн … 6=вс) или названия (mon, tue, wed…). Значение — массив [с, до] в формате ЧЧ:ММ. Оставьте пустым — будет общее окно.</p>
  </section>
  <section class="card">
    <h2>Уведомления</h2>
    <label for="notify_at">За сколько минут до прихода</label>
    <input type="text" id="notify_at" name="notify_at" value="%s" inputmode="numeric" autocomplete="off" required>
    <div class="chips" id="chips">%s</div>
    <p class="hint">Можно любые значения через запятую, например 20,15,10,5,2.</p>
    <label for="notify_mode">Режим уведомлений (антиспам)</label>
    <select id="notify_mode" name="notify_mode">
      <option value="repeat" %s>Повторять, пока маршрутка в зоне порогов</option>
      <option value="once" %s>Один раз на каждый порог (для одной поездки)</option>
    </select>
    <p class="hint">«Один раз на порог» уменьшает спам: для одного и того же автобуса пороги 15, 10, 5 и т.д. сработают по одному разу. «Повторять» использует интервал повтора ниже.</p>
    <label for="notify_repeat_minutes">Как часто повторять уведомления</label>
    <select id="notify_repeat_minutes" name="notify_repeat_minutes">
      <option value="1" %s>Каждую минуту (каждый опрос)</option>
      <option value="2" %s>Каждые 2 минуты</option>
      <option value="3" %s>Каждые 3 минуты</option>
      <option value="5" %s>Каждые 5 минут</option>
      <option value="10" %s>Каждые 10 минут</option>
    </select>
    <p class="hint">В режиме «повторять» повторное уведомление об том же автобусе придёт не раньше чем через выбранный интервал.</p>
    <details>
      <summary>Разные пороги по маршрутам (JSON)</summary>
      <label for="route_settings">Переопределения для маршрутов</label>
      <textarea id="route_settings" name="route_settings" spellcheck="false" placeholder='{"5":{"notify_at":[20,15,10,5],"notify_mode":"once","notify_repeat_minutes":2,"walk_minutes":3}}'>%s</textarea>
      <p class="hint">Необязательно. Позволяет задать свои пороги/режим/повтор/«пора выходить» для конкретного маршрута. Оставьте пустым, чтобы использовать общие настройки.</p>
    </details>
    <label for="walk_minutes">«Пора выходить» — за сколько минут</label>
    <input type="number" id="walk_minutes" name="walk_minutes" min="0" max="120" step="1" value="%s">
    <p class="hint">0 — выключено. Если маршрутка будет раньше этого времени, в пуше появится «⏰ Пора выходить!».</p>
    <details>
      <summary>Дополнительно</summary>
      <label for="interval">Интервал опроса, секунд</label>
      <input type="number" id="interval" name="interval" min="30" max="600" step="10" value="%s">
      <p class="hint">Оптимально 60. Чаще 30 секунд Яндекс опрашивать не стоит.</p>
    </details>
  </section>
  <button class="primary" type="submit">Сохранить</button>
</form>
<form method="post" action="test">
  <input type="hidden" name="csrf" value="%s">
  <button class="ghost" type="submit">Проверить уведомление</button>
  <p class="hint">Пришлёт тестовый пуш в ntfy — можно проверить канал, не дожидаясь маршрутки.</p>
</form>
<footer>Изменения применяются без перезапуска · <a href="#" id="refresh-btn2" style="color:inherit">обновить прогноз</a> · <a href="logout" style="color:inherit">выйти</a></footer>
</main>
<script>
(function(){
  var input=document.getElementById('notify_at');
  var buttons=Array.prototype.slice.call(document.querySelectorAll('#chips button'));
  function values(){return input.value.split(/[,\\s;]+/).filter(Boolean)}
  function sync(){
    var cur=values();
    buttons.forEach(function(b){b.classList.toggle('on',cur.indexOf(b.dataset.min)!==-1)});
  }
  buttons.forEach(function(b){
    b.addEventListener('click',function(){
      var cur=values();
      var i=cur.indexOf(b.dataset.min);
      if(i===-1){cur.push(b.dataset.min)}else{cur.splice(i,1)}
      input.value=cur.filter(Boolean).map(Number).sort(function(a,b){return b-a}).join(',');
      sync();
    });
  });
  input.addEventListener('input',sync);
  sync();
})();

/* автообновление прогноза: страница открывается мгновенно, данные подтягиваются фоном */
(function(){
  var box=document.getElementById('forecast');
  if(!box||!window.fetch){return}
  var busy=false;
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function render(d){
    var out='';
    if(d.error){out+='<p class="warning">Яндекс сейчас не ответил: '+esc(d.error)+
      (d.rows&&d.rows.length?' — показаны данные прошлого опроса':'')+'</p>'}
    if(d.rows&&d.rows.length){
      d.rows.forEach(function(r){
        var detail=[r.side,r.dest].filter(Boolean).join(' · ');
        var src=r.live?esc(r.source):'<span class="muted-src">'+esc(r.source)+', без GPS</span>';
        out+='<div class="arrival"><div class="left"><span class="mins">'+esc(r.mins)+
          '</span> <span class="when">в '+esc(r.when)+'</span> <span class="route">№'+esc(r.route)+
          '</span></div><div class="right">'+
          (detail?'<span class="detail">'+esc(detail)+'</span>':'')+
          '<span class="src">'+src+'</span></div></div>';
      });
    }else if(!d.error){
      var reason=d.reason||'';
      out+='<p class="empty">'+(reason?esc(reason):'Сейчас машин в эфире нет — попробуйте обновить.')+'</p>';
    }
    box.innerHTML=out;
    var sub=document.querySelector('.sub');
    if(sub&&d.now){sub.textContent=sub.textContent.replace(/сейчас \\d\\d:\\d\\d/,'сейчас '+d.now)}
  }
  function load(force){
    if(busy){return}
    busy=true;
    fetch('api/forecast'+(force?'?refresh=1':''),{credentials:'same-origin',cache:'no-store'})
      .then(function(r){return r.ok?r.json():null})
      .then(function(d){if(d)render(d)})
      .catch(function(){})
      .then(function(){busy=false});
  }
  var btn=document.getElementById('refresh-btn');
  if(btn){btn.addEventListener('click',function(e){e.preventDefault();load(true)})}
  var btn2=document.getElementById('refresh-btn2');
  if(btn2){btn2.addEventListener('click',function(e){e.preventDefault();load(true)})}
  load(false);
  setInterval(function(){load(false)},20000);
  document.addEventListener('visibilitychange',function(){
    if(!document.hidden){load(false)}
  });
})();
</script>
</body>
</html>""" % (
        "ok" if status_ok else "bad", esc(fc.get("meta") or "Медицинский центр"),
        esc(status), esc(city_now(tz_offset)),
        ok_html + err_html + test_html,
        "".join(rows),
        esc(", ".join(str(x) for x in (config.get("routes") or ["5"]))),
        obs_html,
        esc(token),
        esc(values.get("window_from") or config.get("window_from") or "05:30"),
        esc(values.get("window_to") or config.get("window_to") or "10:00"),
        weekdays_checked,
        esc(wd_text),
        esc(notify_value),
        chips,
        mode_repeat_sel, mode_once_sel,
        rep_sel[1], rep_sel[2], rep_sel[3], rep_sel[5], rep_sel[10],
        esc(route_settings_text),
        esc(values.get("walk_minutes", config.get("walk_minutes", 0))),
        esc(values.get("interval", config.get("interval", 60))),
        esc(test_token),
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def send_test(config: Dict[str, Any]) -> Tuple[bool, str]:
    """Отправляет тестовый пуш в ntfy тем же кодом, что и трекер."""
    topic = str(config.get("ntfy_topic") or "").strip()
    if not topic:
        return False, "в config.json не задан ntfy_topic"
    try:
        timeout = int(config.get("timeout") or 20)
    except (TypeError, ValueError):
        timeout = 20
    notif = mw.Ntfy(str(config.get("ntfy_server") or "https://ntfy.sh"), topic,
                    str(config.get("ntfy_token") or ""),
                    timeout=min(max(timeout, 5), 30))
    routes = ",".join(str(x) for x in (config.get("routes") or ["5"]))
    stop_url = "https://yandex.ru/maps/%s/%s/stops/%s/" % (
        config.get("region_id", "45"), config.get("city", "cheboksary"),
        config.get("stop_id", "stop__10094747"))
    try:
        ok = notif.send(
            "Проверка уведомлений — маршрутка %s" % routes,
            "Это тест с панели настроек. Канал ntfy работает, ждите пушей о маршрутке %s." % routes,
            priority="default", tags=["white_check_mark"], click=stop_url)
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    if not ok:
        return False, "ntfy не подтвердил отправку"
    return True, ""

SESSION_TTL = 7 * 24 * 3600
COOKIE_NAME = "mw_session"

# Одноразовые токены формы. В отличие от сравнения Origin они не зависят от
# того, как прокси/браузер передают Host, порт и схему.
CSRF: Dict[str, float] = {}
CSRF_TTL = 6 * 3600


def new_csrf() -> str:
    now = time.time()
    for key in [k for k, exp in CSRF.items() if exp < now]:
        CSRF.pop(key, None)
    token = secrets.token_urlsafe(24)
    CSRF[token] = now + CSRF_TTL
    return token


def csrf_ok(token: str) -> bool:
    exp = CSRF.pop(token or "", None)
    return exp is not None and exp > time.time()


def login_page(message: str = "", token: str = "") -> str:
    err = ('<div class="banner error">%s</div>' % html.escape(message)) if message else ""
    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Вход — маршрутка 5</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--text:#1b1f24;--muted:#68707a;--line:#e3e6ea;--accent:#2f6fed;--err:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1d2126;--text:#e8eaed;--muted:#9aa3ad;--line:#2c3238;--accent:#6f9bff;--err:#ff7b72}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:380px;margin:0 auto;padding:40px 16px}
h1{font-size:24px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
label{display:block;font-weight:600;font-size:14px;margin:12px 0 6px}
label:first-of-type{margin-top:0}
input{width:100%%;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%%;margin-top:18px;padding:14px;border:0;border-radius:12px;background:var(--accent);color:#fff;font:inherit;font-weight:700;cursor:pointer}
button:active{opacity:.85}
.banner{border-radius:10px;padding:11px 13px;margin:0 0 14px;font-size:14px}
.banner.error{background:rgba(180,35,24,.12);color:var(--err)}
</style>
</head>
<body>
<main>
<h1>Маршрутка 5</h1>
<p class="sub">Настройки уведомлений</p>
<section class="card">
%s
<form method="post" action="">
  <input type="hidden" name="csrf" value="%s">
  <label for="user">Логин</label>
  <input id="user" name="user" autocomplete="username" autocapitalize="off" required>
  <label for="password">Пароль</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Войти</button>
</form>
</section>
</main>
</body>
</html>""" % (err, html.escape(token))


class Handler(BaseHTTPRequestHandler):
    server_version = "marshrutka-web"
    sys_version = ""
    config_path = ""
    auth_path = ""
    cookie_path = "/marshrutka"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s [WEB] %s %s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.address_string(), fmt % args))
        sys.stdout.flush()

    # -- авторизация -------------------------------------------------------
    def _basic_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        user, _, password = raw.partition(":")
        try:
            auth = load_auth(self.auth_path)
        except Exception:  # noqa: BLE001
            return False
        return check_password(auth, user, password)

    def _session_token(self) -> str:
        for chunk in self.headers.get("Cookie", "").split(";"):
            key, _, value = chunk.strip().partition("=")
            if key == COOKIE_NAME:
                return value
        return ""

    def _session_ok(self) -> bool:
        token = self._session_token()
        if not token:
            return False
        with _sessions_lock:
            expires = SESSIONS.get(token)
            if expires is None:
                # сессия могла появиться в другом процессе/после рестарта
                SESSIONS.update(_sessions_load(SESSIONS_PATH))
                expires = SESSIONS.get(token)
            if expires is None:
                return False
            if expires < time.time():
                SESSIONS.pop(token, None)
                _sessions_save(SESSIONS_PATH, SESSIONS)
                return False
        return True

    def _logged_in(self) -> bool:
        return self._basic_ok() or self._session_ok()

    def _new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with _sessions_lock:
            SESSIONS[token] = time.time() + SESSION_TTL
            _sessions_save(SESSIONS_PATH, SESSIONS)
        cookie = "%s=%s; Path=%s; HttpOnly; SameSite=Lax; Max-Age=%d" % (
            COOKIE_NAME, token, self.cookie_path, SESSION_TTL)
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            cookie += "; Secure"
        return cookie

    def _drop_session(self) -> None:
        token = self._session_token()
        if token:
            with _sessions_lock:
                SESSIONS.pop(token, None)
                _sessions_save(SESSIONS_PATH, SESSIONS)

    def _redirect(self, location: str, cookie: Optional[str] = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _headers(self, code: int, length: int, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'; "
                         "form-action 'self'; base-uri 'none'")
        self.end_headers()

    def _send_html(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self._headers(code, len(data))
        self.wfile.write(data)

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _log_origin(self, action: str) -> None:
        """Пишем в лог несовпадение Origin — только для диагностики.

        Блокировать по Origin ненадёжно: за nginx/прокси браузер может прислать
        Origin с портом, `null` или иной схемой. От CSRF защищает токен формы.
        """
        origin = self.headers.get("Origin") or ""
        if not origin:
            return
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "").split(",")[0].strip().lower()
        try:
            origin_host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            origin_host = ""
        if origin_host and origin_host == host.split(":")[0]:
            return
        sys.stdout.write(
            "[WEB] %s: Origin=%r Host=%r XFH=%r Sec-Fetch-Site=%r\n" % (
                action, origin, self.headers.get("Host"),
                self.headers.get("X-Forwarded-Host"),
                self.headers.get("Sec-Fetch-Site")))
        sys.stdout.flush()

    def _read_form(self) -> Optional[Dict[str, str]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            text = self.rfile.read(length).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
        return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/favicon"):
            self._headers(204, 0)
            return
        if path == "/login":
            if self._logged_in():
                self._redirect("./")
            else:
                self._send_html(200, login_page(token=new_csrf()))
            return
        if path == "/logout":
            self._drop_session()
            self._redirect("login")
            return
        if path == "/api/forecast":
            if not self._logged_in():
                self._send_json(401, {"error": "не авторизован"})
                return
            query = parse_qs(urlparse(self.path).query)
            fc = forecast(self.config_path, force="refresh" in query)
            self._send_json(200, {
                "rows": fc.get("rows") or [],
                "meta": fc.get("meta") or "",
                "tz": fc.get("tz"),
                "error": fc.get("error") or "",
                "reason": fc.get("reason") or "",
                "ts": fc.get("ts") or 0,
                "now": city_now(fc.get("tz")),
                "status": fc.get("status") or {},
                "diagnostics": fc.get("diagnostics") or {},
                "effective": fc.get("effective") or {},
                "stale": bool(fc.get("stale")),
            })
            return
        if not self._logged_in():
            self._redirect("login")
            return
        query = parse_qs(urlparse(self.path).query)
        tested = None
        if "tested" in query:
            tested = query["tested"][-1] == "1"
        try:
            config = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self._send_html(500, "<h1>Ошибка конфига</h1><pre>%s</pre>" % html.escape(str(exc)))
            return
        fc = forecast(self.config_path, force="refresh" in query)
        values = {
            "window_from": config.get("window_from"),
            "window_to": config.get("window_to"),
            "weekdays_only": config.get("weekdays_only", False),
            "window_by_weekday": config.get("window_by_weekday") or {},
            "notify_at": config.get("notify_at") or [],
            "walk_minutes": config.get("walk_minutes", 0),
            "interval": config.get("interval", 60),
        }
        self._send_html(200, page(config, "saved" in query, [], values, fc.get("tz"), fc,
                                   new_csrf(), new_csrf(), tested))

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:
        self._log_origin("POST " + urlparse(self.path).path)
        path = urlparse(self.path).path

        form = self._read_form()
        if form is None:
            self._send_html(400, "Пустой или слишком большой запрос.")
            return
        if not csrf_ok(form.get("csrf", "")):
            self._send_html(403, "Страница устарела — обновите её и попробуйте снова.")
            return

        if path == "/login":
            user = form.get("user", "")
            password = form.get("password", "")
            try:
                auth = load_auth(self.auth_path)
            except Exception:  # noqa: BLE001
                auth = {}
            if check_password(auth, user, password):
                self._redirect("./", cookie=self._new_session())
            else:
                time.sleep(0.5)  # тормозим перебор
                self._send_html(401, login_page("Неверный логин или пароль.", new_csrf()))
            return

        if not self._logged_in():
            self._redirect("login")
            return

        if path == "/test":
            try:
                cfg = load_config(self.config_path)
            except Exception as exc:  # noqa: BLE001
                sys.stdout.write("%s [WEB] тест ntfy: не прочитан конфиг: %s\n" % (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), exc))
                self._redirect("./?tested=0")
                return
            ok, why = send_test(cfg)
            sys.stdout.write("%s [WEB] тест ntfy: %s%s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "отправлено" if ok else "ошибка", "" if ok else " — " + why))
            sys.stdout.flush()
            self._redirect("./?tested=1" if ok else "./?tested=0")
            return

        updates, errors = validate(form)
        config: Dict[str, Any] = {}
        try:
            config = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            errors.append("Не удалось прочитать config.json: %s" % exc)

        if errors:
            fc = forecast(self.config_path)
            self._send_html(400, page(config, False, errors, form, fc.get("tz"), fc,
                                       new_csrf(), new_csrf()))
            return

        for key in MANAGED:
            config[key] = updates[key]
        try:
            save_config(self.config_path, config)
        except Exception as exc:  # noqa: BLE001
            errors.append("Не удалось сохранить: %s" % exc)
            fc = forecast(self.config_path)
            self._send_html(500, page(config, False, errors, form, fc.get("tz"), fc,
                                      new_csrf(), new_csrf()))
            return

        rs_info = "да" if updates.get("route_settings") else "нет"
        sys.stdout.write("%s [WEB] сохранено: окно %s-%s%s, пороги %s, режим %s, повтор %s мин, пора выходить %s, route_settings=%s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), updates["window_from"], updates["window_to"],
            ", будни пн-пт" if updates["weekdays_only"] else "",
            ",".join(str(x) for x in updates["notify_at"]),
            updates.get("notify_mode", "repeat"),
            updates.get("notify_repeat_minutes", 1),
            updates.get("walk_minutes", 0) or "выкл",
            rs_info))
        sys.stdout.flush()
        self.send_response(303)
        self.send_header("Location", "./?saved=1")
        self.send_header("Content-Length", "0")
        self.end_headers()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Веб-панель настроек маршрутки")
    p.add_argument("--config", default="/opt/marshrutka-watch/config.json")
    p.add_argument("--auth", default="/opt/marshrutka-watch/web_auth.json")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--cookie-path", default="/marshrutka", help="Path для cookie сессии")
    p.add_argument("--sessions", default="/opt/marshrutka-watch/web_sessions.json",
                   help="файл с сессиями (чтобы не выкидывало при рестарте)")
    p.add_argument("--set-password", action="store_true", help="записать логин/пароль и выйти")
    p.add_argument("--user", default="marshrutka")
    p.add_argument("--password", help="пароль (если не задан — сгенерируется)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = parse_args(argv)

    if args.set_password:
        password = args.password or secrets.token_urlsafe(9)
        set_password(args.auth, args.user, password)
        print("Логин: %s" % args.user)
        print("Пароль: %s" % password)
        print("Файл: %s" % args.auth)
        return 0

    if not os.path.exists(args.auth):
        print("Нет файла пароля %s — создайте его: --set-password" % args.auth, file=sys.stderr)
        return 2

    Handler.config_path = args.config
    Handler.auth_path = args.auth
    Handler.cookie_path = args.cookie_path

    global SESSIONS_PATH
    SESSIONS_PATH = args.sessions
    with _sessions_lock:
        SESSIONS.update(_sessions_load(SESSIONS_PATH))

    # прогноз обновляем в фоне: страница открывается мгновенно
    warmer = threading.Thread(target=_warmer_loop, args=(args.config,), daemon=True)
    warmer.start()
    _refresh_event.set()      # первый прогноз — сразу после старта

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print("%s [WEB] слушаю http://%s:%d (config: %s)" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), args.bind, args.port, args.config), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
