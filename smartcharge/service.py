"""Smart charge dispatch service — two asyncio coroutines.

dispatch_loop
    Polls the EON Next Kraken API every 60 s for planned and completed
    EV dispatch windows, stores them in SQLite, prunes stale planned
    records, and sends/updates Google Calendar invites when windows change.

dispatch_action_loop
    Every 60 s checks whether we are inside a dispatch window, verifies
    the Zappi is actively charging, and sets the Tesla Powerwall to
    BACKUP mode (100 % reserve) so it does not discharge while the EV is
    charging on the cheap rate. Reverts to TIME_BASED_CONTROL once the
    window ends or the Zappi stops drawing power.

Logic summary:
  - Dispatch window active + Zappi charging   → Powerwall BACKUP 100 %
  - Dispatch window active + Zappi idle        → stay BACKUP until slot end
  - Dispatch window ended / Zappi stopped      → revert to TIME_BASED_CONTROL
  - 00:00–06:00 UK (cheap rate hours)          → no control actions, midnight reset
  - Dispatch pruned mid-window by Kraken       → hold BACKUP until slot end
  - Zappi still charging 1 min after window end → extend BACKUP by 29 min
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

from .apis.kraken import KrakenDispatchAPI
from .apis.myenergi import ZappiStatusAPI
from .apis.gcal import GCalAPI
from .db.store import DispatchStore

UK_TZ = ZoneInfo("Europe/London")
log = logging.getLogger("smartcharge")

DISPATCH_LOOP_INTERVAL    = 60   # seconds between Kraken polls
DISPATCH_ACTION_INTERVAL  = 60   # seconds between Powerwall checks
ZAPPI_CHECK_INTERVAL      = 180  # seconds between Zappi status checks

# pypowerwall set_mode() strings
MODE_TIME_BASED = "autonomous"  # TIME_BASED_CONTROL in Tesla app
MODE_BACKUP     = "backup"      # Backup-Only mode


# ---------------------------------------------------------------------------
# Powerwall controller (pypowerwall cloud API)
# ---------------------------------------------------------------------------

class PowerwallController:
    """Controls Tesla Powerwall mode via the pypowerwall cloud API.

    Uses the free Tesla owner API (not Fleet API). On first run the
    pypowerwall library will prompt for a browser login; subsequent
    runs use the cached token at <authpath>/.pypowerwall.json.

    One-time setup (run interactively before starting the service):
        python3 -c "
        import pypowerwall
        pypowerwall.Powerwall('', password='<pw>', email='<email>',
                              cloudmode=True, authpath='.')
        "
    """

    def __init__(self, email: str, password: str,
                 site_id: str = "", authpath: str = ""):
        self.email = email
        self.password = password
        self.site_id = site_id or None
        self.authpath = authpath
        self._pw = None

    def connect(self):
        try:
            import pypowerwall
        except ImportError:
            raise ImportError("pypowerwall not installed — run: pip install pypowerwall")
        self._pw = pypowerwall.Powerwall(
            host="",
            password=self.password,
            email=self.email,
            timezone="Europe/London",
            cloudmode=True,
            siteid=None,
            authpath=self.authpath,
        )
        if not self._pw.is_connected():
            raise ConnectionError("pypowerwall cloud: not connected after init")

    def set_operation(self, mode: str, reserve_pct: float):
        """Set mode ('autonomous' or 'backup') and reserve percentage."""
        if self._pw is None:
            self.connect()
        self._pw.set_reserve(int(reserve_pct))
        self._pw.set_mode(mode)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _half_hour_boundary(dt: datetime) -> datetime:
    m = 0 if dt.minute < 30 else 30
    return dt.replace(minute=m, second=0, microsecond=0)


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _recently_fetched(d: dict, now: datetime, max_age_s: int = 300) -> bool:
    """True if the dispatch record was refreshed within the last max_age_s seconds."""
    fa = d.get("fetched_at", "")
    if not fa:
        return False
    try:
        ft = datetime.fromisoformat(fa.replace("Z", "+00:00"))
        if ft.tzinfo is None:
            ft = ft.replace(tzinfo=timezone.utc)
        return (now - ft).total_seconds() <= max_age_s
    except Exception:
        return False


# ---------------------------------------------------------------------------
# dispatch_loop
# ---------------------------------------------------------------------------

async def dispatch_loop(cfg: dict, db_path: str):
    """Poll Kraken for dispatch windows every 60 s, persist in SQLite.

    Also prunes planned records that Kraken no longer returns and
    sends/updates Google Calendar invites when windows change.
    """
    en = cfg.get("eon_next", {})
    if not en.get("import_account"):
        log.info("eon_next.import_account not configured — dispatch_loop skipping")
        return

    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: shutdown.set())
        except Exception:
            pass

    device_id = None  # cached after first discovery

    gcal_api = None
    gcal_cfg = cfg.get("gcal", {})
    if gcal_cfg.get("smtp_user") and gcal_cfg.get("smtp_pass"):
        try:
            gcal_api = GCalAPI(
                gcal_cfg["smtp_user"],
                gcal_cfg["smtp_pass"],
                gcal_cfg.get("invite_to", gcal_cfg["smtp_user"]),
                cheap_rate_pence=gcal_cfg.get("cheap_rate_pence", 6.19),
                standard_rate_pence=gcal_cfg.get("standard_rate_pence", 26.073),
            )
        except Exception as e:
            log.warning(f"GCal init failed: {e}")

    while not shutdown.is_set():
        try:
            api = KrakenDispatchAPI(
                en["email"], en["password"], en["import_account"]
            )

            def _fetch():
                nonlocal device_id
                api.authenticate()
                if device_id is None:
                    device_id = api.get_device_id()
                return api.get_dispatches(device_id=device_id)

            dispatches = await asyncio.get_event_loop().run_in_executor(None, _fetch)

            store = DispatchStore(db_path)
            for category in ("planned", "flex", "completed"):
                for d in dispatches[category]:
                    store.upsert_dispatch(**d)

            # Prune planned records no longer returned by Kraken.
            # Completed records (type IS NULL) are permanent history.
            now_utc = datetime.now(timezone.utc)
            received_keys = {
                (d["start"], d["end"])
                for cat in ("planned", "flex")
                for d in dispatches[cat]
            }
            received_by_start = {
                d["start"]: d
                for cat in ("planned", "flex")
                for d in dispatches[cat]
            }
            existing_planned = store.conn.execute(
                "SELECT start, end, gcal_event_id, gcal_sequence "
                "FROM dispatches WHERE type IS NOT NULL"
            ).fetchall()

            to_delete = [
                (r[0], r[1], r[2], r[3]) for r in existing_planned
                if (r[0], r[1]) not in received_keys
                # Don't prune mid-window confirmed records — dispatch_action_loop
                # needs them to manage Powerwall state through to slot end.
                and not (
                    r[2] is not None
                    and _parse_utc(r[0]) <= now_utc < _parse_utc(r[1])
                )
            ]

            for start, end, gcal_uid, gcal_seq in to_delete:
                store.conn.execute(
                    "DELETE FROM dispatches WHERE start=? AND end=?", (start, end)
                )
                if not gcal_uid or not gcal_api:
                    continue
                new_seq = (gcal_seq or 0) + 1

                if start in received_by_start:
                    # Superseded by a new window at the same start — update calendar.
                    new = received_by_start[start]
                    try:
                        def _upd(d=new, uid=gcal_uid, seq=new_seq):
                            gcal_api.update_dispatch_event(
                                uid, d["start"], d["end"], seq,
                                delta_kwh=d.get("delta_kwh"),
                                location=d.get("location"),
                            )
                        await asyncio.get_event_loop().run_in_executor(None, _upd)
                        store.set_dispatch_gcal_event_id(start, gcal_uid, new_seq)
                        log.info(f"Calendar event updated for dispatch {start}: end {end} → {new['end']}")
                    except Exception as e:
                        log.error(f"Failed to update calendar for superseded dispatch {start}: {e}")
                else:
                    # Transfer UID to any completed records that fell within the window.
                    completed = store.conn.execute(
                        "SELECT start, end FROM dispatches "
                        "WHERE type IS NULL AND start >= ? AND start < ? ORDER BY start",
                        (start, end)
                    ).fetchall()
                    if completed:
                        actual_start = completed[0][0]
                        actual_end = completed[-1][1]
                        for r in completed:
                            store.conn.execute(
                                "UPDATE dispatches SET gcal_event_id=?, gcal_sequence=? "
                                "WHERE start=? AND end=? AND type IS NULL",
                                (gcal_uid, new_seq, r[0], r[1])
                            )
                        try:
                            def _upd_actual(uid=gcal_uid, s=actual_start, e=actual_end, seq=new_seq):
                                gcal_api.update_dispatch_event(uid, s, e, seq)
                            await asyncio.get_event_loop().run_in_executor(None, _upd_actual)
                            log.info(f"Calendar event updated to completed span {actual_start}→{actual_end}")
                        except Exception as e:
                            log.error(f"Failed to update calendar to completed span for {start}: {e}")

            if to_delete:
                store.conn.commit()
                log.info(f"Pruned {len(to_delete)} stale planned dispatch record(s)")

            store.close()

            total = sum(len(v) for v in dispatches.values())
            log.info(
                f"Dispatch fetch: {len(dispatches['planned'])} planned, "
                f"{len(dispatches['flex'])} flex, "
                f"{len(dispatches['completed'])} completed  ({total} total)"
            )

        except Exception as e:
            log.error(f"Dispatch fetch error: {e}")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=DISPATCH_LOOP_INTERVAL)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# dispatch_action_loop
# ---------------------------------------------------------------------------

async def dispatch_action_loop(cfg: dict, db_path: str):
    """Set Powerwall BACKUP mode during active EON Next dispatch windows.

    Watches for the Zappi to start charging inside a dispatch window and
    switches the Powerwall to Backup-Only mode (100 % reserve) to prevent
    the battery from discharging while the EV charges on the cheap rate.

    Reverts to TIME_BASED_CONTROL when:
      - The Zappi stops charging and the current 30-min slot ends
      - The dispatch window ends and the Zappi has stopped
      - Midnight UK time (cheap rate applies 00:00–06:00, BACKUP not needed)
    """
    t = cfg.get("tesla", {})
    account_email    = t.get("account_email", "")
    account_password = t.get("account_password", "")
    if not account_email or not account_password:
        log.info("tesla.account_email/account_password not configured — Powerwall control disabled")
        return

    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: shutdown.set())
        except Exception:
            pass

    default_reserve = cfg.get("control", {}).get("default_reserve_pct", 20)

    # Initialise Powerwall controller
    pw_ctl = PowerwallController(
        account_email, account_password,
        site_id=t.get("energy_site_id", ""),
        authpath=str(Path(__file__).parent.parent),
    )
    try:
        await asyncio.get_event_loop().run_in_executor(None, pw_ctl.connect)
        log.info("Powerwall control enabled (pypowerwall cloud)")
    except Exception as e:
        log.error(
            f"pypowerwall auth failed ({e}). "
            "Run the one-time auth command from README then restart the service."
        )
        return

    async def _set_powerwall(label: str, mode: str, reserve_pct: int):
        await asyncio.get_event_loop().run_in_executor(
            None, pw_ctl.set_operation, mode, float(reserve_pct)
        )
        log.info(f"Powerwall → {label}  reserve={reserve_pct}%")

    # Initialise Zappi status client
    me_api = None
    me_cfg = cfg.get("myenergi", {})
    if me_cfg.get("hub_serial") and me_cfg.get("api_key"):
        me_api = ZappiStatusAPI(me_cfg["hub_serial"], me_cfg["api_key"])
    else:
        log.info("myenergi not configured — Zappi charging status unavailable")

    # GCal client for sending invites when EV charging is confirmed
    gcal_api = None
    gcal_cfg = cfg.get("gcal", {})
    if gcal_cfg.get("smtp_user") and gcal_cfg.get("smtp_pass"):
        try:
            gcal_api = GCalAPI(
                gcal_cfg["smtp_user"],
                gcal_cfg["smtp_pass"],
                gcal_cfg.get("invite_to", gcal_cfg["smtp_user"]),
                cheap_rate_pence=gcal_cfg.get("cheap_rate_pence", 6.19),
                standard_rate_pence=gcal_cfg.get("standard_rate_pence", 26.073),
            )
            log.info(f"Calendar invites enabled → {gcal_cfg.get('invite_to')}")
        except Exception as e:
            log.warning(f"GCal init failed — calendar events disabled: {e}")

    # Per-dispatch tracking state
    activated:        set  = set()   # dispatch start strings where BACKUP was set
    reverted:         set  = set()   # dispatch start strings where mode was restored
    pruned_logged:    set  = set()   # logged mid-window prune messages
    calendar_sent:    dict = {}      # dkey → gcal UID for dispatches with sent invites

    zappi_idle              = False
    zappi_was_charging: bool | None = None
    zappi_last_checked: datetime | None = None

    in_extension_check: dict[str, datetime] = {}  # key → dispatch end time
    extended_until:     dict[str, datetime] = {}  # key → when extended BACKUP ends

    last_midnight_reset: date | None = None

    while not shutdown.is_set():
        try:
            now     = datetime.now(timezone.utc)
            now_uk  = now.astimezone(UK_TZ)

            # Midnight reset: revert to TIME_BASED_CONTROL and clear all state.
            # Cheap rate applies 00:00–06:00 so BACKUP is never needed then.
            if now_uk.hour == 0 and last_midnight_reset != now_uk.date():
                last_midnight_reset = now_uk.date()
                log.info("Midnight reset — reverting to TIME_BASED_CONTROL")
                await _set_powerwall("TIME_BASED_CONTROL", MODE_TIME_BASED, default_reserve)
                activated.clear(); reverted.clear(); pruned_logged.clear(); calendar_sent.clear()
                in_extension_check.clear(); extended_until.clear()
                zappi_idle = False; zappi_was_charging = None; zappi_last_checked = None

            # No control actions during cheap rate hours (00:00–06:00 UK).
            if now_uk.hour < 6:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=DISPATCH_ACTION_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                continue

            store = DispatchStore(db_path)
            dispatches = store.get_dispatches(
                from_dt=(now - timedelta(hours=12)).isoformat(),
                to_dt=(now + timedelta(hours=6)).isoformat(),
            )
            store.close()

            # ----------------------------------------------------------------
            # Phase 3: extended BACKUP expiry
            # ----------------------------------------------------------------
            for key, ext_end in list(extended_until.items()):
                if now >= ext_end - timedelta(seconds=60) and key not in reverted:
                    other_active = any(
                        _parse_utc(o["start"]) <= now < _parse_utc(o["end"])
                        and _recently_fetched(o, now)
                        for o in dispatches if o["start"] != key
                    )
                    if not other_active:
                        log.info(f"Extended BACKUP expired for {key} — reverting")
                        await _set_powerwall("TIME_BASED_CONTROL", MODE_TIME_BASED, default_reserve)
                    activated.discard(key)
                    del extended_until[key]
                    in_extension_check.pop(key, None)
                    zappi_idle = False
                    zappi_was_charging = None

            # ----------------------------------------------------------------
            # Determine whether any dispatch is currently active
            # ----------------------------------------------------------------
            any_active = any(
                (
                    (_parse_utc(d["start"]) <= now < _parse_utc(d["end"])
                     and _recently_fetched(d, now))
                    or d["start"] in in_extension_check
                    or d["start"] in extended_until
                )
                for d in dispatches
            )

            need_urgent_zappi = bool(
                {d["start"] for d in dispatches
                 if d["start"] in activated
                 and (_parse_utc(d["end"]) - now).total_seconds() <= 120}
                | set(in_extension_check.keys())
            )

            if not any_active:
                # ----------------------------------------------------------------
                # No active dispatch
                # ----------------------------------------------------------------
                unrevert = activated - reverted
                slot_end = _half_hour_boundary(now) + timedelta(minutes=30)

                if unrevert and now < slot_end:
                    # Dispatch was pruned mid-window — hold BACKUP until slot end
                    # because the cheap rate still applies for the full half-hour.
                    new_pruned = unrevert - pruned_logged
                    if new_pruned:
                        log.info(
                            f"Dispatch(es) {new_pruned} pruned mid-window — staying in BACKUP "
                            f"until {slot_end.astimezone(UK_TZ).strftime('%H:%M %Z')}"
                        )
                        pruned_logged.update(new_pruned)
                elif unrevert:
                    log.info(f"Dispatch(es) {unrevert} expired — reverting to TIME_BASED_CONTROL")
                    await _set_powerwall("TIME_BASED_CONTROL", MODE_TIME_BASED, default_reserve)
                    zappi_idle = False; zappi_was_charging = None; zappi_last_checked = None
                    activated.clear(); reverted.clear(); pruned_logged.clear()
                    in_extension_check.clear(); extended_until.clear(); calendar_sent.clear()
                else:
                    zappi_idle = False; zappi_was_charging = None; zappi_last_checked = None
                    activated.clear(); reverted.clear(); pruned_logged.clear()
                    in_extension_check.clear(); extended_until.clear(); calendar_sent.clear()

            elif me_api and (
                zappi_last_checked is None
                or need_urgent_zappi
                or (now - zappi_last_checked).total_seconds() >= ZAPPI_CHECK_INTERVAL
            ):
                # ----------------------------------------------------------------
                # Active dispatch — check Zappi
                # ----------------------------------------------------------------
                zappi_last_checked = now
                try:
                    try:
                        charging = await me_api.is_charging_async()
                    except Exception as e:
                        log.warning(f"Zappi check failed, retrying once: {e}")
                        await asyncio.sleep(5)
                        charging = await me_api.is_charging_async()

                    # Phase 1: 1 min before dispatch end — decide to extend or revert.
                    for d in dispatches:
                        key = d["start"]
                        if key not in activated or key in reverted \
                                or key in in_extension_check or key in extended_until:
                            continue
                        end = _parse_utc(d["end"])
                        secs_to_end = (end - now).total_seconds()
                        if 0 < secs_to_end <= 60:
                            if charging:
                                log.info(f"Dispatch {key} ending in <1 min — Zappi charging, entering extension check")
                                in_extension_check[key] = end
                            else:
                                other_active = any(
                                    _parse_utc(o["start"]) <= now < _parse_utc(o["end"])
                                    and _recently_fetched(o, now)
                                    for o in dispatches if o["start"] != key
                                )
                                if not other_active:
                                    log.info(f"Dispatch {key} ending — Zappi idle, reverting")
                                    await _set_powerwall("TIME_BASED_CONTROL", MODE_TIME_BASED, default_reserve)
                                reverted.add(key)

                    # Phase 2: first minute after dispatch end — confirm extension.
                    for key, dispatch_end in list(in_extension_check.items()):
                        if now < dispatch_end:
                            continue
                        if charging:
                            ext_end = dispatch_end + timedelta(minutes=29)
                            log.info(f"Dispatch {key} — Zappi still charging, extending BACKUP until {ext_end.isoformat()}")
                            extended_until[key] = ext_end
                        else:
                            other_active = any(
                                _parse_utc(o["start"]) <= now < _parse_utc(o["end"])
                                and _recently_fetched(o, now)
                                for o in dispatches if o["start"] != key
                            )
                            if not other_active:
                                log.info(f"Dispatch {key} extension check — Zappi stopped, reverting")
                                await _set_powerwall("TIME_BASED_CONTROL", MODE_TIME_BASED, default_reserve)
                            reverted.add(key)
                        del in_extension_check[key]

                    in_any_extension = bool(in_extension_check or extended_until)

                    if charging:
                        if zappi_idle:
                            log.info("Zappi resumed charging — re-enabling BACKUP")
                            zappi_idle = False

                        # EV charging inside a dispatch window — activate BACKUP.
                        if now_uk.hour >= 6:
                            for d in dispatches:
                                if not (_parse_utc(d["start"]) <= now < _parse_utc(d["end"])
                                        and _recently_fetched(d, now)):
                                    continue
                                dkey = d["start"]
                                if dkey not in activated:
                                    await _set_powerwall("BACKUP (dispatch)", MODE_BACKUP, 100)
                                    activated.add(dkey)

                                # Send one calendar invite per dispatch once confirmed.
                                if gcal_api and dkey not in calendar_sent:
                                    try:
                                        gcal_store = DispatchStore(db_path)
                                        pending = sorted(
                                            [x for x in gcal_store.get_dispatches_needing_calendar_event()
                                             if x["start"] == dkey],
                                            key=lambda x: x["end"], reverse=True
                                        )
                                        gcal_store.close()
                                        if pending:
                                            p = pending[0]
                                            def _create(dispatch=p):
                                                return gcal_api.create_dispatch_event(
                                                    dispatch["start"], dispatch["end"],
                                                    delta_kwh=dispatch.get("delta_kwh"),
                                                    location=dispatch.get("location"),
                                                )
                                            event_id = await asyncio.get_event_loop().run_in_executor(
                                                None, _create)
                                            gcal_store2 = DispatchStore(db_path)
                                            gcal_store2.set_dispatch_gcal_event_id(
                                                dkey, event_id, end=p["end"])
                                            gcal_store2.close()
                                            calendar_sent[dkey] = event_id
                                            log.info(f"Calendar invite sent for dispatch {dkey} → {event_id}")
                                    except Exception as e:
                                        log.error(f"Failed to create calendar event for {dkey}: {e}")

                    elif not in_any_extension:
                        # Zappi not charging and not in extension period.
                        if not zappi_idle and activated:
                            slot_end = _half_hour_boundary(now) + timedelta(minutes=30)
                            stay_keys = [
                                d["start"] for d in dispatches
                                if (_parse_utc(d["start"]) <= now < _parse_utc(d["end"])
                                    and _recently_fetched(d, now)
                                    and d["start"] in activated
                                    and d["start"] not in reverted
                                    and (slot_end - now).total_seconds() > 60)
                            ]
                            reason = "stopped charging" if zappi_was_charging else "idle at dispatch start"
                            if stay_keys:
                                log.info(
                                    f"Zappi {reason} — staying in BACKUP until slot end "
                                    f"{slot_end.astimezone(UK_TZ).strftime('%H:%M %Z')}"
                                )
                                zappi_idle = True
                                for key in stay_keys:
                                    extended_until[key] = slot_end
                            else:
                                log.info(f"Zappi {reason} — reverting to TIME_BASED_CONTROL")
                                zappi_idle = True
                                await _set_powerwall("TIME_BASED_CONTROL", MODE_TIME_BASED, default_reserve)
                                for d in dispatches:
                                    if _parse_utc(d["start"]) <= now < _parse_utc(d["end"]):
                                        reverted.add(d["start"])

                            # Update calendar to reflect EV stop time.
                            if gcal_api and calendar_sent:
                                actual_end = (_half_hour_boundary(now) + timedelta(minutes=30)).isoformat()
                                gcal_store = DispatchStore(db_path)
                                for dkey, uid in list(calendar_sent.items()):
                                    row = gcal_store.conn.execute(
                                        "SELECT start, gcal_sequence FROM dispatches "
                                        "WHERE start=? AND gcal_event_id=? ORDER BY end DESC LIMIT 1",
                                        (dkey, uid)
                                    ).fetchone()
                                    if row:
                                        disp_start, seq = row
                                        new_seq = (seq or 0) + 1
                                        try:
                                            def _upd(uid=uid, s=disp_start, e=actual_end, sq=new_seq):
                                                gcal_api.update_dispatch_event(uid, s, e, sq)
                                            await asyncio.get_event_loop().run_in_executor(None, _upd)
                                            gcal_store.conn.execute(
                                                "UPDATE dispatches SET gcal_sequence=? "
                                                "WHERE start=? AND gcal_event_id=?",
                                                (new_seq, dkey, uid)
                                            )
                                            gcal_store.conn.commit()
                                            log.info(f"Calendar event trimmed to {actual_end} for {dkey}")
                                        except Exception as ex:
                                            log.error(f"Failed to trim calendar event for {dkey}: {ex}")
                                gcal_store.close()

                    zappi_was_charging = charging

                except Exception as e:
                    log.warning(f"Zappi check failed: {e}")

        except Exception as e:
            log.error(f"Dispatch action error: {e}")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=DISPATCH_ACTION_INTERVAL)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(cfg: dict):
    """Run dispatch_loop and dispatch_action_loop concurrently."""
    db_path = cfg.get("database", {}).get("path", "data/smartcharge.db")
    if not Path(db_path).is_absolute():
        db_path = str(Path(__file__).parent.parent / db_path)

    await asyncio.gather(
        dispatch_loop(cfg, db_path),
        dispatch_action_loop(cfg, db_path),
    )
