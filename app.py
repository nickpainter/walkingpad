import asyncio
import logging
import os
import threading
import webbrowser
from threading import Timer
import time
from collections import deque

from bleak import BleakScanner
from flask import Flask, render_template, redirect, url_for, jsonify, make_response, request
from ph4_walkingpad.pad import Controller, WalkingPad

# ── Logging Setup ────────────────────────────────────────────────────────
# All print() statements will be replaced with this logging configuration.
# It provides timed, leveled output. Set level=logging.DEBUG to see verbose messages.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# ── Conversion constants ─────────────────────────────────────────────────
KM_TO_MI = 0.621371
KMH_TO_MPH = 0.621371
KCAL_PER_MILE = 95  # rough kcal per mile

# Speed control constants
MAX_SPEED_KMH = 6.0  # Approx 3.7 mph, a common max for these pads
MIN_SPEED_KMH = 1.0
SPEED_STEP = 0.6  # Speed change per button press in km/h
SLOW_WALK_SPEED_KMH = 4.5 # Approx 2.8 MPH


def kcal_estimate(miles: float) -> float:
    return KCAL_PER_MILE * miles


# ── Flask & global state ────────────────────────────────────────────────
app = Flask(__name__)

connected = connecting = connection_failed = False
ble_loop: asyncio.AbstractEventLoop | None = None
controller: Controller | None = None
_pad_address: str | None = None
_auto_pause_grace_until = 0
speed_history = deque(maxlen=15)

session_active = belt_running = False
resume_speed_kmh = 2.0  # default if none yet

current_speed_kmh = current_distance_km = 0.0
current_steps = 0
current_calories = 0.0

_last_dev_dist = _last_dev_steps = 0


# ── Context processor so templates always know flags ────────────────────
@app.context_processor
def inject_flags():
    return dict(connected=connected, connecting=connecting, connection_failed=connection_failed)


# ── BLE helpers ─────────────────────────────────────────────────────────
async def _connect_to_pad() -> bool:
    global controller, _pad_address
    dev = None

    if _pad_address:
        logging.info(f"Attempting to connect to known address: {_pad_address}")
        try:
            dev = await BleakScanner.find_device_by_address(_pad_address, timeout=5)
        except Exception as exc:
            logging.warning(f"Failed to find device by address: {exc}")
            dev = None

    if not dev:
        logging.info("Scanning for device by name 'WalkingPad'...")
        try:
            dev = await BleakScanner.find_device_by_name("WalkingPad", timeout=10)
        except Exception as exc:
            logging.warning(f"Failed to find device by name: {exc}")
            dev = None

    if not dev:
        logging.error("Could not find WalkingPad. Ensure it is on and in range.")
        _pad_address = None
        return False

    _pad_address = dev.address
    logging.info(f"Device found! Address: {_pad_address}")

    controller = Controller()
    await controller.run(dev.address)

    if hasattr(controller, "client") and controller.client:
        controller.client.set_disconnected_callback(_handle_disconnect)

    await controller.switch_mode(WalkingPad.MODE_MANUAL)

    def _status_cb(_sender, st):
        try:
            if isinstance(st, dict):
                dist = st.get("dist", 0)
                steps = st.get("steps", 0)
                speed = st.get("speed", 0)
            else:
                dist = getattr(st, "dist", 0)
                steps = getattr(st, "steps", 0)
                speed = getattr(st, "speed", 0)
            process_status_packet(dist, steps, speed)
            logging.debug(f"Push d={dist} s={steps} v={speed}")
        except Exception as exc:
            logging.warning(f"status_cb error: {exc}")

    controller.on_cur_status_received = _status_cb

    if hasattr(controller, "enable_notifications"):
        try:
            await controller.enable_notifications()
        except Exception as exc:
            logging.warning(f"enable_notifications failed: {exc}")
    return True


