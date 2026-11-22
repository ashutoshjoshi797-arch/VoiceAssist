"""Connection manager: detects ONLINE / OFFLINE / AIRPLANE MODE and decides cloud vs. local routing.

Detection layers (each one optional — if a layer is unavailable it is skipped, and when nothing can
be determined the app simply runs locally):

1. Airplane mode, read from the OS without touching the network:
     Windows : HKLM\\SYSTEM\\CurrentControlSet\\Control\\RadioManagement\\SystemRadioState (1 = on)
     Linux   : rfkill state in /sys/class/rfkill (all radios blocked)
2. Network adapters: if no non-loopback adapter is up we are offline (no probe needed, no waiting).
3. Reachability: a TCP connect (no data sent) to the cloud API host, or to the configured proxy.

Routing policy ("auto" mode):
* ONLINE + API key -> cloud provider.
* Not ONLINE -> local model. The switch is "pinned": when the internet returns the user is ASKED
  whether to switch back (or it switches automatically if auto_switch_back is enabled).
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import netutil, secrets_store

log = logging.getLogger(__name__)

ONLINE, OFFLINE, AIRPLANE, UNKNOWN = "ONLINE", "OFFLINE", "AIRPLANE", "UNKNOWN"

PROVIDER_HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
}

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


# ----------------------------------------------------------------------------- OS signals
def detect_airplane_mode() -> bool | None:
    """True/False when the OS reports it, None when this platform can't tell."""
    try:
        if sys.platform == "win32":
            import winreg  # type: ignore

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\RadioManagement\SystemRadioState"
            ) as k:
                val, _ = winreg.QueryValueEx(k, "")
                return int(val) == 1
        if sys.platform.startswith("linux"):
            base = Path(os.environ.get("VOICEASSIST_RFKILL_DIR", "/sys/class/rfkill"))
            if not base.exists():
                return None
            radios = [d for d in base.iterdir() if (d / "type").exists()]
            radios = [d for d in radios if (d / "type").read_text().strip() in ("wlan", "bluetooth", "wwan")]
            if not radios:
                return None
            blocked = 0
            for d in radios:
                soft = (d / "soft").read_text().strip() if (d / "soft").exists() else "0"
                hard = (d / "hard").read_text().strip() if (d / "hard").exists() else "0"
                blocked += soft == "1" or hard == "1"
            return blocked == len(radios)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.debug("airplane detection unavailable: %s", e)
    return None


def detect_adapters_up() -> bool | None:
    """True if any non-loopback adapter is up with a routable address; None if unknown."""
    if psutil is None:
        return None
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for name, st in stats.items():
            if not st.isup:
                continue
            lname = name.lower()
            if lname == "lo" or "loopback" in lname:
                continue
            for a in addrs.get(name, []):
                if a.family in (socket.AF_INET, socket.AF_INET6):
                    ip = a.address.split("%")[0]
                    if netutil.is_loopback_host(ip) or ip.startswith("169.254.") or ip.lower().startswith("fe80"):
                        continue
                    return True
        return False
    except Exception as e:
        log.debug("adapter detection unavailable: %s", e)
        return None


def _proxy_target() -> tuple[str, int] | None:
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var)
        if val:
            u = urllib.parse.urlsplit(val if "://" in val else "http://" + val)
            if u.hostname:
                return u.hostname, u.port or 8080
    return None


def probe_tcp(host: str, port: int = 443, timeout: float = 2.5) -> bool:
    if netutil.socket_guard_enabled():
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, netutil.OfflineViolation):
        return False


