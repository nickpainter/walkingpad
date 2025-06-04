import asyncio
import threading
from threading import Timer # Add this import
import webbrowser # Add this import
from flask import Flask, render_template, redirect, url_for, jsonify, make_response
from bleak import BleakScanner
from ph4_walkingpad.pad import Controller, WalkingPad

"""WalkingPad Controller – clean edition (2025‑06‑03)

* Single copy of every route handler
* Uses controller.latest_status for live stats (no request packets)
* Prints debug lines so we can verify data flow
* Threaded dev server so /stats fetches don’t block
"""

# ── Conversion constants ─────────────────────────────────────────────────
KM_TO_MI = 0.621371
KMH_TO_MPH = 0.621371
KCAL_PER_MILE = 95  # rough kcal per mile


def kcal_estimate(miles: float) -> float:
    return KCAL_PER_MILE * miles


# ── Flask & global state ────────────────────────────────────────────────
app = Flask(__name__)

connected = connecting = connection_failed = False
ble_loop: asyncio.AbstractEventLoop | None = None
controller: Controller | None = None
_pad_address: str | None = None  # Add this line

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

    # 1. If we have a known address, try to connect directly first.
    if _pad_address:
        print(f"[INFO] Attempting to connect to known address: {_pad_address}", flush=True)
        try:
            # Use a short timeout for direct connection attempts
            dev = await BleakScanner.find_device_by_address(_pad_address, timeout=5)
        except Exception as exc:
            print(f"[WARN] Failed to find device by address: {exc}", flush=True)
            dev = None

    # 2. If direct connection failed or we have no address, scan by name.
    if not dev:
        print("[INFO] Scanning for device by name 'WalkingPad'...", flush=True)
        try:
            dev = await BleakScanner.find_device_by_name("WalkingPad", timeout=10)
        except Exception as exc:
            print(f"[WARN] Failed to find device by name: {exc}", flush=True)
            dev = None

    # 3. If we found a device, connect and save its address.
    if not dev:
        print("[ERR] Could not find WalkingPad. Ensure it is on and in range.", flush=True)
        _pad_address = None  # Clear address if connection fails
        return False

    # Save the address for future connections
    _pad_address = dev.address
    print(f"[INFO] Device found! Address: {_pad_address}", flush=True)

    controller = Controller()
    await controller.run(dev.address)
    await controller.switch_mode(WalkingPad.MODE_MANUAL)

    # local callback to capture every pushed status packet
    def _status_cb(_sender, st):
        try:
            if isinstance(st, dict):
                dist  = st.get("dist", 0)
                steps = st.get("steps", 0)
                speed = st.get("speed", 0)
            else:  # WalkingPadCurStatus object
                dist  = getattr(st, "dist", 0)
                steps = getattr(st, "steps", 0)
                speed = getattr(st, "speed", 0)
            process_status_packet(dist, steps, speed)
            print(f"DBG(push) d={dist} s={steps} v={speed}", flush=True)
        except Exception as exc:
            print("[WARN] status_cb:", exc, flush=True)

    controller.on_cur_status_received = _status_cb

    # some versions need notifications enabled explicitly
    if hasattr(controller, "enable_notifications"):
        try:
            await controller.enable_notifications()
        except Exception as exc:
            print("[WARN] enable_notifications failed:", exc, flush=True)
    return True


def process_status_packet(dev_dist, dev_steps, dev_speed):
    """Update cumulative stats from raw values (called by callback)."""
    global current_speed_kmh, current_distance_km, current_steps, current_calories
    global _last_dev_dist, _last_dev_steps

    if dev_dist < _last_dev_dist:
        _last_dev_dist = 0
    current_distance_km += (dev_dist - _last_dev_dist) / 100.0
    _last_dev_dist = dev_dist

    if dev_steps < _last_dev_steps:
        _last_dev_steps = 0
    current_steps += dev_steps - _last_dev_steps
    _last_dev_steps = dev_steps

    current_speed_kmh = dev_speed / 10.0
    current_calories = kcal_estimate(current_distance_km * KM_TO_MI)


async def _stats_monitor():
    """Active monitor: explicitly request a status packet every second.
    Works even on firmware that does not push notifications."""
    print("[MON] monitor started", flush=True)
    while belt_running:
        try:
            status = await controller.ask_stats()
            if status:
                if isinstance(status, dict):
                    dist  = status.get("dist", 0)
                    steps = status.get("steps", 0)
                    speed = status.get("speed", 0)
                else:
                    dist  = getattr(status, "dist", 0)
                    steps = getattr(status, "steps", 0)
                    speed = getattr(status, "speed", 0)
                process_status_packet(dist, steps, speed)
                print("DBG", status, flush=True)
        except Exception as exc:
            print("[WARN] ask_stats:", exc, flush=True)
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

    # reset totals
    current_distance_km = current_steps = current_calories = 0.0
    resume_speed_kmh = 2.0
    session_active = True
    belt_running = True

    async def seq():
        try:
            await controller.start_belt()
            await asyncio.sleep(0.5)
            asyncio.create_task(_stats_monitor())
        except Exception as exc:
            print("[ERR] start seq:", exc, flush=True)

    asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    return redirect(url_for("root"))



# ── Pause / Resume ───────────────────────────────────────────────────────
@app.route("/pause", endpoint="pause")
@app.route("/pause_session", endpoint="pause_session")
def pause_session():
    global belt_running, resume_speed_kmh
    if not belt_running:
        return redirect(url_for("root"))
    resume_speed_kmh = max(current_speed_kmh, 2.0)
    belt_running = False
    asyncio.run_coroutine_threadsafe(controller.stop_belt(), ble_loop)
    return redirect(url_for("root"))


@app.route("/resume", endpoint="resume")
@app.route("/resume_session", endpoint="resume_session")
def resume_session():
    global belt_running
    if belt_running or not session_active:
        return redirect(url_for("root"))
    belt_running = True

    async def seq():
        try:
            await controller.start_belt()
            await asyncio.sleep(0.5)
            # Set speed to the value stored before pausing
            await controller.change_speed(int(resume_speed_kmh * 10))
            await asyncio.sleep(0.5)
            asyncio.create_task(_stats_monitor())
        except Exception as exc:
            print("[ERR] resume seq:", exc, flush=True)

    asyncio.run_coroutine_threadsafe(seq(), ble_loop)
    return redirect(url_for("root"))





# ── Live JSON endpoint ───────────────────────────────────────────────────
@app.route("/stats", endpoint="get_stats")
def stats_json():
    data = dict(
        speed=round(current_speed_kmh * KMH_TO_MPH, 1),
        distance=round(current_distance_km * KM_TO_MI, 2),
        steps=current_steps,
        calories=round(current_calories),
    )
    resp = make_response(jsonify(data))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Kick off BLE thread & run Flask dev server ──────────────────────────
# _start_ble_thread() # <--- REMOVE OR COMMENT OUT THIS LINE

if __name__ == "__main__":
    # Function to open the browser (from previous request)
    def open_browser():
        webbrowser.open_new("http://127.0.0.1:5000")

    # Start the Flask app after a 1-second delay to open the browser
    Timer(1, open_browser).start()
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
