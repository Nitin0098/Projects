"""
Subsidence Monitoring - Node Dashboard
======================================

LEFT  : node list, serial controls, alert banner
RIGHT : site map with node markers

Serial protocol (one byte per message):
    bits 7-4 = node id      (1 = the demo node)
    bit 0    = alert flag   (1 = alert, 0 = normal)
    0x10 -> node 1, normal
    0x11 -> node 1, alert

Node 1 shows OFFLINE until packets arrive, and returns to OFFLINE if
nothing is received for NODE_TIMEOUT_S seconds.

The serial link reconnects automatically if the cable is unplugged or the
board resets. Click Disconnect to stop retrying.

Requirements:
    pip install pyserial pillow

Usage:
    1. Put your map image next to this script as "site_map.jpg"
    2. python subsidence_dashboard.py
    3. Pick the port, click Connect (baud defaults to 115200)
"""

import os
import sys
import time
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import serial
    import serial.tools.list_ports
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
MAP_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site_map.jpg")
HARDCODED_ADDRESS = "Munshi Nagar, Andheri West, Mumbai"
DEFAULT_BAUD = 115200          # must match the Arduino sketch
DEMO_NODE_ID = 1               # must match NODE_ID in the Arduino sketch
NODE_TIMEOUT_S = 5.0           # no packet for this long -> node offline
RECONNECT_DELAY_S = 2.0        # wait between reconnect attempts
DEBUG_SERIAL = True            # print every decoded byte to the terminal

# All nodes start offline; node 1 goes online when packets arrive.
NODES = {
    1: {"name": "Node 1", "status": "offline", "pos": (0.589, 0.458)},
    2: {"name": "Node 2", "status": "offline", "pos": (0.14, 0.66)},
    3: {"name": "Node 3", "status": "offline", "pos": (0.78, 0.58)},
    4: {"name": "Node 4", "status": "offline", "pos": (0.32, 0.12)},
    5: {"name": "Node 5", "status": "offline", "pos": (0.88, 0.18)},
}