def process_status_packet(dev_dist, dev_steps, dev_speed):
    """Update cumulative stats from raw values AND handle auto-pause."""
    global belt_running, resume_speed_kmh, _auto_pause_grace_until
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global _last_dev_dist, _last_dev_steps

    new_reported_speed_kmh = dev_speed / 10.0

    # Continuously populate the speed history with stable, non-zero speeds.
    if belt_running and new_reported_speed_kmh > MIN_SPEED_KMH:
        speed_history.append(new_reported_speed_kmh)

    # AUTO-PAUSE LOGIC
    if time.time() > _auto_pause_grace_until:
        if belt_running and new_reported_speed_kmh == 0 and current_speed_kmh > 0:
            logging.info("Belt has stopped unexpectedly. Auto-pausing session.")
            
            # Use the OLDEST speed from history to ignore the deceleration phase.
            if speed_history:
                resume_speed_kmh = speed_history[0] # Use the first (oldest) item
            else:
                # Fallback if pause happens too quickly after starting
                resume_speed_kmh = MIN_SPEED_KMH

            belt_running = False

    # CUMULATIVE STATS LOGIC (is unchanged)
    # ...
    if dev_dist < _last_dev_dist:
        _last_dev_dist = 0
    current_distance_km += (dev_dist - _last_dev_dist) / 100.0
    _last_dev_dist = dev_dist

    if dev_steps < _last_dev_steps:
        _last_dev_steps = 0
    current_steps += dev_steps - _last_dev_steps
    _last_dev_steps = dev_steps

    current_speed_kmh = new_reported_speed_kmh
    current_calories = kcal_estimate(current_distance_km * KM_TO_MI)


async def _stats_monitor():
    """Active monitor: explicitly request a status packet every second."""
    logging.info("Stats monitor started")
    while belt_running:
        try:
            status = await controller.ask_stats()
            if status:
                if isinstance(status, dict):
                    dist = status.get("dist", 0)
                    steps = status.get("steps", 0)
                    speed = status.get("speed", 0)
                else:
                    dist = getattr(status, "dist", 0)
                    steps = getattr(status, "steps", 0)
                    speed = getattr(status, "speed", 0)
                process_status_packet(dist, steps, speed)
                logging.debug(f"Poll {status}")
        except Exception as exc:
            logging.warning(f"ask_stats error: {exc}")
        await asyncio.sleep(1)


def _ble_thread():
    global connected, connecting, connection_failed, ble_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ble_loop = loop

    if not loop.run_until_complete(_connect_to_pad()):
        connecting = False
        connection_failed = True
        return

    connected = True
    connecting = False
    try:
        loop.run_forever()
    finally:
        connected = False
        loop.close()


def _start_ble_thread():
    global connecting, connection_failed
    if connected or connecting:
        return
    connecting = True
    connection_failed = False
    threading.Thread(target=_ble_thread, daemon=True).start()

def _handle_disconnect(client):
    """Callback function to handle unexpected disconnections."""
    global connected, belt_running, connecting, connection_failed
    if connected: # Only log if we thought we were connected
        logging.warning("Device has disconnected unexpectedly.")
    connected = False
    belt_running = False
    connecting = False
    connection_failed = True

# ── Flask routes ────────────────────────────────────────────────────────
@app.route("/")
def root():
    if not connected:
        return render_template("connecting.html")
    if not session_active:
        return render_template("start_session.html")
    template = "active_session.html" if belt_running else "paused_session.html"
    return render_template(
        template,
        speed=current_speed_kmh * KMH_TO_MPH,
        distance=current_distance_km * KM_TO_MI,
        steps=current_steps,
        calories=current_calories,
    )


@app.route("/reconnect", endpoint="reconnect")
@app.route("/manual_reconnect", endpoint="manual_reconnect")
def reconnect():
    if not connected and not connecting:
        _start_ble_thread()
    return redirect(url_for("root"))


@app.route("/start")
def start_session():
    """Begin a new session: reset counters, start belt, launch stats monitor."""
    global session_active, belt_running, current_distance_km, current_steps, current_calories, resume_speed_kmh

    if not connected:
        return redirect(url_for("root"))

    current_distance_km = current_steps = current_calories = 0.0
    resume_speed_kmh = 2.0
    speed_history.clear() 

    session_active = True
    belt_running = True

    async def seq():
        try:
            await controller.start_belt()
            await asyncio.sleep(0.5)
            asyncio.create_task(_stats_monitor())
        except Exception as exc:
            logging.error(f"Start sequence error: {exc}")

    asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    return redirect(url_for("root"))


# ── Pause / Resume ───────────────────────────────────────────────────────