# ----------------------------------------------------------------------------- manager
class ConnectionManager:
    def __init__(self, settings, interval_online: float = 8.0, interval_offline: float = 3.0):
        self.settings = settings
        self.interval_online = interval_online
        self.interval_offline = interval_offline
        self._lock = threading.RLock()
        self._listeners = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.state = UNKNOWN
        self.airplane: bool | None = None
        self.adapters_up: bool | None = None
        self.internet: bool | None = None
        self.detail = "checking…"
        self.last_check = 0.0
        self.pinned_local = False  # fell back to local because the network went away
        self.switch_back_offered = False
        self._thread: threading.Thread | None = None
        # Test hook: force a state ("ONLINE"/"OFFLINE"/"AIRPLANE") without real detection.
        self.forced_state: str | None = os.environ.get("VOICEASSIST_FORCE_NETWORK") or None

    # -- listeners ----------------------------------------------------------
    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def _emit(self, kind: str) -> None:
        snap = self.snapshot()
        for fn in list(self._listeners):
            try:
                fn(kind, snap)
            except Exception:
                log.exception("connectivity listener failed")

    # -- detection ----------------------------------------------------------
    def _probe_targets(self) -> list[tuple[str, int]]:
        override = os.environ.get("VOICEASSIST_PROBE_HOSTS")  # tests: "127.0.0.1:9999,..."
        if override:
            return [(h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1])) for h in override.split(",") if h]
        proxy = _proxy_target()
        if proxy:
            return [proxy]
        provider = self.settings.get("online.provider", "anthropic")
        hosts = [PROVIDER_HOSTS.get(provider, "api.anthropic.com")]
        hosts += [h for p, h in PROVIDER_HOSTS.items() if p != provider]
        return [(h, 443) for h in hosts]

    def _detect(self) -> tuple[str, str, bool | None, bool | None, bool | None]:
        if self.forced_state:
            return self.forced_state, f"forced by VOICEASSIST_FORCE_NETWORK={self.forced_state}", None, None, None
        airplane = detect_airplane_mode()
        adapters = detect_adapters_up()
        strict = bool(self.settings.get("strict_offline", False))
        internet: bool | None
        if strict:
            internet = None
        elif adapters is False:
            internet = False
        else:
            internet = any(probe_tcp(h, p) for h, p in self._probe_targets()[:2])

        if internet:
            state = ONLINE
            detail = "Internet reachable" + (" (airplane mode on, Wi-Fi re-enabled)" if airplane else "")
        elif airplane:
            state = AIRPLANE
            detail = "Airplane mode is on — all radios off. Using on-device models."
        elif strict:
            state = OFFLINE
            detail = "Strict offline mode — the app makes no network connections."
        elif adapters is False:
            state = OFFLINE
            detail = "No network adapter connected (Wi-Fi/Ethernet off)."
        else:
            state = OFFLINE
            detail = "No internet — cloud AI unreachable."
        return state, detail, airplane, adapters, internet

    def check_now(self) -> dict:
        try:
            state, detail, airplane, adapters, internet = self._detect()
        except Exception as e:  # detection itself failed: keep working locally
            log.warning("network detection failed: %s", e)
            state, detail, airplane, adapters, internet = UNKNOWN, f"Detection unavailable ({e}); running locally.", None, None, None
        self._apply(state, detail, airplane, adapters, internet)
        return self.snapshot()

    def _apply(self, state, detail, airplane, adapters, internet) -> None:
        kind = None
        with self._lock:
            prev = self.state
            self.state, self.detail = state, detail
            self.airplane, self.adapters_up, self.internet = airplane, adapters, internet
            self.last_check = time.time()
            auto = self.settings.get("mode", "auto") == "auto"
            if state != ONLINE and auto:
                self.pinned_local = True
            if prev != state:
                kind = "state"
                log.info("network: %s -> %s (%s)", prev, state, detail)
                if state == ONLINE and self.pinned_local and auto:
                    if self.settings.get("auto_switch_back", False):
                        self.pinned_local = False
                        kind = "auto_switched_back"
                    elif prev != UNKNOWN and self._online_usable():
                        self.switch_back_offered = True
                        kind = "switch_back_offer"
                    else:
                        # App started (or key missing): nothing to "switch back" to.
                        self.pinned_local = False
        if kind:
            self._emit(kind)

    def report_network_failure(self, reason: str) -> None:
        """Called by the engine when a cloud request fails at the network level."""
        log.warning("cloud request failed (%s) — switching to local", reason)
        self._apply(
            OFFLINE if not self.airplane else AIRPLANE,
            f"Cloud AI unreachable: {reason}",
            self.airplane,
            self.adapters_up,
            False,
        )
        with self._lock:
            self.pinned_local = True
        self._wake.set()

    def accept_switch_back(self, accept: bool) -> None:
        with self._lock:
            self.switch_back_offered = False
            if accept:
                self.pinned_local = False
        self._emit("state")

    # -- routing ------------------------------------------------------------
    def _online_usable(self) -> bool:
        provider = self.settings.get("online.provider", "anthropic")
        return bool(secrets_store.get_key(provider))

    def route(self) -> tuple[str, str]:
        """('online'|'local', human-readable reason)."""
        mode = self.settings.get("mode", "auto")
        if mode == "local":
            return "local", "Local-only mode selected"
        if self.settings.get("strict_offline", False):
            return "local", "Strict offline mode"
        with self._lock:
            state, pinned = self.state, self.pinned_local
        if state != ONLINE:
            return "local", {AIRPLANE: "Airplane mode", OFFLINE: "No internet", UNKNOWN: "Network status unknown"}[state]
        if pinned:
            return "local", "Staying on the local model until you choose to switch back"
        if not self._online_usable():
            return "local", "No API key for the selected cloud provider"
        return "online", "Internet available"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "detail": self.detail,
                "airplane": self.airplane,
                "adapters_up": self.adapters_up,
                "internet": self.internet,
                "last_check": self.last_check,
                "pinned_local": self.pinned_local,
                "switch_back_offered": self.switch_back_offered,
                "strict_offline": bool(self.settings.get("strict_offline", False)),
            }

    # -- background loop ----------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="connectivity")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check_now()
            interval = self.interval_online if self.state == ONLINE else self.interval_offline
            self._wake.wait(interval)
            self._wake.clear()
