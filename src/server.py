#!/usr/bin/env python3
"""
RoboTank - Serveur Flask
2 moteurs stepper tank + manette XIAO ESP32C6 via Zigbee+Série
+ Capteur ultrasonique HC-SR04 via ESP32 USB/Série

Brochage moteurs :
  GAUCHE : IN1=17 IN2=18 IN3=27 IN4=22
  DROITE : IN1=23 IN2=24 IN3=25 IN4=8

Manette (XIAO Zigbee receiver) :
  XIAO #1 branché en USB → /dev/ttyACM0
  Format reçu : "F" | "B" | "L" | "R" | "S" | "X"

Capteur distance :
  ESP32 HC-SR04 branché en USB sur le RPi → /dev/ttyUSB0
  Format reçu : "Distance : XX.XX cm"
"""

from flask import Flask, Response, jsonify, request, make_response
import threading, time, io

# ── GPIO ──────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO_OK = True
except ImportError:
    print("[WARN] RPi.GPIO absent — mode simulation")
    GPIO_OK = False

# ── Caméra ────────────────────────────────────────────
try:
    from picamera2 import Picamera2
    CAM_OK = True
except ImportError:
    print("[WARN] picamera2 absent — caméra désactivée")
    CAM_OK = False

# ── Manette XIAO Zigbee (série USB) ───────────────────
XIAO_PORT = "/dev/ttyACM0"   # XIAO #1 branché au Pi
XIAO_BAUD = 115200
xiao_connected = False

# ── Capteur distance (ESP32 via USB) ──────────────────
try:
    import serial
    SERIAL_OK = True
except ImportError:
    print("[WARN] pyserial absent — sudo apt install python3-serial")
    SERIAL_OK = False

app = Flask(__name__)

# ─────────────────────────────────────────────────────
#  CONFIG MOTEURS
# ─────────────────────────────────────────────────────
HALF_STEP = [
    [1,0,0,0],[1,1,0,0],[0,1,0,0],[0,1,1,0],
    [0,0,1,0],[0,0,1,1],[0,0,0,1],[1,0,0,1],
]
motors = {
    "left":  {"pins": [17, 18, 27, 22], "step_idx": 0},
    "right": {"pins": [23, 24, 25,  8], "step_idx": 0},
}
motor_lock    = threading.Lock()
motor_speed   = 0.0005  # vitesse maximale
_drive_thread = None
_running      = False
_cmd          = {"left": 0, "right": 0}

def gpio_setup():
    if not GPIO_OK: return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    all_pins = [p for m in motors.values() for p in m["pins"]]
    GPIO.setup(all_pins, GPIO.OUT)
    GPIO.output(all_pins, False)

def _step(side, direction):
    m = motors[side]
    m["step_idx"] = (m["step_idx"] + (1 if direction > 0 else -1)) % 8
    if GPIO_OK:
        for i, pin in enumerate(m["pins"]):
            GPIO.output(pin, HALF_STEP[m["step_idx"]][i])

def _motor_off(side):
    if GPIO_OK: GPIO.output(motors[side]["pins"], False)

def _drive_loop():
    global _running
    while _running:
        with motor_lock:
            ld = _cmd["left"]
            rd = _cmd["right"]
        if ld != 0: _step("left",  ld)
        if rd != 0: _step("right", rd)
        time.sleep(motor_speed if (ld or rd) else 0.01)
    _motor_off("left")
    _motor_off("right")

def drive(left_dir, right_dir):
    global _drive_thread, _running
    with motor_lock:
        _cmd["left"]  = left_dir
        _cmd["right"] = right_dir
    if left_dir == 0 and right_dir == 0:
        _motor_off("left"); _motor_off("right")
        return
    if not _running:
        _running = True
        _drive_thread = threading.Thread(target=_drive_loop, daemon=True)
        _drive_thread.start()

def stop_all():
    global _running
    _running = False
    with motor_lock:
        _cmd["left"] = _cmd["right"] = 0
    if _drive_thread: _drive_thread.join(timeout=0.5)
    _motor_off("left"); _motor_off("right")

def xy_to_drive(x, y):
    L = max(-1.0, min(1.0, y + x))
    R = max(-1.0, min(1.0, y - x))
    ld = 0 if abs(L) < 0.1 else (1 if L > 0 else -1)
    rd = 0 if abs(R) < 0.1 else (1 if R > 0 else -1)
    return ld, rd

# ─────────────────────────────────────────────────────
#  CAMÉRA
# ─────────────────────────────────────────────────────
camera       = None
cam_active   = False
cam_lock     = threading.Lock()
latest_frame = None

def start_camera():
    global camera, cam_active
    if cam_active: return True
    if not CAM_OK: return False
    try:
        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": (640, 480), "format": "RGB888"},
            controls={"FrameDurationLimits": (33333, 33333)}
        )
        camera.configure(config)
        cam_active = True
        threading.Thread(target=_cam_loop, daemon=True).start()
        return True
    except Exception as e:
        print(f"[ERROR] cam init: {e}")
        cam_active = False
        return False

