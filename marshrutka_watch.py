#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
marshrutka-watch — трекер городской маршрутки с push-уведомлениями.

Задача: каждое утро сказать жене, что маршрутка №N подъедет к остановке
через столько-то минут (и с какой стороны подъедет).

Источник данных: Яндекс Карты. Используется серверный рендер страниц:
  * страница остановки  -> точный прогноз прибытия по каждому маршруту,
    с интервалом, конечными остановками и id машин;
  * страница «Транспорт» -> живые координаты машин и их траектории;
  * страница маршрута   -> геометрия маршрута (список остановок и трек).

Канал доставки: ntfy (ntfy.sh) — бесплатное приложение для iOS/Android,
уведомление прилетает как push.

Зависимостей нет — только стандартная библиотека Python 3.8+.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

APP = "marshrutka-watch"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
STATE_RE = re.compile(r'<script type="application/json" class="state-view">(.*?)</script>', re.S)
R_EARTH = 6371000.0

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_LOG_LEVEL = 20


def log(msg: str, level: str = "INFO") -> None:
    if LOG_LEVELS.get(level, 20) < _LOG_LEVEL:
        return
    print("%s [%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg), flush=True)


def city_time(ts: float, tz_offset: Optional[int] = None) -> datetime:
    """Время в часовом поясе города, а не сервера (VPS может жить в другом поясе)."""
    if tz_offset is None:
        return datetime.fromtimestamp(ts)
    return datetime.fromtimestamp(ts, timezone(timedelta(seconds=tz_offset)))


class DataError(RuntimeError):
    """Яндекс отдал неожиданный ответ (сменился формат, капча и т.п.)."""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@contextlib.contextmanager
def prefer_ipv4():
    """На некоторых VPS IPv6-адрес есть, но не маршрутизируется — принудительно берём IPv4."""
    orig = socket.getaddrinfo

    def ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
        infos = orig(host, port, family, type, proto, flags)
        if family in (0, socket.AF_UNSPEC):
            v4 = [i for i in infos if i[0] == socket.AF_INET]
            if v4:
                return v4
        return infos

    socket.getaddrinfo = ipv4_first
    try:
        yield
    finally:
        socket.getaddrinfo = orig


def http_get(url: str, timeout: int = 30, headers: Optional[Dict[str, str]] = None) -> str:
    h = {
        "User-Agent": UA,
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        # без gzip страницы карт весят по мегабайту — трафик на VPS этого не простит
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with prefer_ipv4():
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        raise DataError("HTTP %s на %s" % (exc.code, url)) from exc
    except Exception as exc:  # сеть, таймаут, DNS
        raise DataError("сеть недоступна (%s): %s" % (url, exc)) from exc
    return raw.decode("utf-8", "replace")


def fetch_state(url: str, timeout: int = 30) -> Dict[str, Any]:
    """Скачивает страницу Яндекс.Карт и достаёт из неё встроенный JSON состояния."""
    html = http_get(url, timeout=timeout)
    m = STATE_RE.search(html)
    if not m:
        raise DataError("не найден JSON-стейт на %s (формат страницы изменился?)" % url)
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise DataError("битый JSON на %s: %s" % (url, exc)) from exc


# --------------------------------------------------------------------------
# Геометрия маршрута
# --------------------------------------------------------------------------

@dataclass
class StopOnRoute:
    name: str
    stop_id: Optional[str]
    poly_idx: int       # индекс точки трека, к которой привязана остановка
    arc: float = 0.0   # расстояние от начала маршрута вдоль трека, метры
    order: int = 0     # порядковый номер в маршруте
    lon: float = 0.0   # собственные координаты остановки (из данных Яндекса)
    lat: float = 0.0


@dataclass
class RouteGeometry:
    """Трек маршрута: ломаная в координатах + остановки с отметками пройденного пути."""

    points: List[Tuple[float, float]] = field(default_factory=list)
    cum: List[float] = field(default_factory=list)
    stops: List[StopOnRoute] = field(default_factory=list)
    total: float = 0.0

    # -- построение --------------------------------------------------------
    @classmethod
    def from_route_state(cls, state: Dict[str, Any]) -> "RouteGeometry":
        feats = _find_track_features(state)
        if not feats:
            raise DataError("в состоянии страницы маршрута нет геометрии")

        geo = cls()
        for feat in feats:
            if isinstance(feat, dict) and isinstance(feat.get("points"), list):
                for p in feat["points"]:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        geo.points.append((float(p[0]), float(p[1])))
            if isinstance(feat, dict) and feat.get("name"):
                coords = feat.get("coordinates") or []
                geo.stops.append(StopOnRoute(
                    name=feat.get("compositeName", {}).get("name") or feat["name"],
                    stop_id=feat.get("id"),
                    poly_idx=max(0, len(geo.points) - 1),
                    order=len(geo.stops),
                    lon=float(coords[0]) if len(coords) > 1 else 0.0,
                    lat=float(coords[1]) if len(coords) > 1 else 0.0,
                ))
        if len(geo.points) < 2:
            raise DataError("трек маршрута пуст")
        geo.cum = [0.0]
        for i in range(1, len(geo.points)):
            geo.cum.append(geo.cum[-1] + _dist(geo.points[i - 1], geo.points[i]))
        geo.total = geo.cum[-1]
        for st in geo.stops:
            st.arc = geo.cum[min(st.poly_idx, len(geo.cum) - 1)]
        return geo

    # -- геометрия ---------------------------------------------------------
    def project(self, lon: float, lat: float) -> Tuple[float, int]:
        """Ближайшая точка трека: (пройденный путь в метрах, индекс точки)."""
        best_i, best_d = 0, float("inf")
        for i, (px, py) in enumerate(self.points):
            d = (px - lon) ** 2 + (py - lat) ** 2
            if d < best_d:
                best_d, best_i = d, i
        return self.cum[best_i], best_i

    def project_in_range(self, lon: float, lat: float, arc_lo: float,
                         arc_hi: float) -> Optional[Tuple[float, int, float]]:
        """Ближайшая точка трека, но только из участка [arc_lo, arc_hi]."""
        best: Optional[Tuple[float, int, float]] = None
        cos_lat = math.cos(math.radians(lat))
        for i, a in enumerate(self.cum):
            if a < arc_lo:
                continue
            if a > arc_hi:
                break
            px, py = self.points[i]
            dx = (px - lon) * 111320.0 * cos_lat
            dy = (py - lat) * 110540.0
            d = math.hypot(dx, dy)
            if best is None or d < best[2]:
                best = (a, i, d)
        return best

    def tangent(self, idx: int, span: int = 6) -> Tuple[float, float]:
        """Единичный вектор касательной к треку в точке idx (направление маршрута)."""
        a = self.points[max(0, idx - span)]
        b = self.points[min(len(self.points) - 1, idx + span)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        if n < 1e-9:
            return (0.0, 0.0)
        return (dx / n, dy / n)

    def occurrences(self, stop_name: str) -> List[StopOnRoute]:
        """Все прохождения маршрута через остановку (у кольцевых — два)."""
        return [s for s in self.stops if s.name == stop_name]

    def next_occurrence(self, stop_name: str, arc: float, forward: bool) -> Optional[StopOnRoute]:
        """Ближайшее по ходу движения прохождение остановки."""
        occ = self.occurrences(stop_name)
        if not occ:
            return None
        if forward:
            after = [s for s in occ if s.arc > arc + 1.0]
            if after:
                return min(after, key=lambda s: s.arc)
            return min(occ, key=lambda s: s.arc)          # кольцо: закольцевались
        before = [s for s in occ if s.arc < arc - 1.0]
        if before:
            return max(before, key=lambda s: s.arc)
        return max(occ, key=lambda s: s.arc)

    def side_label(self, occ: StopOnRoute, min_back_m: float = 700.0) -> str:
        """Откуда подъезжает машина: идём назад по маршруту до заметной остановки."""
        stops = self.stops
        n = len(stops)
        if n == 0:
            return ""
        # ищем индекс текущего прохождения
        idx = occ.order
        acc = 0.0
        prev_arc = occ.arc
        for k in range(1, n):
            j = (idx - k) % n
            back = prev_arc - stops[j].arc
            if back < 0:
                back += self.total
            acc += back
            prev_arc = stops[j].arc
            if acc >= min_back_m:
                return stops[j].name
        return stops[idx % n].name

    def back_chain(self, occ: StopOnRoute, max_back_m: float = 2000.0,
                   max_stops: int = 8) -> List[StopOnRoute]:
        """Остановки перед проходом (от ближайшей к самой дальней)."""
        stops = self.stops
        n = len(stops)
        out: List[StopOnRoute] = []
        acc = 0.0
        prev_arc = occ.arc
        for k in range(1, n):
            j = (occ.order - k) % n
            back = prev_arc - stops[j].arc
            if back < 0:
                back += self.total
            acc += back
            prev_arc = stops[j].arc
            out.append(stops[j])
            if acc >= max_back_m or len(out) >= max_stops:
                break
        return out

    def pass_score(self, occ: StopOnRoute,
                   positions: Sequence[Tuple[float, float]]) -> float:
        """Насколько пройденный путь машины похож на этот проход (меньше — лучше).

        Первые остановки цепочки (Улица Строителей, Мечеть Булгар) у обоих
        проходов общие — машина идёт по одной и той же улице. Поэтому
        сравниваем только по «своим» остановкам ветки.
        """
        chain = [s for s in self.back_chain(occ)[SHARED_CHAIN_STOPS:]
                 if s.lon or s.lat]
        if not chain or not positions:
            return float("inf")
        total = 0.0
        for lon, lat in positions:
            total += min(_dist((lon, lat), (s.lon, s.lat)) for s in chain)
        return total / len(positions)

    def approach(self, occ: StopOnRoute, min_back_m: float = 700.0) -> Tuple[str, str]:
        """Откуда подъезжает машина и куда поедет после остановки.

        Возвращает (ориентир_откуда, направление_куда). Для кольцевого маршрута
        ориентир ищем по ветке, характерной для конкретного прохода.
        """
        chain = self.back_chain(occ, max_back_m=max(min_back_m * 3.0, 2000.0))
        if not chain:
            return "", ""
        next_stop = self.dest_after(occ)
        for keyword in SIDE_KEYWORDS:
            for st in chain:
                if keyword in st.name.lower():
                    side_name = st.name
                    direction = direction_label("со стороны «%s»" % side_name, next_stop) or ""
                    return side_name, direction
        side_name = chain[0].name
        direction = direction_label("со стороны «%s»" % side_name, next_stop) or ""
        return side_name, direction

    def dest_after(self, occ: StopOnRoute) -> str:
        """Куда машина едет дальше (следующая остановка с другим названием)."""
        stops = self.stops
        n = len(stops)
        for k in range(1, n):
            j = (occ.order + k) % n
            if stops[j].name != occ.name:
                return stops[j].name
        return ""

    def distance_between(self, a: float, b: float) -> float:
        d = abs(b - a)
        return min(d, self.total - d) if self.total else d


def _dist(a: Sequence[float], b: Sequence[float]) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = math.radians(b[0] - a[0])
    x = dlon * math.cos((lat1 + lat2) / 2.0)
    return R_EARTH * math.hypot(x, dlat)


def _find_track_features(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ищет в стейте список фич с геометрией маршрута (features c points/name)."""
    best: Optional[List[Dict[str, Any]]] = None

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal best
        if depth > 8 or best is not None or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            if len(node) > 20 and all(isinstance(x, dict) for x in node):
                sample = node[0]
                if isinstance(sample, dict) and ("points" in sample or ("name" in sample and "id" in sample)):
                    best = node
                    return
            for child in node:
                walk(child, depth + 1)
        else:
            for value in node.values():
                walk(value, depth + 1)

    walk(state)
    return best or []


# --------------------------------------------------------------------------
# Модели
# --------------------------------------------------------------------------

@dataclass
class Arrival:
    """Одно прибытие маршрутки на остановку."""
    route: str
    kind: str            # minibus / bus / trolleybus / tram
    ts: int              # unix-время прибытия
    source: str          # Estimated (живой прогноз) или Scheduled (расписание)
    vehicle_id: Optional[str] = None
    dest: Optional[str] = None          # «до ЧПО имени Чапаева»
    side: Optional[str] = None          # «со стороны Ярмарки»
    next_stop: Optional[str] = None
    origin: str = "yandex"              # yandex / geo (посчитали сами)
    live: bool = False                  # машину видели в эфире (есть GPS)

    @property
    def minutes(self) -> float:
        return (self.ts - time.time()) / 60.0


@dataclass
class Vehicle:
    """Живая машина из слоя транспорта."""
    vid: str
    route: str
    line_id: str
    lon: float
    lat: float
    vx: float = 0.0
    vy: float = 0.0
    speed_mps: float = 0.0
    traj: List[Tuple[float, float]] = field(default_factory=list)  # след движения, м


# --------------------------------------------------------------------------
# Источник данных: Яндекс.Карты
# --------------------------------------------------------------------------

class Yandex:
    def __init__(self, region_id: str, city: str, stop_id: str, timeout: int = 30):
        self.region_id = str(region_id)
        self.city = city
        self.stop_id = stop_id
        self.timeout = timeout
        self.tz_offset = 3 * 3600
        self.stop_name = ""
        self._stop_state: Optional[Dict[str, Any]] = None

    # -- адреса ------------------------------------------------------------
    @property
    def stop_url(self) -> str:
        return "https://yandex.ru/maps/%s/%s/stops/%s/" % (self.region_id, self.city, self.stop_id)

    @property
    def transport_url(self) -> str:
        return "https://yandex.ru/maps/%s/%s/transport/" % (self.region_id, self.city)

    def transport_kind_url(self, kind: str) -> str:
        """Страница транспорта по типу. Маршрутки и автобусы лежат в разных срезах,
        и на общей странице часть машин не попадает — а нам нужны именно маршрутки."""
        slug = {"minibus": "minibuses", "bus": "buses", "trolleybus": "trolleybuses",
                "tram": "trams", "train": "trains"}.get(kind)
        if not slug:
            return self.transport_url
        return "https://yandex.ru/maps/%s/%s/transport/%s/" % (self.region_id, self.city, slug)

    def route_url(self, uri: str, kind: str, name: str) -> str:
        blob = uri.encode("utf-8").hex()
        return "https://yandex.ru/maps/%s/%s/routes/%s_%s/%s/" % (
            self.region_id, self.city, kind, name, blob)

    # -- страницы ----------------------------------------------------------
    def stop_state(self, refresh: bool = False) -> Dict[str, Any]:
        if self._stop_state is None or refresh:
            state = fetch_state(self.stop_url, self.timeout)
            cfg = state.get("config", {})
            region = cfg.get("landing", {}).get("region", {})
            if region.get("regionId"):
                self.region_id = str(region["regionId"])
            if region.get("seoname"):
                self.city = region["seoname"]
            stop = cfg.get("masstransitStop", {})
            self.stop_name = stop.get("name", "")
            if stop.get("tzOffset") is not None:
                self.tz_offset = int(stop["tzOffset"])
            self._stop_state = state
        return self._stop_state

    def transport_state(self) -> Dict[str, Any]:
        return fetch_state(self.transport_url, self.timeout)

    def route_state(self, uri: str, kind: str, name: str) -> Dict[str, Any]:
        return fetch_state(self.route_url(uri, kind, name), self.timeout)

    def local_now(self) -> datetime:
        """Локальное время города (не зависит от таймзоны VPS)."""
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=self.tz_offset)


# --------------------------------------------------------------------------
# Основная логика
# --------------------------------------------------------------------------

class Tracker:
    def __init__(self, cfg: "Config"):
        self.cfg = cfg
        self.yandex = Yandex(cfg.region_id, cfg.city, cfg.stop_id, cfg.timeout)
        self._geo: Dict[str, RouteGeometry] = {}
        self._geo_at: Dict[str, float] = {}
        self._veh_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        # история движения машин между опросами: нужна, чтобы понять, по какой
        # ветке кольцевого маршрута едет конкретная машина
        self._veh_hist: Dict[str, List[Tuple[float, float, float]]] = {}
        # последний удачный прогноз: запасной источник, если Яндекс не отвечает
        self._last_arrivals: List[Arrival] = []
        # запомненное направление по рейсу: если однажды определили уверенно,
        # не теряем его при временной потере live-сопоставления
        self._known_dir: Dict[str, Dict[str, Any]] = {}
        self.last_error: Optional[str] = None

    # -- остановка: прогноз прибытия --------------------------------------
    def stop_arrivals(self) -> List[Arrival]:
        state = self.yandex.stop_state(refresh=True)
        stop = state.get("config", {}).get("masstransitStop")
        if not stop:
            raise DataError("на странице остановки нет данных masstransitStop")
        out: List[Arrival] = []
        seen: set = set()
        for t in stop.get("transports", []):
            route = str(t.get("name", ""))
            if route not in self.cfg.routes:
                continue
            kind = t.get("type", "")
            for thread in t.get("threads", []):
                dest = _thread_destination(thread)
                events = thread.get("BriefSchedule", {}).get("Events", []) or []
                key = (thread.get("threadId"), tuple(
                    (e.get("Estimated") or e.get("Scheduled") or {}).get("value") for e in events))
                if key in seen:          # Яндекс дублирует кольцевые маршруты
                    continue
                seen.add(key)
                for ev in events:
                    est, sch = ev.get("Estimated"), ev.get("Scheduled")
                    node = est or sch
                    if not node:
                        continue
                    try:
                        ts = int(node["value"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    out.append(Arrival(
                        route=route, kind=kind, ts=ts,
                        source="Estimated" if est else "Scheduled",
                        vehicle_id=ev.get("vehicleId"),
                        dest=None,  # направление определим отдельно; thread destination на кольце вводит в заблуждение
                    ))
        out.sort(key=lambda a: a.ts)
        return out

    # -- живой транспорт ---------------------------------------------------
    def _transport_state(self, kind: str) -> Optional[Dict[str, Any]]:
        """Состояние слоя транспорта (с кэшем на время одного опроса)."""
        if kind in self._veh_cache:
            return self._veh_cache[kind]
        url = self.yandex.transport_kind_url(kind) if kind else self.yandex.transport_url
        try:
            state = fetch_state(url, self.cfg.timeout)
        except DataError as exc:
            log("не удалось получить живой транспорт (%s): %s" % (kind or "all", exc), "WARN")
            state = None
        self._veh_cache[kind] = state
        return state

    def vehicles(self, line_ids: Sequence[str], kind: str = "") -> List[Vehicle]:
        """Машины в эфире. Данные берём со страницы нужного типа транспорта:
        на общей странице /transport/ часть машин (в т.ч. маршруток) не попадает."""
        wanted = set(line_ids)
        if not wanted:
            return []
        state = self._transport_state(kind)
        if state is None:
            return []
        out: List[Vehicle] = []
        for v in state.get("masstransit", {}).get("vehicles", []) or []:
            meta = v.get("properties", {}).get("VehicleMetaData", {})
            tr = meta.get("Transport", {})
            if str(tr.get("lineId")) not in wanted:
                continue
            segs = v.get("features") or []
            if not segs:
                continue
            last = segs[-1].get("geometry", {}).get("coordinates") or []
            if not last:
                continue
            lon, lat = float(last[-1][0]), float(last[-1][1])
            vx = vy = 0.0
            speed = 0.0
            # скорость и направление — по траектории машины
            seg_speeds: List[float] = []
            prev_pt = None
            first_pt = None
            last_dir = None          # направление последнего значимого участка
            for seg in segs:
                coords = seg.get("geometry", {}).get("coordinates") or []
                meta_t = seg.get("properties", {}).get("TrajectorySegmentMetaData", {})
                dur = float(meta_t.get("duration") or 0)
                seg_dist = 0.0
                seg_first = None
                seg_last = None
                for pt in coords:
                    p = (float(pt[0]), float(pt[1]))
                    if prev_pt is not None:
                        seg_dist += _dist(prev_pt, p)
                    if first_pt is None:
                        first_pt = p
                    if seg_first is None:
                        seg_first = p
                    seg_last = p
                    prev_pt = p
                if dur >= 5 and seg_dist > 5:
                    seg_speeds.append(seg_dist / dur)
                if seg_first and seg_last:
                    ddx, ddy = seg_last[0] - seg_first[0], seg_last[1] - seg_first[1]
                    if math.hypot(ddx, ddy) > 0.00005:   # ~5 м — не шум GPS
                        last_dir = (ddx, ddy)
            if seg_speeds:
                seg_speeds.sort()
                speed = seg_speeds[len(seg_speeds) // 2]      # медиана: без промахов от светофоров
            # направление берём из последнего участка траектории — он отражает
            # текущее движение, а не весь путь машины за последние минуты
            if last_dir:
                vx, vy = last_dir
            elif prev_pt and first_pt:
                vx, vy = prev_pt[0] - first_pt[0], prev_pt[1] - first_pt[1]
            n = math.hypot(vx, vy)
            if n > 0:
                vx, vy = vx / n, vy / n
            speed = max(2.0, min(speed, 15.0)) if speed else 0.0
            out.append(Vehicle(vid=meta.get("id", ""), route=str(tr.get("name", "")),
                               line_id=str(tr.get("lineId", "")), lon=lon, lat=lat,
                               vx=vx, vy=vy, speed_mps=speed,
                               traj=[(float(p[0]), float(p[1]))
                                     for seg in segs
                                     for p in (seg.get("geometry", {}).get("coordinates") or [])]))
        return out

    # -- геометрия маршрута ------------------------------------------------
    def geometry(self, route: str) -> Optional[RouteGeometry]:
        now = time.time()
        if route in self._geo and now - self._geo_at.get(route, 0) < self.cfg.geo_ttl:
            return self._geo[route]
        try:
            state = self.yandex.stop_state()
            transports = state.get("config", {}).get("masstransitStop", {}).get("transports", [])
            tr = next((t for t in transports if str(t.get("name")) == route), None)
            if not tr or not tr.get("uri"):
                return None
            rstate = self.yandex.route_state(tr["uri"], tr.get("type", "bus"), route)
            geo = RouteGeometry.from_route_state(rstate)
            self._geo[route] = geo
            self._geo_at[route] = now
            log("геометрия маршрута %s: %.1f км, остановок %d" % (route, geo.total / 1000.0, len(geo.stops)), "DEBUG")
            return geo
        except DataError as exc:
            log("не удалось взять геометрию маршрута %s: %s" % (route, exc), "WARN")
            return None

    # -- определение направления ------------------------------------------
    def enrich(self, arrivals: List[Arrival]) -> List[Arrival]:
        """Дополняет прогноз направлением движения (куда поедет после остановки).

        Правило: если направление нельзя определить уверенно, прибытие всё равно
        показывается/отправляется с временем, но без угадывания стороны.
        """
        if not arrivals:
            return arrivals
        by_route: Dict[str, List[Arrival]] = {}
        for a in arrivals:
            by_route.setdefault(a.route, []).append(a)

        for route, arrs in by_route.items():
            geo = self.geometry(route)
            if geo is None:
                self._clear_direction(arrs)
                continue
            occ = geo.occurrences(self.yandex.stop_name)
            if not occ:
                # остановка может называться иначе, чем в стейте маршрута
                occ = [o for o in geo.stops if _similar(o.name, self.yandex.stop_name)]
            if not occ:
                self._clear_direction(arrs)
                continue

            branch_labels = {}
            for o in occ:
                from_side, travel_dir = geo.approach(o, self.cfg.side_min_back_m)
                branch_labels[o.arc] = {
                    "occ": o,
                    "from_side": ("со стороны «%s»" % from_side) if from_side else "",
                    "travel_dir": travel_dir or "",
                    "next_stop": geo.dest_after(o),
                }

            line_ids = self._line_ids(route)
            vehicles = self.vehicles(line_ids, arrs[0].kind if arrs else "")
            info = self._vehicle_positions(geo, occ, vehicles)

            # 1) прямое совпадение по id машины
            used_v: set = set()
            for a in arrs:
                if a.vehicle_id and a.vehicle_id in info:
                    self._apply_branch(a, info[a.vehicle_id], branch_labels, live=True)
                    used_v.add(a.vehicle_id)

            # 2) сопоставление по времени — только когда есть один явный кандидат
            for ai, a in enumerate(arrs):
                if a.dest or a.side:
                    continue
                candidates = []
                for vid, v in info.items():
                    if vid in used_v:
                        continue
                    dt = abs(v["eta_ts"] - a.ts)
                    candidates.append((dt, vid, v))
                if not candidates:
                    continue
                candidates.sort(key=lambda x: x[0])
                best_dt, best_vid, best_v = candidates[0]
                tol = max(self.cfg.match_tolerance_min, 0.3 * max(1.0, abs(a.minutes))) * 60.0
                if best_dt > tol:
                    continue
                # не угадываем, если есть другой близкий кандидат на другой проход
                ambiguous = False
                if len(candidates) > 1:
                    second_dt, _, second_v = candidates[1]
                    if second_v.get("occ_arc") != best_v.get("occ_arc") and (second_dt - best_dt) < 90.0:
                        ambiguous = True
                if ambiguous:
                    continue
                self._apply_branch(a, best_v, branch_labels, live=True)
                used_v.add(best_vid)

            # направление не определено уверенно — пробуем восстановить ранее известное для этого рейса
            for a in arrs:
                if a.dest and a.dest.startswith("в сторону "):
                    self._remember_direction(a)
                elif not self._restore_direction(a):
                    self._clear_direction([a])
                else:
                    # нормализуем мусорные поля, но сохраняем восстановленное направление
                    if a.side and not a.side.startswith("со стороны "):
                        a.side = ""
        return arrivals

    def _apply_branch(self, arrival: Arrival, veh_info: Dict[str, Any],
                      branch_labels: Dict[float, Dict[str, Any]], live: bool) -> None:
        lbl = branch_labels.get(veh_info.get("occ_arc")) or {}
        arrival.side = lbl.get("from_side") or veh_info.get("side") or ""
        arrival.dest = lbl.get("travel_dir") or veh_info.get("dest") or ""
        arrival.next_stop = lbl.get("next_stop") or veh_info.get("next_stop")
        if live:
            arrival.origin = "yandex+geo"
            arrival.live = True

    def _remember_direction(self, arrival: Arrival) -> None:
        if not (arrival.dest and arrival.dest.startswith("в сторону ")):
            return
        now = time.time()
        dir_key = (arrival.dest, arrival.side or "", arrival.next_stop or "")
        keys = self._dir_keys(arrival)
        for k in keys:
            item = self._known_dir.get(k)
            if not item:
                item = {"route": arrival.route, "ts": arrival.ts, "seen": now, "counts": {}}
                self._known_dir[k] = item
            counts = item.setdefault("counts", {})
            # миграция со старого формата (одиночное направление)
            if not counts and item.get("dest"):
                old_key = (item.get("dest"), item.get("side") or "", item.get("next_stop") or "")
                counts[old_key] = int(item.get("count") or 1)
            counts[dir_key] = int(counts.get(dir_key, 0)) + 1
            item["route"] = arrival.route
            item["ts"] = arrival.ts
            item["seen"] = now
        # чистим устаревшие записи
        stale_before = now - 3600
        for k, v in list(self._known_dir.items()):
            if float(v.get("seen") or 0) < stale_before:
                self._known_dir.pop(k, None)

    def _dir_keys(self, arrival: Arrival) -> List[str]:
        keys = []
        if arrival.vehicle_id:
            keys.append("vid:%s:%s" % (arrival.route, arrival.vehicle_id))
        # запасной ключ по маршруту и расчётному времени прибытия (допуск на "плавание" ETA)
        keys.append("ts:%s:%s" % (arrival.route, int(round(arrival.ts / 60.0))))
        return keys

    def _pick_majority_direction(self, item: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
        counts = item.get("counts") or {}
        if not counts and item.get("dest"):
            return (item.get("dest"), item.get("side") or "", item.get("next_stop") or "")
        if not counts:
            return None
        # направление, которое встречалось чаще всего; при равенстве — более свежее по ts контекста не храним отдельно,
        # поэтому оставляем детерминированный выбор
        best = max(counts.items(), key=lambda kv: (int(kv[1]), kv[0][0]))
        d, s, n = best[0]
        if not d:
            return None
        return (d, s, n)

    def _restore_direction(self, arrival: Arrival) -> bool:
        if arrival.dest and arrival.dest.startswith("в сторону "):
            return True
        candidates = []
        for k in self._dir_keys(arrival):
            item = self._known_dir.get(k)
            if not item:
                continue
            if abs(int(item.get("ts") or 0) - int(arrival.ts)) > 20 * 60:
                continue
            pick = self._pick_majority_direction(item)
            if pick:
                counts = item.get("counts") or {}
                score = int(counts.get(pick, 1)) if counts else 1
                candidates.append((score, pick))
        if not candidates:
            # дополнительный поиск: тот же маршрут и близкое время прибытия
            for item in self._known_dir.values():
                if item.get("route") != arrival.route:
                    continue
                if abs(int(item.get("ts") or 0) - int(arrival.ts)) <= 6 * 60:
                    pick = self._pick_majority_direction(item)
                    if pick:
                        counts = item.get("counts") or {}
                        score = int(counts.get(pick, 1)) if counts else 1
                        candidates.append((score, pick))
        if not candidates:
            return False
        # выбираем направление с наибольшим числом наблюдений среди подходящих записей
        candidates.sort(key=lambda x: x[0], reverse=True)
        dest, side, next_stop = candidates[0][1]
        arrival.side = arrival.side or side or ""
        arrival.dest = dest or arrival.dest
        if not arrival.next_stop:
            arrival.next_stop = next_stop
        return bool(arrival.dest)

    def _clear_direction(self, arrivals: List[Arrival]) -> None:
        for a in arrivals:
            # конечная из треда/расписания на кольце не равна "куда поедет после остановки"
            a.side = a.side or ""
            a.dest = a.dest or ""
            # если это не наше нормализованное направление, не оставляем мусор
            if a.dest and not a.dest.startswith("в сторону "):
                a.dest = ""
            if a.side and not a.side.startswith("со стороны "):
                # side должен быть ориентиром "со стороны ..."; иначе очищаем
                a.side = ""

    def _line_ids(self, route: str) -> List[str]:
        state = self.yandex.stop_state()
        transports = state.get("config", {}).get("masstransitStop", {}).get("transports", [])
        return [str(t.get("lineId")) for t in transports if str(t.get("name")) == route and t.get("lineId")]

    def _vehicle_positions(self, geo: RouteGeometry, occ: List[StopOnRoute],
                           vehicles: List[Vehicle]) -> Dict[str, Dict[str, Any]]:
        """Считает для каждой машины: где она на маршруте, куда едет и когда подъедет."""
        out: Dict[str, Dict[str, Any]] = {}
        for veh in vehicles:
            # на кольцевом маршруте остановку проходят дважды, а участок трека
            # в этом месте продублирован. Поэтому ветку (каким проходом едет
            # машина) выбираем по её собственному пути, а проекцию делаем уже
            # внутри выбранной ветки.
            branch = self._pick_branch(geo, occ, veh)
            if branch is None:
                continue
            proj = geo.project_in_range(veh.lon, veh.lat,
                                        branch.arc - 6000.0, branch.arc)
            if proj is None:
                continue
            arc, idx, _ = proj
            tx, ty = geo.tangent(idx)
            forward = (veh.vx * tx + veh.vy * ty) >= 0 if (veh.vx or veh.vy) else True
            dist = geo.distance_between(arc, branch.arc)
            speed = veh.speed_mps or self.cfg.default_speed_mps
            eta_ts = time.time() + dist / max(speed, 1.0)
            side_name, direction = geo.approach(branch, self.cfg.side_min_back_m)
            out[veh.vid] = {
                "side": ("со стороны «%s»" % side_name) if side_name else "",
                "dest": direction,
                "next_stop": geo.dest_after(branch),
                "eta_ts": eta_ts,
                "arc": arc,
                "occ_arc": branch.arc,
                "forward": forward,
                "dist": dist,
            }
        return out

    def _pick_branch(self, geo: RouteGeometry, occ: List[StopOnRoute],
                     veh: Vehicle) -> Optional[StopOnRoute]:
        """Выбирает, по какому проходу остановки едет машина.

        У кольцевого маршрута остановку проходят дважды, и в треке Яндекса
        участок рядом с ней продублирован — по текущей точке ветку не
        определить. Ориентируемся на пройденный машиной путь: какая цепочка
        остановок ей ближе. Путь собираем из траектории Яндекса и из нашей
        истории между опросами — так ветка определяется даже когда машина
        уже подъехала близко.
        """
        if not occ:
            return None
        if len(occ) == 1:
            return occ[0]
        pts = self._vehicle_path(veh)
        if not pts:
            return None
        scored = sorted((geo.pass_score(o, pts), i) for i, o in enumerate(occ))
        best_i = scored[0][1]
        if len(scored) > 1 and scored[1][0] - scored[0][0] < 80.0:
            # ветки неразличимы (машина уже рядом с остановкой) — берём ту,
            # до которой ехать ближе по дуге трека
            here = geo.project(veh.lon, veh.lat)[0]
            best_i = min(range(len(occ)),
                         key=lambda i: geo.distance_between(here, occ[i].arc))
        return occ[best_i]

    def _vehicle_path(self, veh: Vehicle) -> List[Tuple[float, float]]:
        """Полный путь машины: история наших опросов + свежая траектория Яндекса."""
        now = time.time()
        hist = self._veh_hist.setdefault(veh.vid, [])
        for lon, lat in veh.traj:
            if not hist or _dist(hist[-1][1:], (lon, lat)) > 5.0:
                hist.append((now, lon, lat))
        hist.append((now, veh.lon, veh.lat))
        # чистим историю: не older 10 минут и не больше 200 точек
        while hist and (now - hist[0][0] > 600.0 or len(hist) > 200):
            hist.pop(0)
        return [(lon, lat) for _, lon, lat in hist]

    def _match_by_time(self, geo: RouteGeometry, occ: List[StopOnRoute],
                       info: Dict[str, Dict[str, Any]], a: Arrival) -> Optional[Dict[str, Any]]:
        """Нет id машины — подбираем по времени прибытия."""
        best, best_dt = None, 1e9
        for v in info.values():
            dt = abs(v["eta_ts"] - a.ts)
            if dt < best_dt:
                best_dt, best = dt, v
        if best is not None and best_dt <= self.cfg.match_tolerance_min * 60:
            return best
        return None


    # -- живые машины: прибытия из эфира ----------------------------------
    def _live_arrivals(self, kind: str, route: str) -> List["Arrival"]:
        """Собирает прибытия из живых машин (по ETA через геометрию, как на карте).

        Если живые машины видны, но парырование с расписанием не сработало —
        используем эти ETA: они с корректной стороны и времени.
        """
        geo = self.geometry(route)
        if geo is None:
            return []
        occ = geo.occurrences(self.yandex.stop_name)
        if not occ:
            occ = [o for o in geo.stops if _similar(o.name, self.yandex.stop_name)]
        if not occ:
            return []
        line_ids = self._line_ids(route)
        vehicles = self.vehicles(line_ids, kind)
        info = self._vehicle_positions(geo, occ, vehicles)
        out: List[Arrival] = []
        for vid, v in info.items():
            eta_ts = float(v.get("eta_ts") or 0)
            if eta_ts <= time.time():
                continue
            out.append(Arrival(
                route=route, kind=kind, ts=int(round(eta_ts)), source="Estimated",
                vehicle_id=vid, dest=v.get("dest"), side=v.get("side"),
                next_stop=v.get("next_stop"), live=True, origin="yandex+geo"))
        out.sort(key=lambda a: a.ts)
        return out

    # -- полный опрос ------------------------------------------------------
    def poll(self) -> List[Arrival]:
        self._veh_cache = {}
        try:
            arrivals = self.stop_arrivals()
            arrivals = self.enrich(arrivals)
        except Exception as exc:  # noqa: BLE001
            # запасной источник: показываем последний удачный прогноз, а не пустоту.
            if self._last_arrivals:
                self.last_error = str(exc)
                cached = list(self._last_arrivals)
                # при использовании кэша не утверждаем "живость", но сохраняем время/направление
                for a in cached:
                    if a.source == "Estimated":
                        a.source = "Scheduled"
                    a.live = False
                return cached
            raise
        # оставляем немного "просроченные" прибытия: маршрутка может опаздывать,
        # но ещё ехать к остановке (иначе запись исчезает слишком рано)
        arrivals = [a for a in arrivals if a.ts > time.time() - 10 * 60]
        self._last_arrivals = list(arrivals)
        self.last_error = None
        return arrivals


def _similar(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _thread_destination(thread: Dict[str, Any]) -> Optional[str]:
    """Из EssentialStops понимаем, куда идёт маршрутка от этой остановки."""
    ess = thread.get("EssentialStops") or []
    if not ess:
        return None
    for st in ess:
        if st.get("info", {}).get("lastStop"):
            return "до «%s»" % (st.get("compositeName", {}).get("name") or st.get("name"))
    for st in ess:
        if st.get("info", {}).get("firstStop"):
            continue
        return "до «%s»" % (st.get("compositeName", {}).get("name") or st.get("name"))
    return None


# Ориентиры, по которым узнаём ветку кольцевого маршрута №5.
# Проверяются по порядку: первое совпадение и определяет проход/направление.
SIDE_KEYWORDS = ("пригородный автовокзал", "лакреевский лес", "ярмарка",
                 "госархив", "улица гладкова", "тк центральный")

# Сколько первых остановок цепочки у обоих проходов общие (одна и та же улица).
SHARED_CHAIN_STOPS = 2


def direction_label(side: Optional[str], next_stop: Optional[str] = None) -> Optional[str]:
    """Куда поедет маршрутка после остановки (направление движения).

    Для кольцевого №5 в Чебоксарах на «Медицинском центре» важны два прохода:
    - подъезд со стороны Пригородного автовокзала/Госархива -> в сторону ЮЗР
    - подъезд со стороны Лакреевского леса/Ярмарки -> в сторону НЮР
    """
    s = (side or "").lower()
    n = (next_stop or "").lower()
    # едет со стороны Пригородного автовокзала/Госархива -> дальше в сторону ЮЗР
    if "пригород" in s or "автовокзал" in s or "госархив" in s:
        return "в сторону ЮЗР"
    # едет со стороны Лакреевского леса/Ярмарки -> дальше в сторону НЮР
    if "лакре" in s or "ярмар" in s:
        return "в сторону НЮР"
    # запасной вариант: по следующей остановке после Медцентра
    if "лакре" in n or "ярмар" in n or "центральн" in n or "гладков" in n:
        return "в сторону ЮЗР"
    if "пригород" in n or "автовокзал" in n or "таможн" in n or "бичурин" in n:
        return "в сторону НЮР"
    return None


# --------------------------------------------------------------------------
# Уведомления (ntfy)
# --------------------------------------------------------------------------

class Ntfy:
    def __init__(self, server: str, topic: str, token: str = "", timeout: int = 15, dry_run: bool = False):
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.dry_run = dry_run

    def send(self, title: str, message: str, priority: str = "default", tags: Optional[List[str]] = None,
             click: str = "") -> bool:
        if self.dry_run:
            log("[DRY-RUN] %s — %s" % (title, message), "INFO")
            return True
        payload: Dict[str, Any] = {
            "topic": self.topic,
            "title": title,
            "message": message,
            # в JSON-API ntfy приоритет — число, а не строка
            "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}.get(priority, 3),
        }
        if tags:
            payload["tags"] = tags
        if click:
            payload["click"] = click
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        req = urllib.request.Request(self.server + "/", data=data, headers=headers, method="POST")
        last_exc = None
        for attempt in range(3):
            try:
                with prefer_ipv4():
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        ok = 200 <= resp.status < 300
                if not ok:
                    log("ntfy ответил %s" % resp.status, "ERROR")
                return ok
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                log("ntfy ответил %s: %s" % (exc.code, body), "ERROR")
                return False
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < 2:
                    delay = 2 * (attempt + 1)
                    log("ntfy: попытка %d не удалась (%s), повтор через %d с" % (
                        attempt + 1, exc, delay), "WARN")
                    time.sleep(delay)
        log("не удалось отправить уведомление: %s" % last_exc, "ERROR")
        return False

    def test(self) -> bool:
        ok = self.send("Тест %s" % APP, "Канал уведомлений настроен, всё работает.", priority="default")
        if ok:
            log("тестовое уведомление отправлено — проверьте телефон")
        return ok


# --------------------------------------------------------------------------
# Конфиг и планировщик
# --------------------------------------------------------------------------

@dataclass
class Config:
    region_id: str = "45"
    city: str = "cheboksary"
    stop_id: str = "stop__10094747"
    routes: List[str] = field(default_factory=lambda: ["5"])
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = "marshrutka-cheboksary-5"
    ntfy_token: str = ""
    interval: int = 60
    window_from: str = "05:30"
    window_to: str = "10:00"
    weekdays_only: bool = False
    window_by_weekday: Dict[str, Any] = field(default_factory=dict)  # {"1": ["08:30", "09:10"], ...}
    notify_at: List[int] = field(default_factory=lambda: [15, 10, 5, 2])
    notify_repeat_minutes: int = 1  # не чаще чем раз в N минут для одной поездки (1 = как сейчас)
    notify_mode: str = "repeat"     # repeat = повторять с интервалом, once = один раз на порог за поездку
    walk_minutes: int = 0          # если ETA <= этого, писать «пора выходить» (0 = выключить)
    route_settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # переопределения по маршрутам
    side_min_back_m: float = 700.0
    default_speed_mps: float = 8.0
    match_tolerance_min: float = 4.0
    geo_ttl: int = 1800
    timeout: int = 30
    dry_run: bool = False
    verbose: bool = False
    test_notify: bool = False
    config_path: str = ""
    snapshot_path: str = ""     # куда писать снимок прогноза (пусто = рядом с конфигом)


def parse_hhmm(value: str) -> Tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not m:
        raise argparse.ArgumentTypeError("ожидается ЧЧ:ММ, получено %r" % value)
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise argparse.ArgumentTypeError("некорректное время %r" % value)
    return h, mi


def in_window(now: datetime, start: Tuple[int, int], end: Tuple[int, int], weekdays_only: bool = False) -> bool:
    if weekdays_only and now.weekday() >= 5:  # 5=сб, 6=вс
        return False
    cur = now.hour * 60 + now.minute
    s, e = start[0] * 60 + start[1], end[0] * 60 + end[1]
    if s <= e:
        return s <= cur <= e
    return cur >= s or cur <= e          # окно через полночь


DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def day_index(key: Any) -> Optional[int]:
    """Номер дня недели по ключу: 0=пн … 6=вс, либо «mon»…«sun», либо «0»…«6»."""
    k = str(key if key is not None else "").strip().lower()
    if not k:
        return None
    if k in DAY_NAMES:
        return DAY_NAMES[k]
    if k.isdigit():
        return int(k) % 7
    return None


def effective_window(cfg: "Config", now: datetime) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Окно слежения для конкретного дня: берём перегрузку из window_by_weekday,
    а если её нет — общие window_from/window_to."""
    start = parse_hhmm(cfg.window_from)
    end = parse_hhmm(cfg.window_to)
    by_day = getattr(cfg, "window_by_weekday", None) or {}
    entry = None
    for dk, dv in (by_day or {}).items():
        if day_index(dk) == now.weekday():
            entry = dv
            break
    pair = None
    if isinstance(entry, dict):
        pair = [entry.get("from") or entry.get("start") or entry.get("с"),
                entry.get("to") or entry.get("end") or entry.get("до")]
    elif isinstance(entry, (list, tuple)):
        pair = [entry[0] if len(entry) > 0 else None, entry[1] if len(entry) > 1 else None]
    if pair and str(pair[0] or "").strip() and str(pair[1] or "").strip():
        try:
            start = parse_hhmm(str(pair[0]))
            end = parse_hhmm(str(pair[1]))
        except Exception:  # noqa: BLE001
            pass
    return start, end


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    cfg.config_path = getattr(args, "config", None) or ""
    if cfg.config_path:
        # utf-8-sig: Notepad и PowerShell любят добавлять BOM
        with open(cfg.config_path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        apply_settings(cfg, data)
    # аргументы командной строки важнее файла
    for key in ("region_id", "city", "stop_id", "ntfy_server", "ntfy_topic", "ntfy_token",
                "interval", "window_from", "window_to", "side_min_back_m", "default_speed_mps",
                "match_tolerance_min", "geo_ttl", "timeout", "walk_minutes", "notify_repeat_minutes"):
        val = getattr(args, key, None)
        if val is not None:
            setattr(cfg, key, val)
    if getattr(args, "routes", None):
        cfg.routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    if getattr(args, "notify_at", None):
        cfg.notify_at = sorted({int(x) for x in args.notify_at.split(",") if x.strip()}, reverse=True)
    if getattr(args, "notify_repeat_minutes", None) is not None:
        cfg.notify_repeat_minutes = int(args.notify_repeat_minutes)
    if getattr(args, "dry_run", False):
        cfg.dry_run = True
    if getattr(args, "verbose", False):
        cfg.verbose = True
    if getattr(args, "test_notify", False):
        cfg.test_notify = True
    _apply_env_secrets(cfg)
    normalize(cfg)
    return cfg


def _apply_env_secrets(cfg: Config) -> None:
    """Секреты из переменных окружения имеют приоритет над config.json."""
    token = os.environ.get("MARSHRUTKA_NTFY_TOKEN") or os.environ.get("NTFY_TOKEN")
    if token is not None and str(token).strip() != "":
        cfg.ntfy_token = str(token).strip()
    topic = os.environ.get("MARSHRUTKA_NTFY_TOPIC") or os.environ.get("NTFY_TOPIC")
    if topic is not None and str(topic).strip() != "":
        cfg.ntfy_topic = str(topic).strip()
    server = os.environ.get("MARSHRUTKA_NTFY_SERVER") or os.environ.get("NTFY_SERVER")
    if server is not None and str(server).strip() != "":
        cfg.ntfy_server = str(server).strip()


def apply_settings(cfg: Config, data: Dict[str, Any]) -> None:
    """Переносит поля из JSON-файла в конфиг (используется и при горячей перезагрузке)."""
    for key, value in data.items():
        if key != "config_path" and hasattr(cfg, key):
            setattr(cfg, key, value)


def normalize(cfg: Config) -> None:
    # пороги — по убыванию, без дублей
    try:
        cfg.notify_at = sorted({int(x) for x in cfg.notify_at if int(x) >= 0}, reverse=True)
    except (TypeError, ValueError):
        cfg.notify_at = [15, 10, 5, 2]
    if isinstance(cfg.routes, str):
        cfg.routes = [r.strip() for r in cfg.routes.split(",") if r.strip()]
    if isinstance(cfg.weekdays_only, str):
        cfg.weekdays_only = cfg.weekdays_only.strip().lower() in ("1", "true", "yes", "on")
    else:
        cfg.weekdays_only = bool(cfg.weekdays_only)
    try:
        cfg.notify_repeat_minutes = int(cfg.notify_repeat_minutes)
    except (TypeError, ValueError):
        cfg.notify_repeat_minutes = 1
    if cfg.notify_repeat_minutes < 1:
        cfg.notify_repeat_minutes = 1
    if cfg.notify_repeat_minutes > 180:
        cfg.notify_repeat_minutes = 180
    mode = str(getattr(cfg, "notify_mode", "repeat") or "repeat").strip().lower()
    if mode in ("lead", "once", "advance"):
        cfg.notify_mode = "lead"
    elif mode in ("every", "repeat", "periodic"):
        cfg.notify_mode = "every"
    else:
        cfg.notify_mode = "every"
    rs = getattr(cfg, "route_settings", None)
    if not isinstance(rs, dict):
        cfg.route_settings = {}
    else:
        clean = {}
        for rk, rv in rs.items():
            if not isinstance(rv, dict):
                continue
            item = {}
            if "notify_at" in rv:
                try:
                    item["notify_at"] = sorted({int(x) for x in rv["notify_at"] if int(x) >= 0}, reverse=True)
                except Exception:
                    pass
            if "notify_repeat_minutes" in rv:
                try:
                    item["notify_repeat_minutes"] = max(1, min(180, int(rv["notify_repeat_minutes"])))
                except Exception:
                    pass
            if "walk_minutes" in rv:
                try:
                    item["walk_minutes"] = max(0, min(120, int(rv["walk_minutes"])))
                except Exception:
                    pass
            if "notify_mode" in rv:
                m = str(rv.get("notify_mode") or "").strip().lower()
                if m in ("lead", "once", "advance"):
                    item["notify_mode"] = "lead"
                elif m in ("every", "repeat", "periodic"):
                    item["notify_mode"] = "every"
            if item:
                clean[str(rk)] = item
        cfg.route_settings = clean


def reload_from_file(cfg: Config) -> bool:
    """Перечитывает config.json на ходу. True — если файл прочитан."""
    if not cfg.config_path:
        return False
    with open(cfg.config_path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    apply_settings(cfg, data)
    _apply_env_secrets(cfg)
    normalize(cfg)
    return True


def file_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def snapshot_path(config_path: str) -> str:
    """Куда складывать снимок последнего прогноза (его читает веб-панель).

    По умолчанию — рядом с конфигом. Если каталог конфига доступен только
    для чтения (так настроен systemd-юнит трекера), путь можно переопределить
    в config.json: "snapshot_path": "/var/lib/marshrutka-watch/last_arrivals.json"
    """
    base = os.path.dirname(os.path.abspath(config_path)) if config_path else "."
    return os.path.join(base, "last_arrivals.json")


def snapshot_file(cfg: "Config") -> str:
    """Итоговый путь файла снимка с учётом переопределения в конфиге."""
    override = str(getattr(cfg, "snapshot_path", "") or "").strip()
    if override:
        return override
    return snapshot_path(cfg.config_path)


def write_snapshot(path: str, arrivals: List[Arrival], tz_offset: Optional[int],
                   stop_name: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """Сохраняем прогноз и служебный статус в файл для веб-панели."""
    rows = []
    for a in sorted(arrivals, key=lambda x: x.ts):
        rows.append({
            "route": a.route,
            "ts": a.ts,
            "when": city_time(a.ts, tz_offset).strftime("%H:%M"),
            "mins": max(0, int(round(a.minutes))),
            "side": a.side or "",          # откуда подъедет
            "dest": a.dest or "",          # куда поедет
            "travel_dir": a.dest or "",
            "from_side": a.side or "",
            "direction_known": bool(a.dest),
            "live": bool(a.live),
            "source": "в эфире" if a.live else "прогноз Яндекса",
        })
    payload = {
        "kind": "marshrutka-watch/1",
        "ts": time.time(),
        "tz": tz_offset,
        "meta": stop_name,
        "rows": rows,
    }
    if extra:
        payload.update(extra)
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        log("не удалось записать снимок прогноза: %s" % exc, "WARN")


class Notifier:
    """Следит за тем, чтобы не прислать одно и то же уведомление дважды."""

    def __init__(self, cfg: Config, notifier: Ntfy, stop_name: str = "", tz_source: Any = None):
        self.cfg = cfg
        self.notifier = notifier
        self.stop_name = stop_name or "остановка"
        self.tz_source = tz_source
        self.announced: Dict[str, set] = {}
        # маршрут -> (id рейса, опорное время). Время может «плавать» на секунды,
        # поэтому рейс считаем тем же, пока id совпадает или ETA в пределах допуска.
        self.last_trip: Dict[str, Tuple[Any, int]] = {}
        # маршрут -> unix-время последнего отправленного уведомления по текущей поездке
        self.last_notify_ts: Dict[str, float] = {}
        # маршрут -> набор порогов, уже уведомлённых для текущей поездки (режим once)
        self.notified_thresholds: Dict[str, set] = {}
        self.warned_no_data = False
        # наблюдаемость
        self.last_notify = None
        self.last_skip_reason = ""
        self.recent_events = []

    def reset(self) -> None:
        """Сбрасывает историю отправленных порогов (после смены настроек)."""
        self.announced.clear()
        self.last_trip.clear()
        self.last_notify_ts.clear()
        self.notified_thresholds.clear()


    def _effective(self, route: str):
        base = {
            "notify_at": list(self.cfg.notify_at or []),
            "notify_repeat_minutes": int(getattr(self.cfg, "notify_repeat_minutes", 1) or 1),
            "notify_mode": str(getattr(self.cfg, "notify_mode", "repeat") or "repeat"),
            "walk_minutes": int(getattr(self.cfg, "walk_minutes", 0) or 0),
        }
        rs = getattr(self.cfg, "route_settings", {}) or {}
        ov = rs.get(str(route)) or rs.get(route)
        if isinstance(ov, dict):
            for k in ("notify_at", "notify_repeat_minutes", "notify_mode", "walk_minutes"):
                if k in ov and ov[k] is not None:
                    base[k] = ov[k]
        try:
            base["notify_at"] = sorted({int(x) for x in (base.get("notify_at") or []) if int(x) >= 0}, reverse=True)
        except Exception:
            base["notify_at"] = list(self.cfg.notify_at or [15, 10, 5, 2])
        if not base["notify_at"]:
            base["notify_at"] = [15, 10, 5, 2]
        try:
            base["notify_repeat_minutes"] = max(1, min(180, int(base.get("notify_repeat_minutes") or 1)))
        except Exception:
            base["notify_repeat_minutes"] = 1
        mode = str(base.get("notify_mode") or "repeat").strip().lower()
        if mode in ("lead", "once", "advance"):
            mode = "lead"
        elif mode in ("every", "repeat", "periodic"):
            mode = "every"
        else:
            mode = "every"
        base["notify_mode"] = mode
        try:
            base["walk_minutes"] = max(0, min(120, int(base.get("walk_minutes") or 0)))
        except Exception:
            base["walk_minutes"] = 0
        return base

    def _record_event(self, event):
        event = dict(event)
        event.setdefault("ts", time.time())
        self.recent_events.append(event)
        if len(self.recent_events) > 30:
            self.recent_events = self.recent_events[-30:]

    def diagnostics(self):
        return {
            "last_notify": self.last_notify,
            "last_skip_reason": self.last_skip_reason or "",
            "recent_events": list(self.recent_events[-10:]),
        }

    def handle(self, arrivals: List[Arrival], now: datetime) -> None:
        nearest: Dict[str, Arrival] = {}
        for a in arrivals:
            if a.minutes < -1:
                continue
            cur = nearest.get(a.route)
            if cur is None or a.ts < cur.ts:
                nearest[a.route] = a

        now_ts = time.time()
        for route, arr in nearest.items():
            trip_id = arr.vehicle_id or "ts"
            mins = arr.minutes
            prev = self.last_trip.get(route)
            same_trip = False
            if prev is not None:
                prev_id, prev_ts = prev
                if arr.vehicle_id and prev_id == arr.vehicle_id:
                    same_trip = True
                elif abs(arr.ts - prev_ts) <= 180:
                    # ETA живого прогноза может «плавать» на минуты — считаем тем же рейсом
                    same_trip = True
            if not same_trip:
                self.announced[route] = set()
                self.last_notify_ts.pop(route, None)
                self.notified_thresholds.pop(route, None)
            self.last_trip[route] = (trip_id, arr.ts)

            eff = self._effective(route)
            notify_at = eff["notify_at"]
            mode = eff.get("notify_mode") or "every"
            # "за N минут до приезда" — используем первое (максимальное) значение как целевой порог
            lead = max(notify_at) if notify_at else 10
            if mode == "lead":
                limit = lead
                if mins > limit or mins < -1:
                    self.last_skip_reason = "вне порога (%s мин)" % int(round(mins))
                    self._record_event({"type": "skip", "route": route, "reason": "out_of_range", "mins": int(round(mins))})
                    continue
                done = self.notified_thresholds.setdefault(route, set())
                if lead in done:
                    self.last_skip_reason = "уведомление за %s мин уже отправлено" % lead
                    self._record_event({"type": "skip", "route": route, "reason": "once_done", "mins": int(round(mins)), "thresholds": [lead]})
                    continue
                done.add(lead)
                self.last_skip_reason = ""
                self._send(arr, mins, lead, eff)
                continue

            # "каждые N минут" — напоминаем периодически, пока маршрутка в пределах горизонта
            horizon = lead
            if mins > horizon or mins < -1:
                self.last_skip_reason = "вне горизонта (%s мин)" % int(round(mins))
                self._record_event({"type": "skip", "route": route, "reason": "out_of_range", "mins": int(round(mins))})
                continue
            repeat = int(eff.get("notify_repeat_minutes") or 1)
            if repeat < 1:
                repeat = 1
            last_ts = self.last_notify_ts.get(route)
            if last_ts is not None and (now_ts - last_ts) < repeat * 60 - 5:
                self.last_skip_reason = "повтор не раньше чем через %s мин" % repeat
                self._record_event({"type": "skip", "route": route, "reason": "throttle", "mins": int(round(mins)), "repeat": repeat})
                continue
            threshold = min([t for t in notify_at if mins <= t] or [horizon])
            self.last_skip_reason = ""
            self.last_notify_ts[route] = now_ts
            self._send(arr, mins, threshold, eff)

    def _send(self, arr: Arrival, mins: float, threshold: int, eff=None) -> None:
        if eff is None:
            eff = self._effective(arr.route)
        tz = getattr(self.tz_source, "tz_offset", None)
        when = city_time(arr.ts, tz).strftime("%H:%M")
        eta_min = max(0, int(round(mins)))
        dir_label = arr.dest or ""  # куда поедет
        if dir_label:
            title = "Маршрутка %s %s — через %d мин" % (arr.route, dir_label, eta_min)
        else:
            title = "Маршрутка %s — через %d мин" % (arr.route, eta_min)

        body_lines = ["Остановка «%s»: прибытие в %s (через %d мин)." % (
            self.stop_name, when, eta_min)]
        if dir_label:
            if arr.side:
                body_lines.append("Поедет %s (%s)." % (dir_label, arr.side))
            else:
                body_lines.append("Поедет %s." % dir_label)
        else:
            body_lines.append("Направление движения пока уточняется.")

        if arr.live:
            body_lines.append("Источник: в эфире (GPS).")
        else:
            body_lines.append("Источник: прогноз Яндекса, машины в эфире сейчас может не быть.")

        walk_minutes = int(eff.get("walk_minutes", 0) or 0)
        if walk_minutes and mins <= walk_minutes:
            body_lines.append("⏰ Пора выходить!")
            if dir_label:
                title = "Маршрутка %s %s — %d мин, пора выходить" % (arr.route, dir_label, eta_min)
            else:
                title = "Маршрутка %s — %d мин, пора выходить" % (arr.route, eta_min)

        body = "\n".join(body_lines)
        priority = "high" if threshold <= 5 else "default"
        log("уведомление: %s | %s" % (title, body.replace("\n", " / ")))
        # по тапу открываем страницу остановки с живым прогнозом
        click = getattr(self.tz_source, "stop_url", "") or "https://yandex.ru/maps/"
        ok = self.notifier.send(title, body, priority=priority, tags=["bus"], click=click)
        event = {
            "ts": time.time(),
            "route": arr.route,
            "mins": eta_min,
            "when": when,
            "threshold": threshold,
            "mode": eff.get("notify_mode"),
            "direction": dir_label,
            "from_side": arr.side or "",
            "title": title,
            "ok": bool(ok),
        }
        if ok:
            self.last_notify = event
            self.last_skip_reason = ""
        self._record_event({"type": "notify" if ok else "notify_error", **event})



# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Трекер маршрутки с push-уведомлениями (ntfy)")
    p.add_argument("--config", help="путь к JSON-файлу с настройками")
    p.add_argument("--region", dest="region_id", help="regionId города в Яндекс.Картах (напр. 45 для Чебоксар)")
    p.add_argument("--city", help="seoname города (напр. cheboksary)")
    p.add_argument("--stop", dest="stop_id", help="id остановки (напр. stop__10094747)")
    p.add_argument("--routes", help="номера маршрутов через запятую (напр. 5)")
    p.add_argument("--ntfy-server", dest="ntfy_server", help="сервер ntfy (https://ntfy.sh)")
    p.add_argument("--ntfy-topic", dest="ntfy_topic", help="топик ntfy")
    p.add_argument("--ntfy-token", dest="ntfy_token", help="токен ntfy (для приватного топика)")
    p.add_argument("--interval", type=int, help="интервал опроса, секунд")
    p.add_argument("--from", dest="window_from", help="начало рабочего окна ЧЧ:ММ")
    p.add_argument("--to", dest="window_to", help="конец рабочего окна ЧЧ:ММ")
    p.add_argument("--notify-at", dest="notify_at", help="пороги уведомлений в минутах, через запятую")
    p.add_argument("--notify-repeat", dest="notify_repeat_minutes", type=int,
                   help="как часто повторять уведомления об одном и том же автобусе, минут (1 = каждый опрос)")
    p.add_argument("--side-min-back-m", dest="side_min_back_m", type=float,
                   help="как далеко назад по маршруту искать остановку-ориентир")
    p.add_argument("--default-speed", dest="default_speed_mps", type=float, help="средняя скорость, м/с")
    p.add_argument("--walk-minutes", dest="walk_minutes", type=int,
                   help="если маршрутка подъедет быстрее, писать «пора выходить»")
    p.add_argument("--timeout", type=int, help="таймаут HTTP, секунд")
    p.add_argument("--once", action="store_true", help="один опрос и выход")
    p.add_argument("--dry-run", action="store_true", help="не отправлять уведомления, только печатать")
    p.add_argument("--test-notify", dest="test_notify", action="store_true", help="проверить канал ntfy и выйти")
    p.add_argument("--selftest", action="store_true", help="подробный дамп данных и выход")
    p.add_argument("--verbose", action="store_true", help="подробный лог")
    return p.parse_args(argv)


def cmd_selftest(cfg: Config) -> int:
    tr = Tracker(cfg)
    y = tr.yandex
    state = y.stop_state(refresh=True)
    stop = state.get("config", {}).get("masstransitStop", {})
    print("Остановка: %s (%s)  tz=%s" % (y.stop_name, y.stop_id, y.tz_offset))
    print("Город: %s (%s)" % (y.city, y.region_id))
    print("URL остановки: %s" % y.stop_url)
    print()
    print("--- все маршруты через остановку ---")
    for t in stop.get("transports", []):
        for th in t.get("threads", []):
            evs = th.get("BriefSchedule", {}).get("Events", []) or []
            times = []
            for e in evs:
                node = e.get("Estimated") or e.get("Scheduled") or {}
                times.append("%s%s" % (node.get("text", "?"), "*" if e.get("Estimated") else ""))
            freq = ", ".join(f.get("text", "") for f in th.get("BriefSchedule", {}).get("Frequencies", []))
            dest = _thread_destination(th) or "?"
            print("  %-6s %-10s %-38s %s  интервал: %s" % (
                t.get("name"), t.get("type"), dest, " ".join(times), freq or "-"))
    print()
    print("--- прогноз по целевым маршрутам ---")
    arrivals = tr.stop_arrivals()
    arrivals = tr.enrich(arrivals)
    now = time.time()
    for a in arrivals:
        direction = a.dest or "направление уточняется"
        where_from = a.side or "-"
        src = "в эфире" if a.live else "прогноз Яндекса"
        print("  %-4s %s  через %5.1f мин  %-22s %-34s %-14s %s" % (
            a.route, city_time(a.ts, y.tz_offset).strftime("%H:%M"), a.minutes,
            direction, where_from, src, a.vehicle_id or ""))
    print()
    print("--- геометрия ---")
    for route in cfg.routes:
        geo = tr.geometry(route)
        if not geo:
            print("  маршрут %s: геометрия недоступна" % route)
            continue
        occ = geo.occurrences(y.stop_name)
        print("  маршрут %s: %.1f км, %d остановок, прохождений нашей остановки: %d" % (
            route, geo.total / 1000.0, len(geo.stops), len(occ)))
        for o in occ:
            print("     проход №%d (%.0f м): со стороны «%s», далее «%s»" % (
                o.order + 1, o.arc, geo.side_label(o, cfg.side_min_back_m), geo.dest_after(o)))
        line_ids = tr._line_ids(route)
        kind = ""
        for t in state.get("config", {}).get("masstransitStop", {}).get("transports", []):
            if str(t.get("name")) == route:
                kind = t.get("type", "")
        vs = tr.vehicles(line_ids, kind)
        print("     машин в эфире: %d" % len(vs))
        info = tr._vehicle_positions(geo, occ, vs)
        for vid, inf in info.items():
            print("       %s -> %s, ETA через %.1f мин" % (
                vid, inf["side"], (inf["eta_ts"] - now) / 60.0))
    return 0


def cmd_once(cfg: Config) -> int:
    tr = Tracker(cfg)
    notif = Ntfy(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token, dry_run=cfg.dry_run)
    arrivals = tr.poll()
    now = tr.yandex.local_now()
    print("Сейчас в городе: %s" % now.strftime("%Y-%m-%d %H:%M"))
    print("Остановка: %s" % tr.yandex.stop_name)
    print()
    if not arrivals:
        print("  ближайших проездов не найдено")
    for a in arrivals:
        when = city_time(a.ts, tr.yandex.tz_offset).strftime("%H:%M")
        mins = max(0, int(round(a.minutes)))
        direction = a.dest or "направление уточняется"
        where_from = (" (%s)" % a.side) if a.side else ""
        src = "в эфире" if a.live else "прогноз Яндекса"
        print("  №%-4s  %s  через %2d мин  %-22s%s  [%s]" % (
            a.route, when, mins, direction, where_from, src))
    if cfg.test_notify:
        notif.test()
    return 0


def run_daemon(cfg: Config) -> int:
    tr = Tracker(cfg)
    notif = Ntfy(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token, dry_run=cfg.dry_run)
    state = Notifier(cfg, notif, tz_source=tr.yandex)
    win_from = parse_hhmm(cfg.window_from)
    win_to = parse_hhmm(cfg.window_to)

    try:
        tr.yandex.stop_state(refresh=True)
        state.stop_name = tr.yandex.stop_name
    except DataError:
        pass
    log("старт: маршруты %s, остановка «%s», топик %s, окно %s-%s%s" % (
        ",".join(cfg.routes), state.stop_name, cfg.ntfy_topic, cfg.window_from, cfg.window_to,
        ", кроме сб-вс" if cfg.weekdays_only else ""))

    if cfg.test_notify:
        notif.test()
        return 0

    failures = 0
    mtime = file_mtime(cfg.config_path)
    status = {
        "in_window": False,
        "last_poll_ts": None,
        "last_ok_ts": None,
        "last_error": "",
        "last_error_ts": None,
        "current_window": "%s-%s" % (cfg.window_from, cfg.window_to),
    }

    def _status_extra() -> Dict[str, Any]:
        routes_eff = {}
        for r in (cfg.routes or []):
            try:
                routes_eff[str(r)] = state._effective(str(r))
            except Exception:
                routes_eff[str(r)] = {}
        extra = {
            "status": dict(status),
            "diagnostics": state.diagnostics(),
            "effective": {
                "notify_at": list(cfg.notify_at or []),
                "notify_repeat_minutes": int(getattr(cfg, "notify_repeat_minutes", 1) or 1),
                "notify_mode": str(getattr(cfg, "notify_mode", "repeat") or "repeat"),
                "walk_minutes": int(getattr(cfg, "walk_minutes", 0) or 0),
                "route_settings": getattr(cfg, "route_settings", {}) or {},
                "routes": routes_eff,
            },
        }
        return extra

    while True:
        # config.json мог измениться из веб-панели — применяем без перезапуска
        new_mtime = file_mtime(cfg.config_path)
        if new_mtime is not None and new_mtime != mtime:
            mtime = new_mtime
            try:
                reload_from_file(cfg)
                win_from = parse_hhmm(cfg.window_from)
                win_to = parse_hhmm(cfg.window_to)
                state.reset()
                log("настройки обновлены: окно %s-%s%s, пороги %s, режим %s, повтор %s мин, пора выходить %s" % (
                    cfg.window_from, cfg.window_to, ", будни пн-пт" if cfg.weekdays_only else "",
                    ",".join(str(x) for x in cfg.notify_at) or "-",
                    getattr(cfg, "notify_mode", "repeat"),
                    getattr(cfg, "notify_repeat_minutes", 1), cfg.walk_minutes or "выкл"))
            except Exception as exc:  # noqa: BLE001
                log("не удалось перечитать конфиг: %s" % exc, "ERROR")
        try:
            now = tr.yandex.local_now()
            win_from, win_to = effective_window(cfg, now)
            in_win = in_window(now, win_from, win_to, cfg.weekdays_only)
            status["in_window"] = in_win
            status["current_window"] = "%s-%s" % (cfg.window_from, cfg.window_to)
            arrivals = tr.poll()
            status["last_poll_ts"] = time.time()
            if getattr(tr, "last_error", None):
                status["last_error"] = tr.last_error
                status["last_error_ts"] = time.time()
            if not arrivals:
                if in_win and not state.warned_no_data:
                    log("нет данных о прибытиях (маршрут не едет или нет GPS)", "WARN")
                    state.warned_no_data = True
                if in_win:
                    state.last_skip_reason = "нет данных о прибытиях"
            else:
                state.warned_no_data = False
                # показываем снимок в панели всегда (для проверки/поиска багов),
                # а уведомления шлём только внутри окна слежения
                if in_win:
                    state.handle(arrivals, now)
            # снимок прогноза пишем всегда — панель должна постоянно видеть машины
            write_snapshot(snapshot_file(cfg), arrivals,
                           tr.yandex.tz_offset, state.stop_name, extra=_status_extra())
            status["last_ok_ts"] = time.time()
            status["last_error"] = ""
            status["last_error_ts"] = None
            failures = 0
        except DataError as exc:
            failures += 1
            status["last_error"] = str(exc)
            status["last_error_ts"] = time.time()
            log("ошибка данных: %s" % exc, "ERROR")
            if failures == 3:
                notif.send("Маршрутка %s: нет данных" % ",".join(cfg.routes),
                           "Не удалось получить прогноз: %s" % exc, priority="low")
            time.sleep(min(300, cfg.interval * failures))
            continue
        except Exception as exc:  # noqa: BLE001
            failures += 1
            status["last_error"] = str(exc)
            status["last_error_ts"] = time.time()
            log("непредвиденная ошибка: %r" % exc, "ERROR")
            time.sleep(min(300, cfg.interval * failures))
            continue
        # спим короткими шагами, чтобы сразу заметить правку конфига
        deadline = time.time() + cfg.interval
        while time.time() < deadline:
            time.sleep(min(5.0, max(0.2, deadline - time.time())))
            if file_mtime(cfg.config_path) not in (None, mtime):
                break


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _LOG_LEVEL
    # чтобы кириллица не ломалась в консоли Windows
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = parse_args(argv)
    if args.verbose:
        _LOG_LEVEL = LOG_LEVELS["DEBUG"]
    cfg = build_config(args)
    if not cfg.ntfy_topic:
        print("Укажите --ntfy-topic (случайная строка — лучше для приватности)", file=sys.stderr)
        return 2
    if args.selftest:
        return cmd_selftest(cfg)
    if args.once:
        return cmd_once(cfg)
    try:
        return run_daemon(cfg)
    except KeyboardInterrupt:
        log("остановлено пользователем")
        return 0


if __name__ == "__main__":
    sys.exit(main())