@app.route("/pause", endpoint="pause")
@app.route("/pause_session", endpoint="pause_session")
def pause_session():
    global belt_running, resume_speed_kmh
    if not belt_running:
        return redirect(url_for("root"))
    
    # Use the most recent speed from our history for manual pause
    if speed_history:
        resume_speed_kmh = speed_history[-1]

    belt_running = False
    asyncio.run_coroutine_threadsafe(controller.stop_belt(), ble_loop)
    return redirect(url_for("root"))


@app.route("/resume", endpoint="resume")
@app.route("/resume_session", endpoint="resume_session")
def resume_session():
    global belt_running, _auto_pause_grace_until, session_active # session_active ensures we only resume active sessions
    
    if not session_active: # Can't resume if no session was active
        logging.warning("Resume called but no active session.")
        return redirect(url_for("root"))

    if belt_running: # Already running, do nothing
        logging.info("Resume called but belt is already running.")
        return redirect(url_for("root"))

    # --- CRITICAL FIX: Optimistically set state for UI and grace period ---
    logging.info("Resume button clicked. Setting app state to active.")
    belt_running = True
    _auto_pause_grace_until = time.time() + 7 # Generous 7-second grace period for commands to take effect

    async def seq():
        try:
            logging.info("Attempting resume: Sending wake-up and start sequence to device...")
            
            # Standard wake-up and start sequence
            await controller.switch_mode(WalkingPad.MODE_STANDBY)
            await asyncio.sleep(0.5) 
            await controller.switch_mode(WalkingPad.MODE_MANUAL)
            await asyncio.sleep(0.5) 
            
            await controller.start_belt()
            await asyncio.sleep(0.5) 
            
            logging.info(f"Setting speed to {resume_speed_kmh:.1f} km/h.")
            await controller.change_speed(int(resume_speed_kmh * 10))
            await asyncio.sleep(0.5) # Allow speed change to propagate
            
            # Start the monitor if it wasn't running or to be sure
            asyncio.create_task(_stats_monitor())
            logging.info("Resume sequence commands sent, monitor ensured.")

        except Exception as exc:
            logging.error(f"Error during resume sequence, device may have disconnected: {exc}")
            _handle_disconnect(None) # This will set belt_running = False and connected = False
                                     # The frontend polling will then reload to the correct disconnected/connecting page.

    asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    # The redirect will now happen after belt_running is True in the main thread.
    return redirect(url_for("root"))


# ── Speed Controls ───────────────────────────────────────────────────────
@app.route("/decrease_speed")
def decrease_speed():
    """Decrease the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = max(MIN_SPEED_KMH, current_speed_kmh - SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/slow_speed")
def slow_speed():
    """Set the belt speed to a predefined slow walk speed."""
    if not belt_running:
        return redirect(url_for("root"))
    
    dev_speed = int(SLOW_WALK_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))

@app.route("/increase_speed")
def increase_speed():
    """Increase the belt speed by one step."""
    if not belt_running:
        return redirect(url_for("root"))

    new_speed_kmh = min(MAX_SPEED_KMH, current_speed_kmh + SPEED_STEP)
    dev_speed = int(new_speed_kmh * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


@app.route("/max_speed")
def max_speed():
    """Set the belt speed to maximum."""
    if not belt_running:
        return redirect(url_for("root"))
    
    dev_speed = int(MAX_SPEED_KMH * 10)
    asyncio.run_coroutine_threadsafe(controller.change_speed(dev_speed), ble_loop)
    return redirect(url_for("root"))


# ── Live JSON endpoint ───────────────────────────────────────────────────
@app.route("/stats", endpoint="get_stats")
def stats_json():

    data = dict(
        is_connected=connected,  # <-- ADD THIS LINE
        is_running=belt_running,
        speed=round(current_speed_kmh * KMH_TO_MPH, 1),
        distance=round(current_distance_km * KM_TO_MI, 2),
        steps=current_steps,
        calories=round(current_calories),
    )

    resp = make_response(jsonify(data))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Shutdown endpoint ──────────────────────────────────────────────────
@app.route("/shutdown", methods=['POST'])
def shutdown():
    """Forcefully shut down the Flask application process."""
    logging.info("Server shutting down via forceful exit...")
    os._exit(0)


# ── Kick off BLE thread ──────────────────────────────────────────────────
# The server is no longer started here. This just pre-starts the BLE thread.
_start_ble_thread()