def _cam_loop():
    global latest_frame, cam_active, camera
    try:
        camera.start()
        print("[INFO] Caméra démarrée")
        while cam_active:
            buf = io.BytesIO()
            camera.capture_file(buf, format='jpeg')
            buf.seek(0)
            with cam_lock: latest_frame = buf.read()
            time.sleep(0.04)
    except Exception as e:
        print(f"[ERROR] cam loop: {e}")
        cam_active = False
    finally:
        try: camera.stop(); camera.close()
        except: pass
        print("[INFO] Caméra arrêtée")

def stop_camera():
    global cam_active, latest_frame
    cam_active = False; latest_frame = None; time.sleep(0.3)

def _gen_frames():
    while cam_active:
        with cam_lock: f = latest_frame
        if f:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + f + b'\r\n')
        time.sleep(0.04)

# ─────────────────────────────────────────────────────
#  CAPTEUR DISTANCE — ESP32 HC-SR04 via USB série
# ─────────────────────────────────────────────────────
SERIAL_PORT  = "/dev/ttyUSB0"   # Change si besoin (/dev/ttyUSB1, etc.)
SERIAL_BAUD  = 115200

distance_cm       = -1.0   # -1 = pas encore de donnée
distance_lock     = threading.Lock()
serial_connected  = False

OBSTACLE_SEUIL    = 15.0   # cm — seuil déclenchement photo
obstacle_photo_lock = threading.Lock()
last_photo_time   = 0      # évite les rafales de photos

# ─────────────────────────────────────────────────────
#  ÉVITEMENT OBSTACLES
# ─────────────────────────────────────────────────────
OBSTACLE_SEUIL   = 15.0   # cm — seuil de détection
OBSTACLE_DELAI   = 2.0    # secondes avant de réagir
ROTATION_90_SEC  = 2.0    # durée pour 90° (doublé pour le poids du robot)
ROTATION_180_SEC = 4.0    # durée pour 180°

obstacle_first_seen = None
avoiding            = False
avoid_lock          = threading.Lock()

# Variables du mode autonome déclarées ici pour être accessibles partout
auto_mode          = False
auto_returning     = False
auto_lock          = threading.Lock()
auto_thread        = None
auto_override_time = 0.0
auto_start_x       = 0.0
auto_start_y       = 0.0

def avoidance_loop():
    """Surveille la distance et déclenche l'évitement automatique."""
    global obstacle_first_seen, avoiding

    while True:
        time.sleep(0.1)

        # Ne rien faire si mode autonome, scan ou exploration actifs
        if auto_mode or scan_active or explore_active:
            obstacle_first_seen = None
            continue

        # Ne rien faire si une manœuvre est déjà en cours
        with avoid_lock:
            if avoiding:
                continue

        with distance_lock:
            d = distance_cm

        obstacle_present = (0 < d < OBSTACLE_SEUIL)

        if obstacle_present:
            if obstacle_first_seen is None:
                obstacle_first_seen = time.time()
                print(f"[AVOID] Obstacle à {d:.1f} cm — décompte 2s...")
            elif time.time() - obstacle_first_seen >= OBSTACLE_DELAI:
                obstacle_first_seen = None
                print("[AVOID] ⚠️  Obstacle confirmé — manœuvre lancée !")
                threading.Thread(target=do_avoidance, daemon=True).start()
        else:
            if obstacle_first_seen is not None:
                print("[AVOID] Obstacle disparu — décompte annulé")
            obstacle_first_seen = None

def do_avoidance():
    """Séquence d'évitement : tourne 90° à droite, vérifie, tourne 180° si besoin."""
    global avoiding

    with avoid_lock:
        avoiding = True

    try:
        # 1. Stop immédiat
        drive(0, 0)
        time.sleep(0.3)

        # 2. Rotation droite 90° : chenille gauche avant, droite arrière
        print("[AVOID] ↻ Rotation droite 90°...")
        drive(1, -1)
        time.sleep(ROTATION_90_SEC)
        drive(0, 0)
        time.sleep(0.5)  # pause pour laisser le capteur se stabiliser

        # 3. Vérifie si obstacle toujours présent
        with distance_lock:
            d = distance_cm
        print(f"[AVOID] Distance après 90° : {d:.1f} cm")

        if 0 < d < OBSTACLE_SEUIL:
            # Obstacle encore là → rotation 180° supplémentaire à droite
            print("[AVOID] ↻↻ Obstacle toujours présent — rotation 180°...")
            drive(1, -1)
            time.sleep(ROTATION_180_SEC)
            drive(0, 0)
            print("[AVOID] ✅ Rotation 180° terminée — voie dégagée")
        else:
            print("[AVOID] ✅ Voie libre après 90° — reprise normale")

    except Exception as e:
        print(f"[AVOID] Erreur manœuvre : {e}")
        drive(0, 0)
    finally:
        with avoid_lock:
            avoiding = False

