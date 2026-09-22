import sys
import os

# Explizite AppUserModelID so früh wie möglich via ctypes für Windows-Taskleiste setzen
if sys.platform == "win32":
    try:
        import ctypes
        myappid = "Lernfabrik.AndonPruefstation.Camera.1.0"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

import glob
import time
import logging
import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal, Qt, Slot
from PySide6.QtGui import QImage, QPixmap, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QPushButton, QVBoxLayout, QHBoxLayout, QFrame, QSizePolicy, QProgressBar
)
import json
import openvino as ov

# --- Verzeichnisse & Logging Setup ---
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SRC_DIR)

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "andon_system.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logging.info("Starting Andon System...")
logging.info(f"Python executable: {sys.executable}")
logging.info(f"Current working dir: {os.getcwd()}")
logging.info(f"Project root: {PROJECT_ROOT}")
logging.info(f"Script location: {os.path.abspath(__file__)}")

# --- Parameter ---
THRESHOLD_VALUE = 30
PRESENCE_TOLERANCE = 25000   # Benötigt echtes Bauteil (nicht nur Rauschen/Schatten)
RESET_TOLERANCE = 8000       # Sicherer Wert für leeren Tisch
MOVEMENT_TOLERANCE = 150
SETTLE_TIME = 3.5            # Wartezeit bis zur Messung (Puffer für Kamera-Fokus)

ROI_W = 700
ROI_H = 750
ROI_X = (1920 - ROI_W) // 2
ROI_Y = (1080 - ROI_H) // 2