COLOR_ONLINE = "#22c55e"
COLOR_OFFLINE = "#9ca3af"
COLOR_ALERT = "#ef4444"
COLOR_WARN = "#f59e0b"
COLOR_BG = "#0f172a"
COLOR_PANEL = "#111827"
COLOR_TEXT = "#e5e7eb"
COLOR_SUBTEXT = "#9ca3af"


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Subsidence Monitoring Dashboard")
        self.geometry("1280x760")
        self.configure(bg=COLOR_BG)
        self.minsize(1000, 620)

        self.serial_conn = None
        self.serial_thread = None
        self.serial_stop_event = threading.Event()
        self.event_queue = queue.Queue()

        self.alert_active = False
        self.blink_state = False
        self.node_online = False
        self.last_rx_time = None

        self.want_connection = False   # user intent: keep trying to connect
        self.link_up = False           # is the port currently open?
        self.port_name = None
        self.baud_rate = DEFAULT_BAUD

        self.node_markers = {}
        self.node_dots = {}

        self._build_layout()
        self._load_map_image()
        self.after(200, self._poll_queue)
        self.after(500, self._blink_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._refresh_node_status()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        container = tk.Frame(self, bg=COLOR_BG)
        container.pack(fill="both", expand=True)

        self.left = tk.Frame(container, bg=COLOR_PANEL, width=380)
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)

        self.right = tk.Frame(container, bg=COLOR_BG)
        self.right.pack(side="right", fill="both", expand=True)

        self._build_left_panel()
        self._build_right_panel()

    def _build_left_panel(self):
        pad = 16

        tk.Label(self.left, text="Subsidence Monitoring", font=("Helvetica", 16, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_TEXT, anchor="w").pack(fill="x", padx=pad, pady=(pad, 0))
        tk.Label(self.left, text="Node Dashboard", font=("Helvetica", 10),
                 bg=COLOR_PANEL, fg=COLOR_SUBTEXT, anchor="w").pack(fill="x", padx=pad, pady=(0, 14))

        # --- Alert banner ---
        self.alert_frame = tk.Frame(self.left, bg="#1f2937", height=64)
        self.alert_frame.pack(fill="x", padx=pad, pady=(0, 14))
        self.alert_label = tk.Label(self.alert_frame, text="○  Node 1 offline — no data received",
                                    font=("Helvetica", 11, "bold"), bg="#1f2937", fg=COLOR_SUBTEXT,
                                    anchor="w", justify="left", wraplength=330)
        self.alert_label.pack(fill="both", expand=True, padx=12, pady=10)

        _divider(self.left, pad)

        # --- Serial connection ---
        tk.Label(self.left, text="SERIAL CONNECTION", font=("Helvetica", 9, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_SUBTEXT, anchor="w").pack(fill="x", padx=pad, pady=(10, 6))

        conn_frame = tk.Frame(self.left, bg=COLOR_PANEL)
        conn_frame.pack(fill="x", padx=pad)

        tk.Label(conn_frame, text="Port:", bg=COLOR_PANEL, fg=COLOR_TEXT,
                 font=("Helvetica", 9)).grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value=self._guess_default_port())
        self.port_entry = ttk.Combobox(conn_frame, textvariable=self.port_var, width=14,
                                       values=self._available_ports())
        self.port_entry.grid(row=0, column=1, padx=(6, 0), pady=3, sticky="w")

        tk.Label(conn_frame, text="Baud:", bg=COLOR_PANEL, fg=COLOR_TEXT,
                 font=("Helvetica", 9)).grid(row=1, column=0, sticky="w")
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        tk.Entry(conn_frame, textvariable=self.baud_var, width=10).grid(row=1, column=1, padx=(6, 0), pady=3, sticky="w")

        btn_frame = tk.Frame(self.left, bg=COLOR_PANEL)
        btn_frame.pack(fill="x", padx=pad, pady=(8, 4))
        self.connect_btn = tk.Button(btn_frame, text="Connect", command=self._toggle_connection,
                                     bg="#2563eb", fg="white", relief="flat", padx=10, pady=4)
        self.connect_btn.pack(side="left")
        tk.Button(btn_frame, text="Refresh ports", command=self._refresh_ports,
                  bg="#374151", fg="white", relief="flat", padx=8, pady=4).pack(side="left", padx=(6, 0))

        self.conn_status = tk.Label(self.left, text="● Not connected", font=("Helvetica", 9),
                                    bg=COLOR_PANEL, fg=COLOR_SUBTEXT, anchor="w")
        self.conn_status.pack(fill="x", padx=pad, pady=(4, 0))

        if not PYSERIAL_AVAILABLE:
            tk.Label(self.left, text="pyserial not installed — run:\npip install pyserial",
                     font=("Helvetica", 8), bg=COLOR_PANEL, fg=COLOR_WARN,
                     justify="left", anchor="w").pack(fill="x", padx=pad, pady=(4, 0))
            self.connect_btn.config(state="disabled")

        _divider(self.left, pad)

        # --- Node list ---
        tk.Label(self.left, text=f"NODES — {HARDCODED_ADDRESS}", font=("Helvetica", 9, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_SUBTEXT, anchor="w",
                 wraplength=340).pack(fill="x", padx=pad, pady=(10, 6))

        list_frame = tk.Frame(self.left, bg=COLOR_PANEL)
        list_frame.pack(fill="x", padx=pad)

        for node_id, node in NODES.items():
            row = tk.Frame(list_frame, bg="#1a2332", pady=8, padx=10)
            row.pack(fill="x", pady=3)

            dot_canvas = tk.Canvas(row, width=14, height=14, bg="#1a2332", highlightthickness=0)
            dot_canvas.pack(side="left", padx=(0, 10))
            color = COLOR_ONLINE if node["status"] == "online" else COLOR_OFFLINE
            dot_id = dot_canvas.create_oval(2, 2, 12, 12, fill=color, outline="")
            self.node_dots[node_id] = (dot_canvas, dot_id)

            text_frame = tk.Frame(row, bg="#1a2332")
            text_frame.pack(side="left", fill="x", expand=True)
            tk.Label(text_frame, text=node["name"], font=("Helvetica", 10, "bold"),
                     bg="#1a2332", fg=COLOR_TEXT, anchor="w").pack(fill="x")
            tk.Label(text_frame, text=HARDCODED_ADDRESS, font=("Helvetica", 8),
                     bg="#1a2332", fg=COLOR_SUBTEXT, anchor="w").pack(fill="x")

            status_text = "ONLINE" if node["status"] == "online" else "OFFLINE"
            status_color = COLOR_ONLINE if node["status"] == "online" else COLOR_SUBTEXT
            status_label = tk.Label(row, text=status_text, font=("Helvetica", 8, "bold"),
                                    bg="#1a2332", fg=status_color)
            status_label.pack(side="right")
            if node_id == DEMO_NODE_ID:
                self.demo_status_label = status_label
                self.demo_dot = (dot_canvas, dot_id)

        _divider(self.left, pad)

        # --- Demo controls ---
        tk.Label(self.left, text="DEMO CONTROLS", font=("Helvetica", 9, "bold"),
                 bg=COLOR_PANEL, fg=COLOR_SUBTEXT, anchor="w").pack(fill="x", padx=pad, pady=(10, 6))
        demo_frame = tk.Frame(self.left, bg=COLOR_PANEL)
        demo_frame.pack(fill="x", padx=pad, pady=(0, 16))
        tk.Button(demo_frame, text="Simulate Alert (Node 1)", command=self._simulate_alert,
                  bg=COLOR_ALERT, fg="white", relief="flat", padx=8, pady=6).pack(fill="x", pady=(0, 6))
        tk.Button(demo_frame, text="Clear Alert", command=self._clear_alert,
                  bg="#374151", fg="white", relief="flat", padx=8, pady=6).pack(fill="x")

    def _build_right_panel(self):
        header = tk.Frame(self.right, bg=COLOR_BG)
        header.pack(fill="x", padx=18, pady=(16, 6))
        tk.Label(header, text=f"Site Map — {HARDCODED_ADDRESS}", font=("Helvetica", 13, "bold"),
                 bg=COLOR_BG, fg=COLOR_TEXT, anchor="w").pack(side="left")

        legend = tk.Frame(header, bg=COLOR_BG)
        legend.pack(side="right")
        _legend_item(legend, COLOR_ONLINE, "Online")
        _legend_item(legend, COLOR_OFFLINE, "Offline")
        _legend_item(legend, COLOR_ALERT, "Alert")

        self.map_canvas = tk.Canvas(self.right, bg="#1e293b", highlightthickness=0)
        self.map_canvas.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.map_canvas.bind("<Configure>", lambda e: self._redraw_map())

    # ------------------------------------------------------------------
    # Map
    # ------------------------------------------------------------------
    def _load_map_image(self):
        self._pil_image = None
        if PIL_AVAILABLE and os.path.exists(MAP_IMAGE_PATH):
            try:
                self._pil_image = Image.open(MAP_IMAGE_PATH)
            except Exception:
                self._pil_image = None
        self._tk_image = None
        self.after(100, self._redraw_map)

    def _redraw_map(self):
        canvas = self.map_canvas
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10 or h < 10:
            return

        img_x0, img_y0, img_w, img_h = 0, 0, w, h

        if self._pil_image is not None:
            img_ratio = self._pil_image.width / self._pil_image.height
            box_ratio = w / h
            if img_ratio > box_ratio:
                img_w = w
                img_h = int(w / img_ratio)
            else:
                img_h = h
                img_w = int(h * img_ratio)
            img_x0 = (w - img_w) // 2
            img_y0 = (h - img_h) // 2
            resized = self._pil_image.resize((max(img_w, 1), max(img_h, 1)))
            self._tk_image = ImageTk.PhotoImage(resized)
            canvas.create_image(img_x0, img_y0, anchor="nw", image=self._tk_image)
        else:
            canvas.create_rectangle(0, 0, w, h, fill="#1e293b", outline="")
            canvas.create_text(w / 2, h / 2 - 10, text="Map image not found",
                               fill="#94a3b8", font=("Helvetica", 13, "bold"))
            canvas.create_text(w / 2, h / 2 + 14,
                               text=f'Place your map image at:\n"{os.path.basename(MAP_IMAGE_PATH)}" next to this script',
                               fill="#64748b", font=("Helvetica", 9), justify="center")

        self.node_markers = {}
        for node_id, node in NODES.items():
            fx, fy = node["pos"]
            x = img_x0 + fx * img_w
            y = img_y0 + fy * img_h
            color = COLOR_ONLINE if node["status"] == "online" else COLOR_OFFLINE
            if node_id == DEMO_NODE_ID and self.alert_active and self.node_online:
                color = COLOR_ALERT if self.blink_state else "#7f1d1d"
            r = 9 if node_id == DEMO_NODE_ID else 7
            oval = canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="white", width=1.5)
            canvas.create_text(x, y - r - 10, text=node["name"], fill="white", font=("Helvetica", 8, "bold"))
            self.node_markers[node_id] = oval

            if node_id == DEMO_NODE_ID and self.alert_active and self.node_online:
                ring_r = r + (10 if self.blink_state else 4)
                canvas.create_oval(x - ring_r, y - ring_r, x + ring_r, y + ring_r,
                                   outline=COLOR_ALERT, width=2)
                canvas.create_text(x, y + r + 16, text="⚠ ALERT", fill=COLOR_ALERT,
                                   font=("Helvetica", 10, "bold"))

    def _blink_tick(self):
        self.blink_state = not self.blink_state
        if self.alert_active and self.node_online:
            self._redraw_map()
        self.after(500, self._blink_tick)

    # ------------------------------------------------------------------
    # Serial
    # ------------------------------------------------------------------
    def _available_ports(self):
        if not PYSERIAL_AVAILABLE:
            return []
        try:
            return [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            return []

    def _refresh_ports(self):
        self.port_entry["values"] = self._available_ports()

    def _guess_default_port(self):
        ports = self._available_ports()
        return ports[0] if ports else ("COM3" if sys.platform.startswith("win") else "/dev/ttyUSB0")

    def _toggle_connection(self):
        if self.want_connection:
            self._disconnect_serial()
        else:
            self._connect_serial()

    def _connect_serial(self):
        if not PYSERIAL_AVAILABLE:
            messagebox.showerror("pyserial missing", "Install it with: pip install pyserial")
            return

        port = self.port_var.get().strip()
        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid baud rate", "Baud rate must be a number.")
            return

        # Try once up front so a bad port name gives immediate feedback
        try:
            test = serial.Serial(port, baud, timeout=0.5)
            test.close()
        except Exception as e:
            messagebox.showerror("Connection failed", str(e))
            return

        self.port_name = port
        self.baud_rate = baud
        self.want_connection = True

        self.serial_stop_event.clear()
        self.serial_thread = threading.Thread(target=self._serial_read_loop, daemon=True)
        self.serial_thread.start()

        self.connect_btn.config(text="Disconnect", bg="#374151")
        self.conn_status.config(text=f"● Connecting — {port} @ {baud} baud", fg=COLOR_WARN)

    def _disconnect_serial(self):
        self.want_connection = False       # stop reconnect attempts
        self.serial_stop_event.set()
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_conn = None
        self.link_up = False

        self.node_online = False
        self.last_rx_time = None
        self.alert_active = False
        NODES[DEMO_NODE_ID]["status"] = "offline"
        self._refresh_node_status()

        self.connect_btn.config(text="Connect", bg="#2563eb")
        self.conn_status.config(text="● Not connected", fg=COLOR_SUBTEXT)

    def _serial_read_loop(self):
        """Background thread. Opens the port, reads bytes, and reopens
        automatically if the link drops (cable unplugged, board reset).
        Runs until the user clicks Disconnect."""
        while not self.serial_stop_event.is_set() and self.want_connection:

            # ---- open (or reopen) the port ----
            if self.serial_conn is None:
                try:
                    self.serial_conn = serial.Serial(self.port_name, self.baud_rate, timeout=0.5)
                    if DEBUG_SERIAL:
                        print(f"[serial] opened {self.port_name}")
                    self.event_queue.put(("link", True))
                except Exception as e:
                    if DEBUG_SERIAL:
                        print(f"[serial] open failed: {e} — retrying in {RECONNECT_DELAY_S}s")
                    self.serial_conn = None
                    self.event_queue.put(("link", False))
                    self._interruptible_sleep(RECONNECT_DELAY_S)
                    continue

            # ---- read one byte ----
            try:
                data = self.serial_conn.read(1)
            except Exception as e:
                if DEBUG_SERIAL:
                    print(f"[serial] read failed: {e} — reconnecting")
                try:
                    self.serial_conn.close()
                except Exception:
                    pass
                self.serial_conn = None
                self.event_queue.put(("link", False))
                self._interruptible_sleep(RECONNECT_DELAY_S)
                continue

            if not data:
                continue                    # timeout, no data — normal

            byte_val = data[0]
            node_id = (byte_val >> 4) & 0x0F
            alert_bit = byte_val & 0x01
            if DEBUG_SERIAL:
                print(f"RX 0x{byte_val:02X}  node={node_id}  alert={alert_bit}")
            self.event_queue.put(("serial_byte", node_id, alert_bit))

        # thread exiting
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None

    def _interruptible_sleep(self, seconds):
        """Sleep in small slices so Disconnect stays responsive."""
        waited = 0.0
        while waited < seconds and not self.serial_stop_event.is_set():
            time.sleep(0.1)
            waited += 0.1

    def _poll_queue(self):
        try:
            while True:
                item = self.event_queue.get_nowait()

                if item[0] == "serial_byte":
                    _, node_id, alert_bit = item
                    if node_id == DEMO_NODE_ID:
                        self.last_rx_time = time.time()
                        if not self.node_online:
                            self.node_online = True
                            NODES[DEMO_NODE_ID]["status"] = "online"
                        self._set_alert(alert_bit == 1)

                elif item[0] == "link":
                    _, up = item
                    self.link_up = up
                    if up:
                        self.conn_status.config(
                            text=f"● Connected — {self.port_name} @ {self.baud_rate} baud",
                            fg=COLOR_ONLINE)
                    else:
                        self.conn_status.config(
                            text=f"● Reconnecting — {self.port_name}...", fg=COLOR_WARN)
                        # link down means the node is unreachable
                        self.node_online = False
                        self.alert_active = False
                        NODES[DEMO_NODE_ID]["status"] = "offline"
                        self._refresh_node_status()

        except queue.Empty:
            pass

        # staleness check -> node offline if the byte stream stops
        if self.node_online and self.last_rx_time is not None:
            if (time.time() - self.last_rx_time) > NODE_TIMEOUT_S:
                self.node_online = False
                self.alert_active = False
                NODES[DEMO_NODE_ID]["status"] = "offline"
                self._refresh_node_status()

        self.after(150, self._poll_queue)

    # ------------------------------------------------------------------
    # State display
    # ------------------------------------------------------------------
    def _refresh_node_status(self):
        if not self.node_online:
            if hasattr(self, "demo_status_label"):
                self.demo_status_label.config(text="OFFLINE", fg=COLOR_SUBTEXT)
            if hasattr(self, "demo_dot"):
                c, oid = self.demo_dot
                c.itemconfig(oid, fill=COLOR_OFFLINE)
            self.alert_frame.config(bg="#1f2937")
            self.alert_label.config(bg="#1f2937", fg=COLOR_SUBTEXT,
                                    text="○  Node 1 offline — no data received")
            self._redraw_map()
        else:
            self._set_alert(self.alert_active)

    def _simulate_alert(self):
        # force online so the demo works without hardware attached
        self.node_online = True
        self.last_rx_time = time.time()
        NODES[DEMO_NODE_ID]["status"] = "online"
        self._set_alert(True)

    def _clear_alert(self):
        self.node_online = True
        self.last_rx_time = time.time()
        NODES[DEMO_NODE_ID]["status"] = "online"
        self._set_alert(False)

    def _set_alert(self, active: bool):
        self.alert_active = active

        if not self.node_online:
            self._refresh_node_status()
            return

        if active:
            self.alert_frame.config(bg="#3b0d0d")
            self.alert_label.config(
                bg="#3b0d0d", fg=COLOR_ALERT,
                text=f"⚠  ALERT — possible subsidence activity\nNode 1 · {HARDCODED_ADDRESS}"
            )
            if hasattr(self, "demo_status_label"):
                self.demo_status_label.config(text="ALERT", fg=COLOR_ALERT)
            if hasattr(self, "demo_dot"):
                c, oid = self.demo_dot
                c.itemconfig(oid, fill=COLOR_ALERT)
        else:
            self.alert_frame.config(bg="#1f2937")
            self.alert_label.config(bg="#1f2937", fg=COLOR_ONLINE, text="✓  No active alerts")
            if hasattr(self, "demo_status_label"):
                self.demo_status_label.config(text="ONLINE", fg=COLOR_ONLINE)
            if hasattr(self, "demo_dot"):
                c, oid = self.demo_dot
                c.itemconfig(oid, fill=COLOR_ONLINE)
        self._redraw_map()

    def _on_close(self):
        self._disconnect_serial()
        self.destroy()


def _divider(parent, pad):
    tk.Frame(parent, bg="#243044", height=1).pack(fill="x", padx=pad, pady=(6, 0))


def _legend_item(parent, color, label):
    frame = tk.Frame(parent, bg=COLOR_BG)
    frame.pack(side="left", padx=(14, 0))
    c = tk.Canvas(frame, width=12, height=12, bg=COLOR_BG, highlightthickness=0)
    c.pack(side="left", padx=(0, 4))
    c.create_oval(1, 1, 11, 11, fill=color, outline="")
    tk.Label(frame, text=label, bg=COLOR_BG, fg=COLOR_SUBTEXT, font=("Helvetica", 9)).pack(side="left")


if __name__ == "__main__":
    if not PIL_AVAILABLE:
        print("Note: Pillow is not installed — the map image will not display.")
        print("Install with: pip install pillow")
    app = Dashboard()
    app.mainloop()