def serial_reader():
    """Thread qui lit en continu les données de l'ESP32 via USB."""
    global distance_cm, serial_connected

    if not SERIAL_OK:
        print("[SERIAL] pyserial non disponible — distance désactivée")
        return

    while True:
        try:
            print(f"[SERIAL] Connexion sur {SERIAL_PORT} à {SERIAL_BAUD} baud...")
            ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=2)
            serial_connected = True
            print(f"[SERIAL] ESP32 connecté ✓")

            while True:
                ligne = ser.readline().decode("utf-8", errors="ignore").strip()
                if not ligne:
                    continue

                # Format attendu : "Distance : 23.45 cm"
                if "Distance" in ligne and ":" in ligne:
                    try:
                        valeur = ligne.split(":")[1].replace("cm", "").strip()
                        new_dist = float(valeur)
                        with distance_lock:
                            distance_cm = new_dist
                        # Photo automatique si obstacle détecté
                        if new_dist < OBSTACLE_SEUIL:
                            _auto_photo()
                    except ValueError:
                        pass

                # Format alternatif : "Hors portée !"
                elif "Hors" in ligne:
                    with distance_lock:
                        distance_cm = -1.0

        except serial.SerialException as e:
            serial_connected = False
            print(f"[SERIAL] Erreur : {e} — nouvelle tentative dans 3s")
            time.sleep(3)
        except Exception as e:
            serial_connected = False
            print(f"[SERIAL] Erreur inattendue : {e}")
            time.sleep(3)