class CameraWorker(QThread):
    frame_ready = Signal(np.ndarray)
    status_changed = Signal(str, str)  # Text, Farbcode
    result_ready = Signal(bool, float)  # is_defect, score

    def __init__(self):
        super().__init__()
        self.running = True
        self.calibrate_bg_requested = True
        self.bg_gray = None
        self.compiled_model = None
        self.output_score_layer = None
        self.threshold = 0.5
        self.ui_ready = True

        # YOLO Kugel-Erkennung
        self.yolo_model = None
        self.yolo_output_layer = None

        # Persistente Inspektionsdaten für dauerhaftes Overlay im Stream
        self.last_detected_balls = []
        self.last_pin_box = None
        self.last_pin_ok = False

    def load_model(self):
        # Basisverzeichnis ermitteln (Projekt-Hauptverzeichnis)
        base_dir = PROJECT_ROOT

        # 1. Padim-Modell suchen
        search_path = os.path.join(base_dir, "models", "padim", "*.xml")
        found = glob.glob(search_path)
        if not found:
            # Fallback falls alternative Trainingsstruktur vorhanden ist
            search_path = os.path.join(base_dir, "results", "Padim", "schluesselanhaenger", "*", "weights", "openvino", "model.xml")
            found = glob.glob(search_path)
            if not found:
                logging.error("FEHLER: Kein Padim-Modell gefunden!")
                return False

        found.sort()
        xml_path = found[-1]
        logging.info(f"Loaded Padim model from: {xml_path}")

        # Fester Schwellenwert: Werte bis 0.70 gelten als Gut-Teil
        self.threshold = 0.70

        core = ov.Core()
        model = core.read_model(model=xml_path)
        self.compiled_model = core.compile_model(model=model, device_name="CPU")

        for out in self.compiled_model.outputs:
            if "score" in out.get_any_name().lower():
                self.output_score_layer = out
                break
        if self.output_score_layer is None:
            self.output_score_layer = self.compiled_model.output(0)

        # 2. YOLO Kugel-Modell dynamisch suchen
        yolo_search = glob.glob(os.path.join(base_dir, "models", "kugel", "*.xml"))
        if not yolo_search:
            logging.warning("WARNUNG: Kein Kugel-Modell im Ordner models/kugel/ gefunden!")
            return False

        yolo_xml = yolo_search[0]
        logging.info(f"Loaded YOLO model from: {yolo_xml}")
        yolo_ov_model = core.read_model(model=yolo_xml)
        self.yolo_model = core.compile_model(model=yolo_ov_model, device_name="CPU")
        self.yolo_output_layer = self.yolo_model.output(0)

        return True

    def request_bg_calibration(self):
        self.calibrate_bg_requested = True

    def detect_balls(self, roi):
        if self.yolo_model is None:
            return []

        h_orig, w_orig = roi.shape[:2]

        # 1. Preprocessing für YOLO (RGB, 640x640, [0..1], NCHW)
        img_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
        img_resized = cv2.resize(img_rgb, (640, 640))
        input_tensor = (img_resized.astype(np.float32) / 255.0).transpose((2, 0, 1))
        input_tensor = np.expand_dims(input_tensor, axis=0)

        # 2. Native Inferenz via OpenVINO
        preds = self.yolo_model([input_tensor])[self.yolo_output_layer]
        preds = np.squeeze(preds)  # Shape: (5, 8400) -> [cx, cy, w, h, conf]
        preds = preds.T            # Shape: (8400, 5)

        boxes = []
        confidences = []
        x_scale = w_orig / 640.0
        y_scale = h_orig / 640.0

        for row in preds:
            conf = float(row[4])
            if conf >= 0.40:
                cx, cy, bw, bh = row[0], row[1], row[2], row[3]
                x1 = int((cx - bw / 2.0) * x_scale)
                y1 = int((cy - bh / 2.0) * y_scale)
                w_box = int(bw * x_scale)
                h_box = int(bh * y_scale)
                boxes.append([x1, y1, w_box, h_box])
                confidences.append(conf)

        # 3. Non-Maximum Suppression (verhindert doppelte Boxen um dieselbe Kugel)
        indices = cv2.dnn.NMSBoxes(boxes, confidences, score_threshold=0.40, nms_threshold=0.45)

        detected_balls = []
        if len(indices) > 0:
            for idx in indices.flatten():
                x, y, bw, bh = boxes[idx]
                cx = x + bw // 2
                cy = y + bh // 2
                r = max(int((bw + bh) / 4), 14)
                detected_balls.append((cx, cy, r))

        return detected_balls[:4]

    def run(self):
        if not self.load_model():
            self.status_changed.emit("FEHLER: Kein Modell gefunden", "#D32F2F")
            return

        # Automatische Suche: Versucht zuerst Index 1 (externe USB-Cam), dann Fallback auf 0
        cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        cap.set(cv2.CAP_PROP_FPS, 30)

        if not cap.isOpened():
            self.status_changed.emit("FEHLER: Kamera nicht erreichbar", "#D32F2F")
            return

        # 30 Frames verwerfen, damit Helligkeits- & Farbautomatik der Webcam einpendeln
        self.status_changed.emit("KAMERA STARTET...", "#1976D2")
        for _ in range(30):
            cap.read()

        state = 0
        prev_gray = None
        last_movement_time = 0

        while self.running:
            ret, frame = cap.read()
            if not ret:
                continue

            roi = frame[ROI_Y:ROI_Y + ROI_H, ROI_X:ROI_X + ROI_W].copy()
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            # Hintergrund neu anlernen
            if self.calibrate_bg_requested:
                self.bg_gray = gray.copy()
                self.calibrate_bg_requested = False
                state = 0
                self.status_changed.emit("BEREIT", "#1976D2")

            if self.bg_gray is None:
                continue

            if prev_gray is None:
                prev_gray = gray.copy()
                continue

            # Differenzberechnungen
            presence_diff = cv2.absdiff(self.bg_gray, gray)
            _, presence_thresh = cv2.threshold(presence_diff, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY)
            presence_pixels = cv2.countNonZero(presence_thresh)

            movement_diff = cv2.absdiff(prev_gray, gray)
            _, movement_thresh = cv2.threshold(movement_diff, THRESHOLD_VALUE, 255, cv2.THRESH_BINARY)
            movement_pixels = cv2.countNonZero(movement_thresh)

            # Zustandslogik
            if state == 0 and presence_pixels > PRESENCE_TOLERANCE:
                state = 1
                last_movement_time = time.time()
                self.status_changed.emit("ERFASSE...", "#F57C00")

            elif state == 1:
                if movement_pixels > MOVEMENT_TOLERANCE:
                    last_movement_time = time.time()
                else:
                    if (time.time() - last_movement_time) >= SETTLE_TIME:
                        # 1. KI-INFERENZ (Kratzer, Risse & Geometrie)
                        img_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
                        img_resized = cv2.resize(img_rgb, (256, 256))

                        input_tensor = img_resized.astype(np.float32) / 255.0
                        input_tensor = np.transpose(input_tensor, (2, 0, 1))
                        input_tensor = np.expand_dims(input_tensor, axis=0)

                        infer_result = self.compiled_model([input_tensor])
                        raw_score = infer_result[self.output_score_layer]
                        score = float(np.squeeze(raw_score))
                        # Bis einschließlich 0.70 in Ordnung, erst ab > 0.70 Ausschuss
                        is_defect = score > self.threshold

                        h_roi, w_roi = roi.shape[:2]
                        fehler_gruende = []

                        # 2. HARTE PRÜFUNGEN (OpenCV)
                        # A) Rahmen-Farbe (Soll = Kräftiges Blau auf dem Inlay)
                        # Suchbereich fest auf das zentrierte Inlay begrenzen (blendet Tisch & äußeres Alu aus)
                        inlay_half = 180
                        cx_mid, cy_mid = w_roi // 2, h_roi // 2
                        inlay_crop = roi[cy_mid - inlay_half : cy_mid + inlay_half, 
                                         cx_mid - inlay_half : cx_mid + inlay_half]

                        hsv_inlay = cv2.cvtColor(inlay_crop, cv2.COLOR_BGR2HSV)
                        
                        # Sättigung min. 75 filtert Alu/Grau sicher aus, hält aber auch bei Schatten/Glanz stand
                        lower_blue = np.array([96, 75, 40])
                        upper_blue = np.array([132, 255, 255])
                        
                        blue_inlay_mask = cv2.inRange(hsv_inlay, lower_blue, upper_blue)
                        blue_inlay_mask = cv2.morphologyEx(blue_inlay_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                        blue_pixels = cv2.countNonZero(blue_inlay_mask)

                        # Fremdfarben (Rot, Grün, Gelb etc.) liefern < 800 Pixel; Blau liegt bei > 12.000 Pixeln
                        if blue_pixels < 7000:
                            is_defect = True
                            fehler_gruende.append("Rahmen nicht blau")
                            score = max(score, 1)

                        # B) Pin-Check (Zentrum: Soll = Gelber Pin vorhanden)
                        pin_y1, pin_y2 = h_roi // 2 - 45, h_roi // 2 + 45
                        pin_x1, pin_x2 = w_roi // 2 - 45, w_roi // 2 + 45
                        pin_roi = roi[pin_y1:pin_y2, pin_x1:pin_x2]
                        hsv_pin = cv2.cvtColor(pin_roi, cv2.COLOR_BGR2HSV)

                        lower_yellow = np.array([18, 65, 70])
                        upper_yellow = np.array([34, 255, 255])
                        yellow_mask = cv2.inRange(hsv_pin, lower_yellow, upper_yellow)
                        yellow_mask = cv2.morphologyEx(yellow_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                        yellow_pixels = cv2.countNonZero(yellow_mask)

                        self.last_pin_ok = yellow_pixels > 350
                        self.last_pin_box = (pin_x1, pin_y1, pin_x2, pin_y2)
                        if not self.last_pin_ok:
                            is_defect = True
                            fehler_gruende.append("Pin fehlt/falsch")
                            score = max(score, 1)

                        # C) Kugel-Zähler via trainiertes YOLO-Modell
                        self.last_detected_balls = self.detect_balls(roi)

                        kugel_count = len(self.last_detected_balls)
                        if kugel_count < 4:
                            is_defect = True
                            fehler_gruende.append(f"Kugeln: {kugel_count}/4")
                            score = max(score, 1)

                        # 4. ERGEBNIS AN GUI SENDEN
                        self.result_ready.emit(is_defect, score)

                        if is_defect:
                            status_text = " | ".join(fehler_gruende) if fehler_gruende else "AUSSCHUSS"
                            self.status_changed.emit(status_text, "#D32F2F")
                        else:
                            self.status_changed.emit("GUT-TEIL (OK)", "#388E3C")

                        state = 2

            elif state == 2 and presence_pixels < RESET_TOLERANCE:
                state = 0
                self.last_detected_balls = []
                self.last_pin_box = None
                self.status_changed.emit("BEREIT", "#1976D2")

            prev_gray = gray.copy()

            # Persistentes Overlay im Video-Stream, solange das Ergebnis angezeigt wird (state 2)
            if state == 2:
                if is_defect:
                    # Dicker Alarm-Rahmen um den gesamten Kamera-Ausschnitt
                    cv2.rectangle(roi, (0, 0), (ROI_W - 1, ROI_H - 1), (0, 0, 255), 14)
                    # Auffälliges rotes Warn-Banner am oberen Rand
                    cv2.rectangle(roi, (0, 0), (ROI_W, 60), (0, 0, 210), -1)
                    cv2.putText(roi, "ACHTUNG: AUSSCHUSS", (25, 42), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.95, (255, 255, 255), 3)
                else:
                    # Grüner Bestätigungsrahmen für Gut-Teile
                    cv2.rectangle(roi, (0, 0), (ROI_W - 1, ROI_H - 1), (0, 255, 0), 8)
                    cv2.rectangle(roi, (0, 0), (ROI_W, 50), (0, 150, 0), -1)
                    cv2.putText(roi, "GUT-TEIL (OK)", (25, 36), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

                # Kugeln einzeichnen
                ball_color = (0, 255, 0) if len(self.last_detected_balls) == 4 else (0, 165, 255)
                for bx, by, br in self.last_detected_balls:
                    cv2.circle(roi, (bx, by), br + 3, ball_color, 2)
                    cv2.circle(roi, (bx, by), 2, (0, 0, 255), -1)

                # Pin-Region anzeigen
                if self.last_pin_box:
                    px1, py1, px2, py2 = self.last_pin_box
                    p_color = (0, 255, 0) if self.last_pin_ok else (0, 0, 255)
                    cv2.rectangle(roi, (px1, py1), (px2, py2), p_color, 2)
                    cv2.putText(roi, "PIN", (px1, py1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, p_color, 1)

            # Originales Fadenkreuz aus andon_live.py (türkise Linien + roter Punkt)
            center_x = ROI_W // 2
            center_y = ROI_H // 2
            cv2.line(roi, (center_x - 30, center_y), (center_x + 30, center_y), (255, 255, 0), 2)
            cv2.line(roi, (center_x, center_y - 30), (center_x, center_y + 30), (255, 255, 0), 2)
            cv2.circle(roi, (center_x, center_y), 3, (0, 0, 255), -1)

            # Nur senden, wenn die GUI den letzten Frame fertig gerendert hat
            if self.ui_ready:
                self.ui_ready = False
                rgb_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
                self.frame_ready.emit(rgb_roi)

        cap.release()

    def stop(self):
        self.running = False
        self.wait()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lernfabrik Andon Prüfsystem")
        icon_candidates = [
            os.path.join(PROJECT_ROOT, "App Icon.ico"),
            os.path.join(SRC_DIR, "App Icon.ico"),
            os.path.join(os.getcwd(), "App Icon.ico")
        ]
        for candidate in icon_candidates:
            if os.path.exists(candidate):
                self.setWindowIcon(QIcon(candidate))
                break
        self.resize(1280, 800)
        self.setStyleSheet("background-color: #1E1E1E; color: #FFFFFF;")

        self.total_count = 0
        self.good_count = 0
        self.defect_count = 0

        self.setup_ui()

        # Thread initialisieren & starten
        self.worker = CameraWorker()
        self.worker.frame_ready.connect(self.update_video)
        self.worker.status_changed.connect(self.update_status)
        self.worker.result_ready.connect(self.update_counters)
        self.worker.start()

    def setup_ui(self):
        # Basis-Styling für das gesamte Fenster
        self.setStyleSheet("""
            QMainWindow { background-color: #0F141C; }
            QLabel { color: #E2E8F0; font-family: 'Segoe UI', Arial, sans-serif; }
            QFrame#card {
                background-color: #1A222D;
                border: 1px solid #2A3644;
                border-radius: 12px;
            }
            QPushButton {
                background-color: #243040;
                color: #FFFFFF;
                border: 1px solid #3B4D63;
                border-radius: 8px;
                font-weight: bold;
                font-size: 13px;
                padding: 10px;
            }
            QPushButton:hover {
                background-color: #314257;
                border-color: #4A6382;
            }
            QPushButton:pressed {
                background-color: #1A2430;
            }
            QProgressBar {
                background-color: #121820;
                border: 1px solid #2A3644;
                border-radius: 6px;
                height: 18px;
                text-align: center;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 11px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00C853, stop:0.7 #FFD600, stop:1 #D50000);
                border-radius: 5px;
            }
        """)

        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        # 1. Header-Bar
        header = QHBoxLayout()
        title_label = QLabel("LERNFABRIK 4.0  │  KI-QUALITÄTSKONTROLLE")
        title_label.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title_label.setStyleSheet("color: #64B5F6; letter-spacing: 1px;")
        
        station_badge = QLabel("● STATION ONLINE")
        station_badge.setFont(QFont("Segoe UI", 10, QFont.Bold))
        station_badge.setStyleSheet("color: #00E676; background-color: #132E20; padding: 4px 10px; border-radius: 12px; border: 1px solid #1B5E20;")

        header.addWidget(title_label)
        header.addStretch()
        header.addWidget(station_badge)
        root_layout.addLayout(header)

        # Hauptbereich (Kamera links, Sidebar rechts)
        main_layout = QHBoxLayout()
        main_layout.setSpacing(16)

        # 2. Kamera-Stream
        self.video_label = QLabel("Kamera wird initialisiert...")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_label.setStyleSheet("background-color: #080B0F; border: 1px solid #2A3644; border-radius: 12px;")
        main_layout.addWidget(self.video_label, stretch=3)

        # 3. Rechte Sidebar
        sidebar = QVBoxLayout()
        sidebar.setSpacing(12)

        # Status-Box mit Glow-Effekt
        self.status_box = QLabel("BEREIT")
        self.status_box.setAlignment(Qt.AlignCenter)
        self.status_box.setFont(QFont("Segoe UI", 22, QFont.Bold))
        self.status_box.setFixedHeight(95)
        self.status_box.setStyleSheet("""
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1E88E5, stop:1 #1565C0);
            border: 2px solid #42A5F5;
            border-radius: 12px;
            color: #FFFFFF;
            letter-spacing: 2px;
        """)
        sidebar.addWidget(self.status_box)

        # Anomaly-Score Kachel mit Balken
        score_card = QFrame()
        score_card.setObjectName("card")
        score_layout = QVBoxLayout(score_card)
        score_layout.setContentsMargins(14, 12, 14, 12)
        
        self.score_title = QLabel("ANOMALY-SCORE: 0.000")
        self.score_title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        score_layout.addWidget(self.score_title)

        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, 1000)
        self.score_bar.setValue(0)
        self.score_bar.setFormat("Score: %v / 1000")
        score_layout.addWidget(self.score_bar)
        sidebar.addWidget(score_card)

        # KPI Statistik Kachel
        stats_card = QFrame()
        stats_card.setObjectName("card")
        stats_layout = QVBoxLayout(stats_card)
        stats_layout.setContentsMargins(14, 12, 14, 12)

        stats_head = QLabel("PRODUKTIONS-STATISTIK")
        stats_head.setFont(QFont("Segoe UI", 11, QFont.Bold))
        stats_head.setStyleSheet("color: #90CAF9;")
        stats_layout.addWidget(stats_head)

        kpi_grid = QHBoxLayout()
        
        # OK Kachel
        ok_box = QFrame()
        ok_box.setStyleSheet("background-color: #13241A; border: 1px solid #2E7D32; border-radius: 8px; padding: 6px;")
        ok_layout = QVBoxLayout(ok_box)
        ok_layout.setContentsMargins(4, 4, 4, 4)
        lbl_ok_title = QLabel("GUT-TEILE")
        lbl_ok_title.setFont(QFont("Segoe UI", 9, QFont.Bold))
        lbl_ok_title.setStyleSheet("color: #81C784;")
        self.lbl_good = QLabel("0")
        self.lbl_good.setFont(QFont("Segoe UI", 20, QFont.Bold))
        self.lbl_good.setStyleSheet("color: #E8F5E9;")
        ok_layout.addWidget(lbl_ok_title)
        ok_layout.addWidget(self.lbl_good)

        # NOK Kachel
        nok_box = QFrame()
        nok_box.setStyleSheet("background-color: #2D1418; border: 1px solid #C62828; border-radius: 8px; padding: 6px;")
        nok_layout = QVBoxLayout(nok_box)
        nok_layout.setContentsMargins(4, 4, 4, 4)
        lbl_nok_title = QLabel("AUSSCHUSS")
        lbl_nok_title.setFont(QFont("Segoe UI", 9, QFont.Bold))
        lbl_nok_title.setStyleSheet("color: #E57373;")
        self.lbl_defect = QLabel("0")
        self.lbl_defect.setFont(QFont("Segoe UI", 20, QFont.Bold))
        self.lbl_defect.setStyleSheet("color: #FFEBEE;")
        nok_layout.addWidget(lbl_nok_title)
        nok_layout.addWidget(self.lbl_defect)

        kpi_grid.addWidget(ok_box)
        kpi_grid.addWidget(nok_box)
        stats_layout.addLayout(kpi_grid)

        self.lbl_total = QLabel("Gesamt geprüft: 0 (Ausschussquote: 0.0 %)")
        self.lbl_total.setFont(QFont("Segoe UI", 10))
        self.lbl_total.setStyleSheet("color: #B0BEC5; margin-top: 4px;")
        stats_layout.addWidget(self.lbl_total)

        sidebar.addWidget(stats_card)
        sidebar.addStretch()

        # Steuerungs-Buttons
        btn_calibrate = QPushButton("⚡ Hintergrund kalibrieren")
        btn_calibrate.setFixedHeight(42)
        btn_calibrate.clicked.connect(self.on_calibrate_clicked)
        sidebar.addWidget(btn_calibrate)

        btn_reset_stats = QPushButton("↺ Zähler zurücksetzen")
        btn_reset_stats.setFixedHeight(36)
        btn_reset_stats.setStyleSheet("background-color: #1A222D; border-color: #2A3644; color: #90A4AE;")
        btn_reset_stats.clicked.connect(self.reset_stats)
        sidebar.addWidget(btn_reset_stats)

        main_layout.addLayout(sidebar, stretch=1)
        root_layout.addLayout(main_layout)

        central_widget = QWidget()
        central_widget.setLayout(root_layout)
        self.setCentralWidget(central_widget)

    @Slot(np.ndarray)
    def update_video(self, rgb_frame):
        target_size = self.video_label.size()
        if target_size.width() > 0 and target_size.height() > 0:
            h, w, ch = rgb_frame.shape
            q_img = QImage(rgb_frame.data, w, h, ch * w, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(q_img)

            scaled_pixmap = pixmap.scaled(
                target_size, 
                Qt.KeepAspectRatio, 
                Qt.FastTransformation
            )
            self.video_label.setPixmap(scaled_pixmap)

        # Signal an den Worker: GUI ist frei für den nächsten Frame
        self.worker.ui_ready = True
    @Slot(str, str)
    def update_status(self, text, color_hex):
        self.status_box.setText(text)

        if "AUSSCHUSS" in text:
            gradient = "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #E53935, stop:1 #B71C1C); border: 3px solid #FF5252;"
            # Fetter roter Leuchtrahmen um den gesamten Kamera-Stream & rötlicher Hintergrund
            self.video_label.setStyleSheet("background-color: #24080B; border: 5px solid #FF1744; border-radius: 12px;")

        elif "GUT" in text:
            gradient = "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #43A047, stop:1 #1B5E20); border: 3px solid #69F0AE;"
            # Grüner Rahmen um das Video
            self.video_label.setStyleSheet("background-color: #08140C; border: 3px solid #00E676; border-radius: 12px;")

        elif "ERFASSE" in text:
            gradient = "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FB8C00, stop:1 #E65100); border: 2px solid #FFA726;"
            self.video_label.setStyleSheet("background-color: #080B0F; border: 2px solid #FF9800; border-radius: 12px;")

        else: # BEREIT
            gradient = "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1E88E5, stop:1 #1565C0); border: 2px solid #42A5F5;"
            # Neutraler Rahmen
            self.video_label.setStyleSheet("background-color: #080B0F; border: 1px solid #2A3644; border-radius: 12px;")

        self.status_box.setStyleSheet(f"{gradient} border-radius: 12px; color: #FFFFFF; letter-spacing: 2px;")

    @Slot(bool, float)
    def update_counters(self, is_defect, score):
        self.total_count += 1
        if is_defect:
            self.defect_count += 1
        else:
            self.good_count += 1

        quote = (self.defect_count / self.total_count) * 100 if self.total_count > 0 else 0.0

        # Score & Progress-Bar aktualisieren (Wert von 0.0 - 1.0 auf 0 - 1000 normieren)
        self.score_title.setText(f"ANOMALY-SCORE: {score:.3f}")
        self.score_bar.setValue(int(min(max(score, 0.0), 1.0) * 1000))

        self.lbl_good.setText(str(self.good_count))
        self.lbl_defect.setText(str(self.defect_count))
        self.lbl_total.setText(f"Gesamt geprüft: {self.total_count} (Ausschussquote: {quote:.1f} %)")

    def on_calibrate_clicked(self):
        self.worker.request_bg_calibration()

    def reset_stats(self):
        self.total_count = 0
        self.good_count = 0
        self.defect_count = 0
        self.score_title.setText("ANOMALY-SCORE: 0.000")
        self.score_bar.setValue(0)
        self.lbl_good.setText("0")
        self.lbl_defect.setText("0")
        self.lbl_total.setText("Gesamt geprüft: 0 (Ausschussquote: 0.0 %)")

    def closeEvent(self, event):
        self.worker.stop()
        event.accept()


if __name__ == "__main__":
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.critical("Uncaught exception:", exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = handle_exception

    # AppUserModelID für Windows-Taskleiste vor der Initialisierung der QApplication via ctypes setzen
    if sys.platform == "win32":
        try:
            import ctypes
            myappid = "Lernfabrik.AndonPruefstation.Camera.1.0"
            res = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            logging.info(f"AppUserModelID '{myappid}' gesetzt (Ergebnis: {res})")
        except Exception as e:
            logging.warning(f"Konnte AppUserModelID via ctypes nicht setzen: {e}")

    app = QApplication(sys.argv)

    # App-Icon laden und der QApplication sowie dem Fenster zuweisen
    icon_candidates = [
        os.path.join(PROJECT_ROOT, "App Icon.ico"),
        os.path.join(SRC_DIR, "App Icon.ico"),
        os.path.join(os.getcwd(), "App Icon.ico")
    ]
    app_icon = None
    for candidate in icon_candidates:
        if os.path.exists(candidate):
            app_icon = QIcon(candidate)
            logging.info(f"Kamera-Icon gefunden und geladen: {candidate}")
            break

    if app_icon and not app_icon.isNull():
        app.setWindowIcon(app_icon)

    window = MainWindow()
    if app_icon and not app_icon.isNull():
        window.setWindowIcon(app_icon)

    window.show()
    sys.exit(app.exec())