# ─────────────────────────────────────────────────────
#  PHOTO AUTOMATIQUE — obstacle détecté
# ─────────────────────────────────────────────────────
import os, base64
PHOTOS_DIR = os.path.expanduser("~/obstacle_photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

def _auto_photo():
    """Prend une photo sur obstacle — fonctionne même si le flux MJPEG est inactif."""
    global last_photo_time
    now = time.time()
    with obstacle_photo_lock:
        if now - last_photo_time < 3.0:
            print("[PHOTO] Trop tôt, photo ignorée")
            return
        last_photo_time = now

    jpeg_data = None

    # Cas 1 : caméra déjà active → utilise le dernier frame du flux
    if cam_active:
        with cam_lock:
            frame = latest_frame
        if frame and len(frame) > 0:
            jpeg_data = frame

    # Cas 2 : caméra inactive → capture one-shot via Picamera2
    if jpeg_data is None and CAM_OK:
        try:
            cam_tmp = Picamera2()
            cam_tmp.configure(cam_tmp.create_still_configuration())
            cam_tmp.start()
            time.sleep(0.3)  # laisser l'auto-exposition se stabiliser
            buf = io.BytesIO()
            cam_tmp.capture_file(buf, format='jpeg')
            cam_tmp.stop(); cam_tmp.close()
            buf.seek(0)
            jpeg_data = buf.read()
        except Exception as e:
            print(f"[PHOTO] Erreur capture one-shot : {e}")
            return

    if not jpeg_data or len(jpeg_data) == 0:
        print("[PHOTO] Pas de données image disponibles")
        return

    filename = time.strftime("obstacle_%Y%m%d_%H%M%S.jpg")
    filepath = os.path.join(PHOTOS_DIR, filename)
    try:
        with open(filepath, "wb") as f:
            f.write(jpeg_data)
        print(f"[PHOTO] ✅ Sauvegardée : {filename} ({len(jpeg_data)} bytes)")
    except Exception as e:
        print(f"[PHOTO] Erreur sauvegarde : {e}")

# ─────────────────────────────────────────────────────
#  MANETTE XIAO — lecture série USB (Zigbee receiver)
# ─────────────────────────────────────────────────────
def _process_cmd(cmd):
    """Traite une commande reçue : F/B/L/R/S/X"""
    global auto_override_time
    # Si mode autonome actif et commande non-stop → override manette
    if auto_mode and cmd not in ("S", "X"):
        auto_override_time = time.time()
        print(f"[AUTO] Override manette : {cmd} ({AUTO_OVERRIDE_SEC}s)")

    if   cmd == "F": drive(1,  1)   # avant
    elif cmd == "B": drive(-1, -1)  # arrière
    elif cmd == "L": drive(-1, 1)   # tourne gauche
    elif cmd == "R": drive(1, -1)   # tourne droite
    elif cmd == "S": drive(0,  0)   # stop
    elif cmd == "X": drive(0,  0)   # bouton = stop

def xiao_reader():
    """Thread qui lit en continu les commandes du XIAO via USB."""
    global xiao_connected
    if not SERIAL_OK:
        print("[XIAO] pyserial non disponible")
        return
    while True:
        try:
            print(f"[XIAO] Connexion sur {XIAO_PORT} à {XIAO_BAUD} baud...")
            ser = serial.Serial(XIAO_PORT, XIAO_BAUD, timeout=2)
            xiao_connected = True
            print("[XIAO] XIAO connecté ✓")
            while True:
                ligne = ser.readline().decode("utf-8", errors="ignore").strip()
                if ligne in ("F", "B", "L", "R", "S", "X"):
                    _process_cmd(ligne)
        except serial.SerialException as e:
            xiao_connected = False
            print(f"[XIAO] Erreur : {e} — nouvelle tentative dans 3s")
            time.sleep(3)
        except Exception as e:
            xiao_connected = False
            print(f"[XIAO] Erreur inattendue : {e}")
            time.sleep(3)

# ─────────────────────────────────────────────────────
#  ROUTES API
# ─────────────────────────────────────────────────────
@app.route('/api/drive', methods=['POST'])
def api_drive():
    d  = request.get_json()
    x  = float(d.get('x', 0))
    y  = float(d.get('y', 0))
    y  = -y
    ld, rd = xy_to_drive(x, y)
    drive(ld, rd)
    return jsonify({"L": ld, "R": rd})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    drive(0, 0)
    return jsonify({"status": "stopped"})

@app.route('/api/speed', methods=['POST'])
def api_speed():
    global motor_speed
    v = float(request.get_json().get('value', 5))
    motor_speed = max(0.0005, 0.01 - (v - 1) * 0.00105)
    return jsonify({"ms": round(motor_speed * 1000, 2)})

@app.route('/api/camera', methods=['POST'])
def api_camera():
    on = request.get_json().get('active', False)
    if on:
        ok = start_camera()
        return jsonify({"camera": ok})
    stop_camera()
    return jsonify({"camera": False})

@app.route('/api/last_photo')
def api_last_photo():
    """Retourne la dernière photo d'obstacle en base64."""
    try:
        photos = sorted([
            f for f in os.listdir(PHOTOS_DIR) if f.endswith('.jpg')
        ])
        if not photos:
            return jsonify({"photo": None})
        last = os.path.join(PHOTOS_DIR, photos[-1])
        with open(last, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return jsonify({
            "photo":    data,
            "filename": photos[-1],
            "total":    len(photos)
        })
    except Exception as e:
        return jsonify({"photo": None, "error": str(e)})

# ─────────────────────────────────────────────────────
#  CARTOGRAPHIE WiFi + DEAD RECKONING
# ─────────────────────────────────────────────────────
import math, subprocess, json as _json

# 28BYJ-48 : 512 demi-pas = 1 tour, roue Ø86mm
STEPS_PER_REV   = 512
WHEEL_CIRC_MM   = math.pi * 86
DIST_PER_STEP   = WHEEL_CIRC_MM / STEPS_PER_REV
WHEELBASE_MM    = 300.0
ROT_FACTOR      = 5.0     # calibré empiriquement
CELL_SIZE_MM    = 100                 # case 10cm — adapté au scan
GRID_W          = 60                  # 60×10cm = 6m de large
GRID_H          = 60

# États des cases
CELL_UNKNOWN  = 0
CELL_FREE     = 1
CELL_OBSTACLE = 2

# Grille occupation + grille RSSI séparée
map_grid      = [[CELL_UNKNOWN]*GRID_W for _ in range(GRID_H)]
rssi_grid     = [[None]*GRID_W for _ in range(GRID_H)]   # None = pas de mesure
map_lock      = threading.Lock()
pos_x_mm      = GRID_W * CELL_SIZE_MM / 2.0
pos_y_mm      = GRID_H * CELL_SIZE_MM / 2.0
pos_angle_rad = 0.0
steps_left    = 0
steps_right   = 0
pos_lock      = threading.Lock()

explore_active = False
explore_thread = None

def _update_position(dl, dr):
    """Met à jour la position via dead reckoning."""
    global pos_x_mm, pos_y_mm, pos_angle_rad
    dist_l = dl * DIST_PER_STEP
    dist_r = dr * DIST_PER_STEP
    dist   = (dist_l + dist_r) / 2.0
    dangle = (dist_r - dist_l) / WHEELBASE_MM
    with pos_lock:
        pos_angle_rad += dangle
        pos_x_mm += dist * math.sin(pos_angle_rad)
        pos_y_mm -= dist * math.cos(pos_angle_rad)

def _get_cell():
    """Retourne la case courante (cx, cy)."""
    with pos_lock:
        cx = int(pos_x_mm / CELL_SIZE_MM)
        cy = int(pos_y_mm / CELL_SIZE_MM)
    return max(0, min(GRID_W-1, cx)), max(0, min(GRID_H-1, cy))

def _mark_cell(state):
    """Marque la case actuelle dans la grille."""
    cx, cy = _get_cell()
    with map_lock:
        map_grid[cy][cx] = state

def _measure_rssi():
    """Mesure le RSSI et l'enregistre dans la case courante."""
    rssi = _get_rssi()
    if rssi is not None:
        cx, cy = _get_cell()
        with map_lock:
            # Moyenne glissante si plusieurs mesures sur la même case
            prev = rssi_grid[cy][cx]
            rssi_grid[cy][cx] = rssi if prev is None else int((prev + rssi) / 2)
    return rssi

def _get_rssi():
    """Lit le RSSI WiFi via iwconfig (en dBm)."""
    try:
        out = subprocess.check_output(["iwconfig", "wlan0"], stderr=subprocess.DEVNULL).decode()
        for part in out.split():
            if "level=" in part:
                val = part.split("=")[1]
                return int(val.split("/")[0])  # gère "level=-65" et "level=45/100"
    except Exception:
        pass
    # Fallback via /proc/net/wireless
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if "wlan0" in line:
                    parts = line.split()
                    return int(float(parts[3].rstrip('.')))
    except Exception:
        pass
    return None

def _get_motor_speed():
    """Retourne toujours la vitesse max en mode auto/scan."""
    if auto_mode or scan_active or explore_active:
        return 0.0005
    return motor_speed

def _drive_steps(left_dir, right_dir, n_steps):
    """Fait tourner les moteurs n_steps pas."""
    drive(left_dir, right_dir)
    # ×1.5 de buffer pour compenser l'overhead thread Python
    time.sleep(n_steps * 0.0005 * 1.5)
    drive(0, 0)
    time.sleep(0.05)
    _update_position(left_dir * n_steps, right_dir * n_steps)

def explore_loop():
    """Thread d'exploration autonome : avance + cartographie RSSI (sans évitement)."""
    global explore_active
    STEPS_FORWARD   = 200
    STEPS_ROTATE_90 = int((math.pi/2 * WHEELBASE_MM/2) / DIST_PER_STEP * ROT_FACTOR)

    print("[MAP] Exploration démarrée")
    while explore_active:
        _mark_cell(CELL_FREE)
        _drive_steps(*SCAN_FWD, STEPS_FORWARD)
        rssi = _measure_rssi()
        if rssi:
            print(f"[MAP] RSSI={rssi}dBm @ ({int(pos_x_mm/CELL_SIZE_MM)},{int(pos_y_mm/CELL_SIZE_MM)})")
        time.sleep(0.1)

    drive(0, 0)
    print("[MAP] Exploration terminée")

def reset_map():
    global map_grid, rssi_grid, pos_x_mm, pos_y_mm, pos_angle_rad
    with map_lock:
        map_grid  = [[CELL_UNKNOWN]*GRID_W for _ in range(GRID_H)]
        rssi_grid = [[None]*GRID_W for _ in range(GRID_H)]
    with pos_lock:
        pos_x_mm      = GRID_W * CELL_SIZE_MM / 2.0
        pos_y_mm      = GRID_H * CELL_SIZE_MM / 2.0
        pos_angle_rad = 0.0

# ─────────────────────────────────────────────────────
#  MODE AUTONOME COMPLET
# ─────────────────────────────────────────────────────
AUTO_OBSTACLE_CM    = 20.0   # cm — seuil détection obstacle
AUTO_STEPS_FWD      = 150    # demi-pas par mouvement avant (~8cm)
AUTO_STEPS_ROT90    = int((math.pi/2 * WHEELBASE_MM/2) / DIST_PER_STEP * ROT_FACTOR)
AUTO_OVERRIDE_SEC   = 3.0    # secondes de pause après input manette
AUTO_RETURN_TOL_MM  = 80.0   # tolérance retour au départ (8cm)
AUTO_ANGLE_TOL_RAD  = 0.15   # tolérance angulaire (~8°)

DIR_FWD   = ( 1,  1)   # avant — identique à _process_cmd("F") -> drive(1,1)
DIR_BWD   = (-1, -1)   # arrière
DIR_ROT_R = ( 1, -1)
DIR_ROT_L = (-1,  1)

def _auto_drive_steps(ld, rd, n):
    """Déplacement n pas avec mise à jour position — interruptible."""
    drive(ld, rd)
    steps_done = 0
    while steps_done < n:
        if not auto_mode:
            drive(0, 0)
            return False
        # Override manette : pause si commande récente
        if time.time() - auto_override_time < AUTO_OVERRIDE_SEC:
            drive(0, 0)
            time.sleep(0.1)
            continue
        time.sleep(0.0005 * 10 * 1.5)  # vitesse max + buffer
        steps_done += 10
    drive(0, 0)
    _update_position(ld * n, rd * n)
    return True

def _auto_obstacle_ahead():
    with distance_lock:
        d = distance_cm
    return 0 < d < AUTO_OBSTACLE_CM

def _auto_rotate_to_angle(target_rad):
    """Tourne vers un angle cible (dead reckoning)."""
    with pos_lock:
        current = pos_angle_rad

    # Calcule le delta angulaire le plus court
    delta = (target_rad - current + math.pi) % (2 * math.pi) - math.pi

    # Convertit en pas
    steps = int(abs(delta) * WHEELBASE_MM / 2 / DIST_PER_STEP)
    if steps < 5:
        return True  # déjà orienté

    ld, rd = (1, -1) if delta > 0 else (-1, 1)
    return _auto_drive_steps(ld, rd, steps)

def _auto_return_to_start():
    """Navigue vers le point de départ par dead reckoning."""
    global auto_returning
    auto_returning = True
    print("[AUTO] Retour au point de départ...")

    max_iter = 50
    for _ in range(max_iter):
        if not auto_mode:
            break

        with pos_lock:
            dx = auto_start_x - pos_x_mm
            dy = auto_start_y - pos_y_mm
            dist = math.hypot(dx, dy)

        if dist < AUTO_RETURN_TOL_MM:
            print("[AUTO] Point de départ atteint !")
            drive(0, 0)
            break

        # Calcule l'angle vers le départ
        # Y inversé car y++ = bas sur la grille
        target_angle = math.atan2(dx, -dy)

        # Oriente vers le départ
        if not _auto_rotate_to_angle(target_angle):
            break

        time.sleep(0.2)

        # Avance d'un pas vers le départ
        steps = min(AUTO_STEPS_FWD, int(dist / DIST_PER_STEP))
        if not _auto_drive_steps(*SCAN_FWD, max(steps, 50)):
            break

        # Évite les obstacles pendant le retour
        if _auto_obstacle_ahead():
            drive(0, 0)
            time.sleep(0.3)
            _auto_drive_steps(*DIR_ROT_R, AUTO_STEPS_ROT90 // 2)
            time.sleep(0.2)

    auto_returning = False

def auto_loop():
    """Thread principal du mode autonome."""
    global auto_mode, auto_start_x, auto_start_y

    with pos_lock:
        auto_start_x = pos_x_mm
        auto_start_y = pos_y_mm
    print(f"[AUTO] Départ enregistré : ({auto_start_x:.0f}, {auto_start_y:.0f}) mm")

    consecutive_obstacles = 0

    while auto_mode:
        # --- Override manette ---
        if time.time() - auto_override_time < AUTO_OVERRIDE_SEC:
            time.sleep(0.05)
            continue

        # --- Obstacle devant ---
        if _auto_obstacle_ahead():
            drive(0, 0)
            consecutive_obstacles += 1
            _auto_photo()

            # Choisit le sens de rotation (alterne G/D)
            rot = DIR_ROT_R if consecutive_obstacles % 2 == 0 else DIR_ROT_L

            # Tourne par petits pas JUSQU'À ce que le capteur ne détecte plus d'obstacle
            # Limite de sécurité : max 180° (2×ROT90 pas)
            steps_done = 0
            max_steps = AUTO_STEPS_ROT90 * 2
            step_chunk = max(AUTO_STEPS_ROT90 // 8, 20)  # ~11° par itération

            while _auto_obstacle_ahead() and steps_done < max_steps and auto_mode:
                _auto_drive_steps(*rot, step_chunk)
                steps_done += step_chunk
                time.sleep(0.15)  # laisse le temps au capteur de se stabiliser

            # Si toujours bloqué après 180° → demi-tour complet dans l'autre sens
            if _auto_obstacle_ahead() and auto_mode:
                rot2 = DIR_ROT_L if consecutive_obstacles % 2 == 0 else DIR_ROT_R
                steps2 = 0
                while _auto_obstacle_ahead() and steps2 < max_steps and auto_mode:
                    _auto_drive_steps(*rot2, step_chunk)
                    steps2 += step_chunk
                    time.sleep(0.15)

            time.sleep(0.2)
        else:
            # --- Avance ---
            consecutive_obstacles = 0
            _mark_cell(CELL_FREE)
            _auto_drive_steps(*SCAN_FWD, AUTO_STEPS_FWD)
            time.sleep(0.05)

    drive(0, 0)
    print("[AUTO] Mode autonome arrêté")

# ─────────────────────────────────────────────────────
#  SCAN RSSI SYSTÉMATIQUE — grille 1m² (serpentin)
# ─────────────────────────────────────────────────────
SCAN_COLS       = 5
SCAN_ROWS       = 5
SCAN_STEP_MM    = 1000       # 5 pts × 4 intervalles × 25cm = 1m réel (ajuster)
SCAN_STEPS_MOVE = int(SCAN_STEP_MM / DIST_PER_STEP)
SCAN_RSSI_N     = 5

# Direction pour scan/explore (inversée par rapport au manuel)
SCAN_FWD   = (-1, -1)
SCAN_ROT_R = (-1,  1)
SCAN_ROT_L = ( 1, -1)

scan_active   = False
scan_progress = {"current": 0, "total": SCAN_COLS * SCAN_ROWS, "status": "idle"}
scan_results  = []   # liste de {"col":c, "row":r, "x_mm":x, "y_mm":y, "rssi":val}

def _scan_drive(ld, rd, n):
    """Déplacement contrôlé pour le scan — vitesse max + buffer timing."""
    drive(ld, rd)
    time.sleep(n * 0.0005 * 1.5)   # ×1.5 buffer pour que les pas soient réels
    drive(0, 0)
    time.sleep(0.5)   # stabilisation mécanique + capteur
    _update_position(ld * n, rd * n)

def _scan_measure_rssi():
    """Moyenne de SCAN_RSSI_N mesures RSSI espacées de 200ms."""
    vals = []
    for _ in range(SCAN_RSSI_N):
        r = _get_rssi()
        if r is not None:
            vals.append(r)
        time.sleep(0.2)
    return int(sum(vals) / len(vals)) if vals else None

def rssi_scan_loop():
    """
    Parcours en serpentin sur une grille SCAN_COLS × SCAN_ROWS.
    Chemin :
      col 0 : bas → haut
      col 1 : haut → bas
      col 2 : bas → haut  ...etc
    Entre deux colonnes : rotation 90° + avance SCAN_STEP_MM + rotation -90°
    """
    global scan_active, scan_progress, scan_results

    scan_results = []
    scan_progress["current"] = 0
    scan_progress["status"]  = "running"
    total = SCAN_COLS * SCAN_ROWS
    scan_progress["total"] = total

    print(f"[SCAN] Démarrage grille {SCAN_COLS}×{SCAN_ROWS} — {SCAN_STEP_MM}mm — {total} points")

    # Enregistre le point de départ (coin bas-gauche)
    with pos_lock:
        start_x = pos_x_mm
        start_y = pos_y_mm

    for col in range(SCAN_COLS):
        if not scan_active:
            break

        # Sens de parcours : pair=haut, impair=bas
        rows = range(SCAN_ROWS) if col % 2 == 0 else range(SCAN_ROWS - 1, -1, -1)

        for row in rows:
            if not scan_active:
                break

            # ── Mesure au point courant ──
            rssi = _scan_measure_rssi()
            with pos_lock:
                px, py = pos_x_mm, pos_y_mm

            result = {"col": col, "row": row, "x_mm": round(px), "y_mm": round(py), "rssi": rssi}
            scan_results.append(result)

            # Enregistre dans la grille rssi_grid
            if rssi is not None:
                cx = max(0, min(GRID_W-1, int(px / CELL_SIZE_MM)))
                cy = max(0, min(GRID_H-1, int(py / CELL_SIZE_MM)))
                with map_lock:
                    rssi_grid[cy][cx] = rssi
                    map_grid[cy][cx]  = CELL_FREE

            scan_progress["current"] += 1
            pct = int(scan_progress["current"] / total * 100)
            print(f"[SCAN] {scan_progress['current']}/{total} ({pct}%) "
                  f"col={col} row={row} RSSI={rssi}dBm @ ({px:.0f},{py:.0f})mm")

            # ── Avance d'une ligne (sauf dernier point de la colonne) ──
            is_last_row = (row == SCAN_ROWS - 1) if col % 2 == 0 else (row == 0)
            if not is_last_row and scan_active:
                _scan_drive(*SCAN_FWD, SCAN_STEPS_MOVE)

        # ── Passage à la colonne suivante (sauf dernière) ──
        if col < SCAN_COLS - 1 and scan_active:
            # Tourne 90° vers la droite
            rot90_steps = int((math.pi/2 * WHEELBASE_MM/2) / DIST_PER_STEP * ROT_FACTOR)
            _scan_drive(*SCAN_ROT_R, rot90_steps)
            time.sleep(0.1)
            # Avance d'un pas colonne
            _scan_drive(*SCAN_FWD, SCAN_STEPS_MOVE)
            time.sleep(0.1)
            # Tourne 90° dans la direction de la prochaine colonne
            # rotation fin de colonne gérée directement
            _scan_drive(*SCAN_ROT_L, rot90_steps)
            time.sleep(0.1)

    drive(0, 0)
    scan_active = False

    if scan_progress["current"] == total:
        scan_progress["status"] = "done"
        print(f"[SCAN] Terminé — {len(scan_results)} points, "
              f"RSSI moy={int(sum(r['rssi'] for r in scan_results if r['rssi'])/max(1,len([r for r in scan_results if r['rssi']])))}")
    else:
        scan_progress["status"] = "stopped"
        print("[SCAN] Arrêté par l'utilisateur")

# ─────────────────────────────────────────────────────
#  ROUTES MODE AUTONOME
# ─────────────────────────────────────────────────────
@app.route('/api/auto', methods=['POST'])
def api_auto():
    global auto_mode, auto_thread
    data   = request.get_json() or {}
    action = data.get('action', 'start')

    if action == 'start' and not auto_mode:
        auto_mode   = True
        auto_thread = threading.Thread(target=auto_loop, daemon=True)
        auto_thread.start()
        return jsonify({"auto": True, "returning": False})

    elif action == 'stop':
        auto_mode = False
        return jsonify({"auto": False})

    elif action == 'return' and auto_mode:
        # Déclenche le retour au point de départ
        threading.Thread(target=_auto_return_to_start, daemon=True).start()
        return jsonify({"auto": True, "returning": True})

    return jsonify({"auto": auto_mode, "returning": auto_returning})


@app.route('/api/scan', methods=['POST'])
def api_scan():
    global scan_active
    data   = request.get_json() or {}
    action = data.get('action', 'start')

    if action == 'start' and not scan_active:
        scan_active = True
        threading.Thread(target=rssi_scan_loop, daemon=True).start()
        return jsonify({"scan": True, "progress": scan_progress})

    elif action == 'stop':
        scan_active = False
        return jsonify({"scan": False})

    return jsonify({"scan": scan_active, "progress": scan_progress})

@app.route('/api/scan/status')
def api_scan_status():
    """Retourne la progression et les résultats du scan."""
    vals = [r['rssi'] for r in scan_results if r['rssi'] is not None]
    return jsonify({
        "scan":     scan_active,
        "progress": scan_progress,
        "results":  scan_results,
        "stats": {
            "count": len(vals),
            "min":   min(vals) if vals else None,
            "max":   max(vals) if vals else None,
            "avg":   int(sum(vals)/len(vals)) if vals else None,
        } if vals else {}
    })


def api_explore():
    global explore_active, explore_thread
    data   = request.get_json() or {}
    action = data.get('action', 'start')
    if action == 'start' and not explore_active:
        explore_active = True
        explore_thread = threading.Thread(target=explore_loop, daemon=True)
        explore_thread.start()
        return jsonify({"explore": True})
    elif action == 'stop':
        explore_active = False
        return jsonify({"explore": False})
    elif action == 'reset':
        explore_active = False
        reset_map()
        return jsonify({"explore": False, "reset": True})
    return jsonify({"explore": explore_active})

@app.route('/api/map')
def api_map():
    with map_lock:
        grid_copy = [row[:] for row in map_grid]
        rssi_copy = [row[:] for row in rssi_grid]
    with pos_lock:
        px = pos_x_mm / CELL_SIZE_MM
        py = pos_y_mm / CELL_SIZE_MM
        pa = math.degrees(pos_angle_rad)
    # Stats RSSI
    vals = [v for row in rssi_copy for v in row if v is not None]
    rssi_stats = {
        "min": min(vals), "max": max(vals),
        "avg": int(sum(vals)/len(vals)), "count": len(vals)
    } if vals else {}
    return jsonify({
        "grid":       grid_copy,
        "rssi":       rssi_copy,
        "width":      GRID_W,
        "height":     GRID_H,
        "robot":      {"x": round(px, 1), "y": round(py, 1), "angle": round(pa, 1)},
        "cell_size_mm": CELL_SIZE_MM,
        "rssi_stats": rssi_stats,
    })

@app.route('/api/distance')
def api_distance():
    """Retourne la dernière distance mesurée par le HC-SR04."""
    with distance_lock:
        d = distance_cm
    return jsonify({
        "distance":  round(d, 2) if d >= 0 else None,
        "unite":     "cm",
        "hors_portee": d < 0,
        "connecte":  serial_connected
    })

@app.route('/api/status')
def api_status():
    with distance_lock:
        d = distance_cm
    return jsonify({
        "running":        _running,
        "left":           _cmd["left"],
        "right":          _cmd["right"],
        "camera":         cam_active,
        "xiao_connected": xiao_connected,
        "gpio":           GPIO_OK,
        "cam_hw":         CAM_OK,
        "distance_cm":    round(d, 2) if d >= 0 else None,
        "serial":         serial_connected,
        "avoiding":       avoiding,
        "auto_mode":      auto_mode,
        "auto_returning": auto_returning,
    })

@app.route('/video_feed')
def video_feed():
    if not cam_active:
        return Response("Caméra inactive", status=503)
    return Response(_gen_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index():
    with open("index.html") as f: content = f.read()
    resp = make_response(content)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp

# ─────────────────────────────────────────────────────
if __name__ == '__main__':
    gpio_setup()

    # Manette XIAO via Zigbee+Série
    threading.Thread(target=xiao_reader, daemon=True).start()

    # Capteur distance HC-SR04 via ESP32 USB
    threading.Thread(target=serial_reader, daemon=True).start()

    # Évitement automatique d'obstacles
    threading.Thread(target=avoidance_loop, daemon=True).start()

    print("╔══════════════════════════════════════════╗")
    print("║  RoboTank  —  http://0.0.0.0:5000         ║")
    print("║  Gauche : GPIO 17 18 27 22                ║")
    print("║  Droite : GPIO 23 24 25  8                ║")
    print("║  XIAO   : /dev/ttyACM0 (Zigbee)          ║")
    print("║  Série  : /dev/ttyUSB0 (HC-SR04)         ║")
    print("║  Auto   : /api/auto (start/stop/return)  ║")
    print("╚══════════════════════════════════════════╝")
    try:
        app.run(host='0.0.0.0', port=5000, threaded=True, debug=False)
    finally:
        stop_all()
        stop_camera()
        if GPIO_OK: GPIO.cleanup()
        print("[INFO] GPIO nettoyé.")
