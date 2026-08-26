from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from pathlib import Path
from collections import Counter
from datetime import datetime
import re
import threading
import urllib.request
import base64
import json
import shutil
import webbrowser
import io

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

try:
    import resvg_py
except ImportError:
    resvg_py = None

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

_BaseApp = ctk.CTk if ctk is not None else tk.Tk

from db import Database
from scryfall_client import ScryfallClient, ScryfallError

APP_TITLE = "MTG Organizer V3.2d — Colección & Commander"
# MTG_ORGANIZER_ANALYTICS_REFACTOR_2_2
# MTG_ORGANIZER_BUILDER_SUITE_2_0
# MTG_ORGANIZER_DECK_MANA_SCROLL_FIX_1_14A
# MTG_ORGANIZER_MOXFIELD_STYLE_ORGANIZATION_1_14
# MTG_ORGANIZER_UNDO_COLLECTION_BUILD_1_13
# MTG_ORGANIZER_PHYSICAL_MANAGEMENT_1_12
# MTG_ORGANIZER_COMMANDER_REDESIGN_BASIC_LANDS_1_11
# MTG_ORGANIZER_CONTEXT_MENU_EXPORT_LIST_1_10C
# MTG_ORGANIZER_UI_CORRECTIVE_1_10B
# MTG_ORGANIZER_UI_RESPONSIVE_1_10A
# MTG_ORGANIZER_UI_OPTIMIZATION_1_10
# MTG_ORGANIZER_LISTA_GENERIC_REVERT_MANA_1_9_8
# MTG_ORGANIZER_COMMANDER_FILTERS_1_9_7
# MTG_ORGANIZER_MANA_ICONS_1_9_6
# MTG_ORGANIZER_COMMANDER_QOL_1_9_5B

COLORS = {
    "W": "#f2ead3",
    "U": "#dbeafe",
    "B": "#e5e7eb",
    "R": "#fee2e2",
    "G": "#dcfce7",
    "M": "#fef3c7",
    "C": "#f3f4f6",
}

UI = {
    "bg": "#0D1015",
    "sidebar": "#12161D",
    "surface": "#181D25",
    "surface2": "#202630",
    "surface3": "#2A313D",
    "hover": "#323A48",
    "line": "#394352",
    "text": "#F3F5F8",
    "muted": "#A8B0BD",
    "purple": "#7668E8",
    "purple_hover": "#6759D5",
    "green": "#43B88E",
    "green_hover": "#389B79",
    "danger": "#CC6670",
    "warning": "#D6A85E",
}

class App(_BaseApp):
    def __init__(self):
        super().__init__()
        if ctk is None:
            self.withdraw()
            messagebox.showerror(
                "Falta CustomTkinter",
                "MTG Organizer V3 necesita CustomTkinter.\n\n"
                "Instálalo una sola vez con:\n\n    py -m pip install customtkinter"
            )
            self.destroy()
            return

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(APP_TITLE)
        self.geometry("1440x860")
        self.minsize(980, 650)
        self.configure(fg_color=UI["bg"])

        self._configure_style()

        data_dir = Path(__file__).resolve().parent / "data"
        data_dir.mkdir(exist_ok=True)
        self._mana_symbol_dir = data_dir / "mana_symbols"
        self._mana_symbol_dir.mkdir(exist_ok=True)
        self._mana_photo_cache = {}
        self._mana_symbol_pending = set()
        self._mana_redraw_scheduled = False
        self.db = Database(data_dir / "mtg_organizer.db")
        self._init_extended_schema()
        self.scry = ScryfallClient()
        self._scry_detail_cache = {}

        self.status_var = tk.StringVar(value="Listo")
        self._undo_stack = []
        self._build()
        self.bind_all("<Control-z>", lambda e: self.undo_last_action())
        self.refresh_collection()
        self.refresh_decks()


    def _init_extended_schema(self):
        """Extensiones locales V2: metadatos, plan, considering y combos."""
        with self.db.con() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS deck_meta (
                deck_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'Montado',
                notes TEXT NOT NULL DEFAULT '',
                declared_bracket INTEGER NOT NULL DEFAULT 0,
                cedh_intent INTEGER NOT NULL DEFAULT 0,
                chain_extra_turns INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS deck_plan (
                deck_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                is_commander INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(deck_id, collection_id, is_commander),
                FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS considering (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT '',
                UNIQUE(deck_id, collection_id),
                FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS deck_combos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id INTEGER NOT NULL,
                card1 TEXT NOT NULL,
                card2 TEXT NOT NULL,
                early INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
            );
            """)
            decks=c.execute("SELECT id FROM decks").fetchall()
            for d in decks:
                did=int(d["id"])
                c.execute("INSERT OR IGNORE INTO deck_meta(deck_id) VALUES (?)",(did,))
                exists=c.execute("SELECT 1 FROM deck_plan WHERE deck_id=? LIMIT 1",(did,)).fetchone()
                if not exists:
                    c.execute("""
                        INSERT OR REPLACE INTO deck_plan(deck_id,collection_id,quantity,is_commander)
                        SELECT deck_id,collection_id,quantity,is_commander FROM deck_cards WHERE deck_id=?
                    """,(did,))

    def _deck_meta(self,did):
        with self.db.con() as c:
            c.execute("INSERT OR IGNORE INTO deck_meta(deck_id) VALUES (?)",(did,))
            return c.execute("SELECT * FROM deck_meta WHERE deck_id=?",(did,)).fetchone()

    def _deck_status(self,did):
        m=self._deck_meta(did)
        return (m["status"] if m else "Montado") or "Montado"

    def _sync_plan_from_assignments(self,did):
        # El plan puede contener cartas todavía no compradas; no se destruye al refrescar.
        return

    def _deck_rows(self,did):
        with self.db.con() as c:
            return c.execute("""
                SELECT (-dp.rowid) AS deck_card_id, dp.quantity, dp.is_commander,
                       col.*, col.quantity AS owned,
                       COALESCE((SELECT SUM(dc2.quantity) FROM deck_cards dc2
                                 WHERE dc2.collection_id=col.id AND dc2.deck_id<>?),0) AS used_elsewhere
                FROM deck_plan dp JOIN collection col ON col.id=dp.collection_id
                WHERE dp.deck_id=?
                ORDER BY dp.is_commander DESC,col.name COLLATE NOCASE
            """,(did,did)).fetchall()

    def _plan_add(self,did,cid,qty=1,is_commander=False):
        with self.db.con() as c:
            row=c.execute("""SELECT quantity FROM deck_plan
                             WHERE deck_id=? AND collection_id=? AND is_commander=?""",
                          (did,cid,1 if is_commander else 0)).fetchone()
            if row:
                c.execute("""UPDATE deck_plan SET quantity=quantity+?
                             WHERE deck_id=? AND collection_id=? AND is_commander=?""",
                          (qty,did,cid,1 if is_commander else 0))
            else:
                c.execute("""INSERT INTO deck_plan(deck_id,collection_id,quantity,is_commander)
                             VALUES (?,?,?,?)""",(did,cid,qty,1 if is_commander else 0))

    def _set_deck_status(self,*_):
        did=self.current_deck_id()
        if did is None or not hasattr(self,"deck_status_var"): return
        new=self.deck_status_var.get()
        old=self._deck_status(did)
        if new==old:return
        deck=self.db.get_deck(did)
        try:
            if new=="Desarmado":
                self._sync_plan_from_assignments(did)
                with self.db.con() as c:
                    c.execute("DELETE FROM deck_cards WHERE deck_id=?",(did,))
                    c.execute("UPDATE deck_meta SET status=? WHERE deck_id=?",(new,did))
                self.status_var.set(f"{deck['name']} desarmado: sus cartas quedaron disponibles.")
            else:
                # Antes de montar, comprueba que TODO el plan pueda reservarse.
                # Si falta algo, no modifica ninguna asignación ni destruye el plan.
                with self.db.con() as c:
                    plan=list(c.execute("SELECT * FROM deck_plan WHERE deck_id=?",(did,)).fetchall())
                shortages=[]
                for p in plan:
                    col=self.db.get_collection_card(p["collection_id"])
                    if not col:
                        shortages.append(f"Carta eliminada de colección · ×{int(p['quantity'])}")
                        continue
                    free=self._free_qty_for_collection_row(col)
                    if free<int(p["quantity"]):
                        shortages.append(f"{col['name']} · necesita {int(p['quantity'])}, libres {free}")
                if shortages:
                    self.deck_status_var.set(old)
                    messagebox.showwarning("No se puede montar completo",
                        "Faltan copias físicas libres:\n\n"+"\n".join(shortages[:25])+
                        "\n\nEl mazo sigue Desarmado y su lista no se modificó.")
                    return
                with self.db.con() as c:
                    c.execute("DELETE FROM deck_cards WHERE deck_id=?",(did,))
                for p in plan:
                    self.db.add_to_deck(did,p["collection_id"],int(p["quantity"]),bool(p["is_commander"]))
                with self.db.con() as c:c.execute("UPDATE deck_meta SET status=? WHERE deck_id=?",(new,did))
                self.status_var.set(f"Estado de {deck['name']}: {new}")
            self.refresh_decks()
            for i,d in enumerate(self.deck_rows):
                if d["id"]==did:
                    self.deck_list.selection_set(i); self.deck_list.activate(i); break
            self.on_deck_select(); self.refresh_collection()
        except Exception as e:
            self.deck_status_var.set(old)
            messagebox.showerror("Estado del mazo",str(e))


    def _configure_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.option_add("*Font", "Arial 10")
        self.option_add("*TCombobox*Listbox.background", UI["surface2"])
        self.option_add("*TCombobox*Listbox.foreground", UI["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", UI["purple"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

        style.configure("TFrame", background=UI["surface"])
        style.configure("TLabelframe", background=UI["surface"], borderwidth=0, relief="flat")
        style.configure("TLabelframe.Label", background=UI["surface"], foreground=UI["text"], font=("Arial", 10, "bold"))
        style.configure("TLabel", background=UI["surface"], foreground=UI["text"], font=("Arial", 10))
        style.configure("Header.TLabel", background=UI["surface"], foreground=UI["text"], font=("Arial", 18, "bold"))
        style.configure("Sub.TLabel", background=UI["surface"], foreground=UI["muted"], font=("Arial", 9))
        style.configure("Accent.TButton", background=UI["purple"], foreground="#FFFFFF", borderwidth=0, padding=(12, 7), font=("Arial", 10, "bold"))
        style.map("Accent.TButton", background=[("active", UI["purple_hover"])])
        style.configure("Primary.TButton", background=UI["green"], foreground="#08120E", borderwidth=0, padding=(12, 7), font=("Arial", 10, "bold"))
        style.map("Primary.TButton", background=[("active", UI["green_hover"])])
        style.configure("TButton", background=UI["surface3"], foreground=UI["text"], borderwidth=0, padding=(10, 6))
        style.map("TButton", background=[("active", UI["hover"]), ("pressed", UI["purple"])], foreground=[("disabled", UI["muted"])])
        style.configure("TCheckbutton", background=UI["surface"], foreground=UI["text"])
        style.map("TCheckbutton", background=[("active", UI["surface"])])
        style.configure("TRadiobutton", background=UI["surface"], foreground=UI["text"])
        style.configure("TEntry", fieldbackground=UI["surface2"], foreground=UI["text"], bordercolor=UI["line"], lightcolor=UI["line"], darkcolor=UI["line"], insertcolor=UI["text"])
        style.configure("TCombobox", fieldbackground=UI["surface2"], background=UI["surface2"], foreground=UI["text"], arrowcolor=UI["muted"], bordercolor=UI["line"], lightcolor=UI["line"], darkcolor=UI["line"])
        style.map("TCombobox", fieldbackground=[("readonly", UI["surface2"])], foreground=[("readonly", UI["text"])], selectbackground=[("readonly", UI["surface2"])], selectforeground=[("readonly", UI["text"])])
        style.configure("TSpinbox", fieldbackground=UI["surface2"], foreground=UI["text"], arrowcolor=UI["muted"], bordercolor=UI["line"], lightcolor=UI["line"], darkcolor=UI["line"])

        style.configure("Treeview", background=UI["surface"], fieldbackground=UI["surface"], foreground=UI["text"], borderwidth=0, relief="flat", rowheight=34, font=("Arial", 10))
        style.map("Treeview", background=[("selected", UI["purple"])], foreground=[("selected", "#FFFFFF")])
        style.configure("Treeview.Heading", background=UI["surface2"], foreground=UI["muted"], borderwidth=0, relief="flat", padding=(10, 9), font=("Arial", 10, "bold"))
        style.map("Treeview.Heading", background=[("active", UI["surface3"])])
        style.configure("Vertical.TScrollbar", background=UI["surface3"], troughcolor=UI["surface"], bordercolor=UI["surface"], arrowcolor=UI["muted"])
        style.configure("Horizontal.TScrollbar", background=UI["surface3"], troughcolor=UI["surface"], bordercolor=UI["surface"], arrowcolor=UI["muted"])
        style.configure("TNotebook", background=UI["surface"], borderwidth=0)
        style.configure("TNotebook.Tab", background=UI["surface2"], foreground=UI["muted"], borderwidth=0, padding=(15, 9), font=("Arial", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", UI["surface3"]), ("active", UI["hover"])], foreground=[("selected", UI["text"])])

    def _build(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # Sidebar: estructura fija como el mockup elegido.
        self.sidebar = ctk.CTkFrame(self, width=205, corner_radius=0, fg_color=UI["sidebar"])
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(20, weight=1)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(22, 18))
        ctk.CTkLabel(brand, text="MTG Organizer", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=19, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(brand, text="Colección & Commander", text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=10)).pack(anchor="w", pady=(2,0))

        self.nav_buttons = {}
        nav_items = [
            ("collection", "▦   Mi colección"),
            ("decks", "♜   Commander"),
            ("add", "＋   Añadir cartas"),
            ("analytics", "◫   Analíticas"),
        ]
        for row, (key, label) in enumerate(nav_items, start=1):
            b = ctk.CTkButton(
                self.sidebar, text=label, anchor="w", height=42, corner_radius=8,
                fg_color="transparent", hover_color=UI["hover"], text_color=UI["muted"],
                font=ctk.CTkFont(family="Arial", size=11, weight="bold"), command=lambda k=key: self._show_main_section(k)
            )
            b.grid(row=row, column=0, sticky="ew", padx=11, pady=3)
            self.nav_buttons[key] = b

        ctk.CTkFrame(self.sidebar, height=1, fg_color=UI["line"]).grid(row=6, column=0, sticky="ew", padx=16, pady=16)
        ctk.CTkLabel(self.sidebar, text="ATAJOS", text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=9, weight="bold")).grid(row=7, column=0, sticky="w", padx=20, pady=(0,5))
        self.undo_button = ctk.CTkButton(
            self.sidebar, text="↶   Deshacer", anchor="w", height=38, corner_radius=8,
            fg_color="transparent", hover_color=UI["hover"], text_color=UI["muted"],
            command=self.undo_last_action, state="disabled"
        )
        self.undo_button.grid(row=8, column=0, sticky="ew", padx=11, pady=2)

        ctk.CTkLabel(self.sidebar, text="V3.2c", text_color="#626A78", font=ctk.CTkFont(family="Arial", size=9)).grid(row=21, column=0, sticky="sw", padx=20, pady=16)

        # Zona principal.
        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color=UI["bg"])
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_rowconfigure(1, weight=1)
        self.main.grid_columnconfigure(0, weight=1)

        topbar = ctk.CTkFrame(self.main, height=68, corner_radius=0, fg_color=UI["surface"])
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.grid_propagate(False)
        topbar.grid_columnconfigure(1, weight=1)

        self.page_title = ctk.CTkLabel(topbar, text="Mi colección", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=20, weight="bold"))
        self.page_title.grid(row=0, column=0, padx=(24,16), pady=18, sticky="w")

        self.global_search_var = tk.StringVar()
        self.global_search = ctk.CTkEntry(
            topbar, textvariable=self.global_search_var, height=36, corner_radius=8,
            fg_color=UI["surface2"], border_color=UI["line"], border_width=1,
            text_color=UI["text"], placeholder_text="Buscar en mi colección...", placeholder_text_color=UI["muted"]
        )
        self.global_search.grid(row=0, column=1, padx=12, pady=16, sticky="ew")
        self.global_search.bind("<Return>", lambda e: self._run_global_search())

        ctk.CTkButton(
            topbar, text="＋  Añadir carta", width=126, height=36, corner_radius=8,
            fg_color=UI["green"], hover_color=UI["green_hover"], text_color="#07120E",
            font=ctk.CTkFont(family="Arial", size=10, weight="bold"), command=lambda: self._show_main_section("add")
        ).grid(row=0, column=2, padx=(8,22), pady=16)

        self.content_host = ctk.CTkFrame(self.main, corner_radius=0, fg_color=UI["bg"])
        self.content_host.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self.content_host.grid_rowconfigure(0, weight=1)
        self.content_host.grid_columnconfigure(0, weight=1)

        self.tab_collection = ctk.CTkFrame(self.content_host, fg_color=UI["bg"], corner_radius=0)
        self.tab_decks = ctk.CTkFrame(self.content_host, fg_color=UI["bg"], corner_radius=0)
        self.tab_add = ctk.CTkFrame(self.content_host, fg_color=UI["bg"], corner_radius=0)
        self.tab_analytics = ctk.CTkFrame(self.content_host, fg_color=UI["bg"], corner_radius=0)
        for frame in (self.tab_collection, self.tab_decks, self.tab_add, self.tab_analytics):
            frame.grid(row=0, column=0, sticky="nsew")

        self._build_add_tab()
        self._build_collection_tab()
        self._build_decks_tab()
        self._build_analytics_tab()

        self.status_bar = ctk.CTkLabel(
            self.main, textvariable=self.status_var, anchor="w", height=28,
            fg_color=UI["surface"], text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=9)
        )
        self.status_bar.grid(row=2, column=0, sticky="ew", padx=0, pady=0)

        self._show_main_section("collection")

    def _show_main_section(self, key):
        frames = {
            "collection": self.tab_collection,
            "decks": self.tab_decks,
            "add": self.tab_add,
            "analytics": self.tab_analytics,
        }
        titles = {
            "collection": "Mi colección",
            "decks": "Commander",
            "add": "Añadir cartas",
            "analytics": "Analíticas",
        }
        if key not in frames:
            return
        frames[key].tkraise()
        self.page_title.configure(text=titles[key])
        for k, button in self.nav_buttons.items():
            if k == key:
                button.configure(fg_color=UI["surface3"], text_color=UI["text"])
            else:
                button.configure(fg_color="transparent", text_color=UI["muted"])
        # Colección y Commander ya se refrescan cuando realmente cambian datos.
        # Evitamos reconstruir tablas completas solo por cambiar de sección.
        if key == "analytics":
            self._refresh_analytics()

    def _run_global_search(self):
        q = self.global_search_var.get().strip()
        self._show_main_section("collection")
        if hasattr(self, "collection_filter"):
            self.collection_filter.set(q)
            self.refresh_collection()

    def _build_analytics_tab(self):
        wrap = ctk.CTkFrame(self.tab_analytics, fg_color=UI["bg"], corner_radius=0)
        wrap.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(wrap, text="Resumen de tu colección", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=18, weight="bold")).pack(anchor="w", pady=(0,14))
        cards = ctk.CTkFrame(wrap, fg_color="transparent")
        cards.pack(fill="x")
        self.analytics_labels = {}
        items = [("physical","Cartas físicas"),("prints","Impresiones"),("free","Disponibles"),("decks","Mazos") ]
        for key, title in items:
            box = ctk.CTkFrame(cards, fg_color=UI["surface"], corner_radius=12, border_width=1, border_color=UI["line"])
            box.pack(side="left", fill="x", expand=True, padx=(0,10))
            ctk.CTkLabel(box, text=title, text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=10)).pack(anchor="w", padx=16, pady=(14,2))
            lbl = ctk.CTkLabel(box, text="—", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=26, weight="bold"))
            lbl.pack(anchor="w", padx=16, pady=(0,14))
            self.analytics_labels[key]=lbl
        note = ctk.CTkLabel(
            wrap, text="Esta pantalla resume tu colección actual. Las estadísticas detalladas de cada mazo siguen dentro de Commander.",
            text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=10), anchor="w"
        )
        note.pack(fill="x", pady=18)

    def _refresh_analytics(self):
        if not hasattr(self, "analytics_labels"):
            return
        rows = list(self.db.list_collection(""))
        physical = sum(int(r["quantity"] or 0) for r in rows)
        free = 0
        for r in rows:
            uses = self.db.card_usage(r["id"])
            used = sum(int(u["quantity"] or 0) for u in uses)
            free += max(0, int(r["quantity"] or 0) - used)
        decks = list(self.db.list_decks())
        vals={"physical":physical,"prints":len(rows),"free":free,"decks":len(decks)}
        for k,v in vals.items():
            self.analytics_labels[k].configure(text=str(v))

    # ---------------- Registrar ----------------
    def _build_add_tab(self):
        wrap = ttk.Frame(self.tab_add)
        wrap.pack(fill="both", expand=True, padx=10, pady=8)

        quick = ttk.LabelFrame(wrap, text="Entrada rápida")
        quick.pack(fill="x", pady=(0, 7))

        self.set_code = tk.StringVar()
        self.collector_number = tk.StringVar()
        self.qty = tk.IntVar(value=1)
        self.lang = tk.StringVar(value="Inglés")
        self.finish = tk.StringVar(value="Normal")
        self.keep_set = tk.BooleanVar(value=False)

        labels = ["Edición", "Número", "Cantidad", "Idioma", "Acabado"]
        for i, lab in enumerate(labels):
            ttk.Label(quick, text=lab + ":").grid(row=0, column=i*2, padx=(7, 3), pady=7, sticky="w")

        e1 = ttk.Entry(quick, textvariable=self.set_code, width=11)
        e1.grid(row=0, column=1, padx=(0,8), pady=12)
        e2 = ttk.Entry(quick, textvariable=self.collector_number, width=11)
        e2.grid(row=0, column=3, padx=(0,8), pady=12)

        ttk.Spinbox(quick, from_=1, to=99, textvariable=self.qty, width=6).grid(row=0, column=5, padx=(0,8))
        ttk.Combobox(
            quick, textvariable=self.lang, state="readonly", width=13,
            values=["Inglés", "Español", "Portugués", "Francés", "Italiano", "Alemán", "Japonés", "Otro"]
        ).grid(row=0, column=7, padx=(0,8))
        ttk.Combobox(
            quick, textvariable=self.finish, state="readonly", width=10,
            values=["Normal", "Foil", "Etched"]
        ).grid(row=0, column=9, padx=(0,8))

        ttk.Button(quick, text="Agregar carta", style="Primary.TButton", command=self.add_by_code).grid(
            row=0, column=10, padx=10, pady=12
        )
        ttk.Checkbutton(
            quick, text="Mantener edición", variable=self.keep_set
        ).grid(row=0, column=11, padx=(0,10), pady=12, sticky="w")

        self.set_entry = e1
        self.number_entry = e2

        ttk.Label(
            quick,
            text="Ejemplo: CMR + 144. La rareza impresa (C/U/R/M) no es necesaria.",
            style="Sub.TLabel"
        ).grid(row=1, column=0, columnspan=12, padx=10, pady=(0,5), sticky="w")

        by_name = ttk.LabelFrame(wrap, text="Carta antigua o sin número")
        by_name.pack(fill="x", pady=(0, 7))
        self.name_query = tk.StringVar()
        ttk.Label(by_name, text="Nombre o parte del nombre:").grid(row=0, column=0, padx=10, pady=12)
        ne = ttk.Entry(by_name, textvariable=self.name_query)
        ne.grid(row=0, column=1, padx=8, pady=12, sticky="ew")
        ttk.Button(by_name, text="Buscar y agregar", style="Primary.TButton", command=self.add_by_name).grid(row=0, column=2, padx=(10,4))
        ttk.Button(by_name, text="Identificar edición antigua", command=self.identify_old_printing).grid(row=0, column=3, padx=(4,10))
        by_name.columnconfigure(1, weight=1)

        result_box = ttk.LabelFrame(wrap, text="Última carta registrada")
        result_box.pack(fill="both", expand=True)

        self.last_result = tk.Text(
            result_box, height=8, wrap="word", font=("Arial", 10),
            bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF", relief="flat", padx=12, pady=12
        )
        self.last_result.pack(fill="both", expand=True, padx=5, pady=5)
        self.last_result.configure(state="disabled")

        e1.bind("<Return>", lambda e: e2.focus_set())
        e2.bind("<Return>", lambda e: self.add_by_code())
        ne.bind("<Return>", lambda e: self.add_by_name())

    def _lang_code(self):
        return {
            "Inglés":"en","Español":"es","Portugués":"pt","Francés":"fr",
            "Italiano":"it","Alemán":"de","Japonés":"ja","Otro":"und"
        }.get(self.lang.get(), "en")

    def _finish_code(self):
        return {"Normal":"nonfoil","Foil":"foil","Etched":"etched"}.get(self.finish.get(),"nonfoil")


    def _detect_treatment(self, card):
        tags = set(card.get("frame_effects") or [])
        promo_types = set(card.get("promo_types") or [])
        border = (card.get("border_color") or "").lower()
        treatments = []

        if card.get("full_art"):
            treatments.append("Full Art")
        if border == "borderless":
            treatments.append("Borderless")
        if "showcase" in tags or "showcase" in promo_types:
            treatments.append("Showcase")
        if "extendedart" in tags or "extendedart" in promo_types:
            treatments.append("Extended Art")
        if "retro" in tags or "retro" in promo_types:
            treatments.append("Retro Frame")
        if "inverted" in tags:
            treatments.append("Inverted")
        if card.get("promo"):
            treatments.append("Promo")

        if not treatments:
            return "Normal"
        return " / ".join(dict.fromkeys(treatments))

    def _show_last(self, card, qty):
        lang = self.lang.get()
        finish = self.finish.get()
        text = (
            f"{card['name']}\n"
            f"{card.get('set_name','')} · {card.get('set','').upper()} {card.get('collector_number','')}\n"
            f"{qty} copia(s) · {lang} · {finish} · {self._treatment_name(self._detect_treatment(card))}\n\n"
            f"Tipo: {card.get('type_line','')}\n"
            f"Coste: {card.get('mana_cost','') or '—'} · Valor de maná: {card.get('cmc',0)}\n"
            f"Identidad de color: {' '.join(card.get('color_identity') or []) or 'Incolora'}\n"
            f"Commander: {('Legal' if card.get('legalities',{}).get('commander') == 'legal' else 'No legal / revisar')}\n\n"
            f"{card.get('oracle_text','') or card.get('printed_text','') or ''}"
        )
        self.last_result.configure(state="normal")
        self.last_result.delete("1.0", "end")
        self.last_result.insert("1.0", text)
        self.last_result.configure(state="disabled")

    def add_by_code(self):
        code = self.set_code.get().strip().lower()
        number = self.collector_number.get().strip()
        qty = max(1, int(self.qty.get() or 1))
        if not code or not number:
            messagebox.showerror("Faltan datos", "Escribe el código de edición y el número.")
            return
        try:
            self.status_var.set(f"Buscando {code.upper()} {number}...")
            self.update_idletasks()
            card = self.scry.get_by_set_number(code, number)
            cid=self.db.add_card(card, qty, self._lang_code(), self._finish_code(), self._detect_treatment(card))
            self._auto_assign_new_physical_card(cid,card["name"],qty)
            self._show_last(card, qty)
            self.collector_number.set("")
            if self.keep_set.get():
                self.number_entry.focus_set()
            else:
                self.set_code.set("")
                self.set_entry.focus_set()
            self.refresh_collection()
            self.status_var.set(f"Agregada: {card['name']}")
        except ScryfallError as e:
            messagebox.showerror("No encontrada", str(e))
            self.status_var.set("No se pudo agregar la carta.")

    def add_by_name(self):
        q = self.name_query.get().strip()
        qty = max(1, int(self.qty.get() or 1))
        if not q:
            messagebox.showerror("Falta nombre", "Escribe el nombre o parte del nombre.")
            return
        try:
            self.status_var.set(f"Buscando «{q}»...")
            self.update_idletasks()
            card = self.scry.get_by_name(q)
            cid=self.db.add_card(card, qty, self._lang_code(), self._finish_code(), self._detect_treatment(card))
            self._auto_assign_new_physical_card(cid,card["name"],qty)
            self._show_last(card, qty)
            self.name_query.set("")
            self.refresh_collection()
            self.status_var.set(f"Agregada: {card['name']}")
        except ScryfallError as e:
            messagebox.showerror("No encontrada", str(e))
            self.status_var.set("No se pudo agregar la carta.")



    def identify_old_printing(self):
        name = self.name_query.get().strip()
        if not name:
            name = simpledialog.askstring("Carta antigua", "Escribe el nombre de la carta:")
            if not name:
                return

        self.status_var.set(f"Buscando impresiones de «{name}»...")
        self.update_idletasks()
        try:
            prints = self.scry.get_printings_by_name(name)
        except ScryfallError as e:
            messagebox.showerror("No encontrada", str(e))
            self.status_var.set("No se pudieron obtener las impresiones.")
            return

        if not prints:
            messagebox.showinfo("Sin resultados", "No encontré impresiones para esa carta.")
            return

        self._show_printing_picker(name, prints)

    def _show_printing_picker(self, searched_name, prints):
        win = tk.Toplevel(self)
        win.title(f"Identificar edición — {searched_name}")
        win.geometry("980x600")
        win.minsize(820, 480)
        win.transient(self)
        win.grab_set()

        top = ttk.Frame(win)
        top.pack(fill="x", padx=12, pady=10)

        ttk.Label(
            top,
            text="Elige la impresión que coincida con tu carta física.",
            font=("Arial", 12, "bold")
        ).pack(anchor="w")
        ttk.Label(
            top,
            text="Usa el año, la edición, el marco, el ilustrador y el número para distinguirla. "
                 "El número mostrado por Scryfall puede existir aunque no esté impreso en cartas antiguas.",
            style="Sub.TLabel"
        ).pack(anchor="w", pady=(2,8))

        filter_row = ttk.Frame(top)
        filter_row.pack(fill="x")
        year_var = tk.StringVar(value="Todos")
        set_search = tk.StringVar()

        years = sorted({
            (p.get("released_at") or "")[:4]
            for p in prints if (p.get("released_at") or "")[:4].isdigit()
        })
        ttk.Label(filter_row, text="Año:").pack(side="left")
        year_box = ttk.Combobox(filter_row, textvariable=year_var, state="readonly",
                                width=10, values=["Todos"] + years)
        year_box.pack(side="left", padx=(4,12))
        ttk.Label(filter_row, text="Filtrar edición:").pack(side="left")
        set_entry = ttk.Entry(filter_row, textvariable=set_search, width=28)
        set_entry.pack(side="left", padx=4)

        cols = ("date","set","code","number","lang","artist","version")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
        heads = {
            "date":"Fecha","set":"Edición","code":"Código","number":"Nº Scryfall",
            "lang":"Idioma","artist":"Ilustrador","version":"Versión"
        }
        widths = {"date":90,"set":250,"code":70,"number":90,"lang":70,"artist":190,"version":160}
        for c in cols:
            tree.heading(c, text=heads[c])
            tree.column(c, width=widths[c], anchor="w")
        tree.pack(fill="both", expand=True, padx=12, pady=(0,8))

        indexed = {}

        def lang_name(code):
            return self._lang_name(code)

        def refresh_picker(*_):
            for item in tree.get_children():
                tree.delete(item)
            indexed.clear()
            yf = year_var.get()
            sf = set_search.get().strip().lower()

            for i, card in enumerate(prints):
                date = card.get("released_at") or ""
                if yf != "Todos" and not date.startswith(yf):
                    continue
                hay = " ".join([
                    card.get("set_name") or "",
                    card.get("set") or "",
                    card.get("collector_number") or "",
                    card.get("artist") or ""
                ]).lower()
                if sf and sf not in hay:
                    continue

                iid = f"p{i}"
                indexed[iid] = card
                tree.insert("", "end", iid=iid, values=(
                    date,
                    card.get("set_name") or "",
                    (card.get("set") or "").upper(),
                    card.get("collector_number") or "—",
                    lang_name(card.get("lang") or "en"),
                    card.get("artist") or "—",
                    self._detect_treatment(card)
                ))

        def choose():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Selecciona una impresión", "Selecciona una fila primero.", parent=win)
                return
            card = indexed[sel[0]]

            # Adopt the language reported by Scryfall when known.
            inv_lang = {
                "en":"Inglés","es":"Español","pt":"Portugués","fr":"Francés",
                "it":"Italiano","de":"Alemán","ja":"Japonés"
            }
            if card.get("lang") in inv_lang:
                self.lang.set(inv_lang[card["lang"]])

            qty = max(1, int(self.qty.get() or 1))
            try:
                self.db.add_card(
                    card, qty, self._lang_code(), self._finish_code(),
                    self._detect_treatment(card)
                )
                self._show_last(card, qty)
                self.name_query.set("")
                self.refresh_collection()
                self.status_var.set(
                    f"Agregada: {card['name']} · {(card.get('set') or '').upper()} "
                    f"{card.get('collector_number') or ''}"
                )
                win.destroy()
            except Exception as e:
                messagebox.showerror("No se pudo agregar", str(e), parent=win)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=12, pady=(0,12))
        ttk.Button(buttons, text="Cancelar", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Usar esta impresión", command=choose).pack(side="right", padx=6)

        year_box.bind("<<ComboboxSelected>>", refresh_picker)
        set_entry.bind("<KeyRelease>", refresh_picker)
        tree.bind("<Double-1>", lambda e: choose())

        refresh_picker()
        if tree.get_children():
            tree.selection_set(tree.get_children()[0])

    # ---------------- Colección ----------------
    def _build_collection_tab(self):
        outer = ctk.CTkFrame(self.tab_collection, fg_color=UI["bg"], corner_radius=0)
        outer.pack(fill="both", expand=True, padx=22, pady=20)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(outer, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0,10))
        self.collection_mode = tk.StringVar(value="private")
        seg = ctk.CTkSegmentedButton(
            top, values=["Mi colección", "Disponibles"], height=34, corner_radius=8,
            fg_color=UI["surface2"], selected_color=UI["purple"], selected_hover_color=UI["purple_hover"],
            unselected_color=UI["surface2"], unselected_hover_color=UI["hover"], text_color=UI["text"],
            command=self._switch_collection_mode
        )
        seg.set("Mi colección")
        seg.pack(side="left")

        self.collection_left = ctk.CTkFrame(outer, fg_color=UI["surface"], corner_radius=12, border_width=1, border_color=UI["line"])
        self.collection_left.grid(row=1, column=0, sticky="nsew", padx=(0,12))
        self.collection_left.grid_rowconfigure(0, weight=1)
        self.collection_left.grid_columnconfigure(0, weight=1)

        self.private_collection_tab = ctk.CTkFrame(self.collection_left, fg_color=UI["surface"], corner_radius=10)
        self.public_collection_tab = ctk.CTkFrame(self.collection_left, fg_color=UI["surface"], corner_radius=10)
        for f in (self.private_collection_tab,self.public_collection_tab):
            f.grid(row=0,column=0,sticky="nsew",padx=10,pady=10)

        self._build_private_collection()
        self._build_public_collection()
        self.private_collection_tab.tkraise()

        self.collection_preview = ctk.CTkFrame(outer, width=305, fg_color=UI["surface"], corner_radius=12, border_width=1, border_color=UI["line"])
        self.collection_preview.grid(row=1, column=1, sticky="ns")
        self.collection_preview.grid_propagate(False)
        self._build_collection_preview()

        # Si la ventana se hace angosta, ocultamos el panel derecho para mantener la tabla práctica.
        outer.bind("<Configure>", self._collection_responsive_layout, add="+")

    def _switch_collection_mode(self, value):
        if value == "Disponibles":
            self.public_collection_tab.tkraise()
        else:
            self.private_collection_tab.tkraise()

    def _collection_responsive_layout(self, event=None):
        try:
            width = self.tab_collection.winfo_width()
            if width < 1080:
                self.collection_preview.grid_remove()
            else:
                self.collection_preview.grid()
        except Exception:
            pass

    def _build_collection_preview(self):
        p = self.collection_preview
        ctk.CTkLabel(p, text="Detalle de carta", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=15, weight="bold")).pack(anchor="w", padx=16, pady=(15,10))
        self.preview_image = tk.Label(p, text="Selecciona una carta", bg=UI["surface2"], fg=UI["muted"], font=("Arial",10), bd=0, relief="flat")
        self.preview_image.pack(fill="x", padx=16, pady=(0,12), ipady=72)
        self.preview_name = ctk.CTkLabel(p, text="—", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=16, weight="bold"), anchor="w", justify="left", wraplength=270)
        self.preview_name.pack(fill="x", padx=16, pady=(0,2))
        self.preview_meta = ctk.CTkLabel(p, text="", text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=9), anchor="w", justify="left", wraplength=270)
        self.preview_meta.pack(fill="x", padx=16, pady=(0,12))
        self.preview_type = ctk.CTkLabel(p, text="", text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=10, weight="bold"), anchor="w", justify="left", wraplength=270)
        self.preview_type.pack(fill="x", padx=16, pady=(0,8))
        self.preview_oracle = ctk.CTkTextbox(p, height=145, corner_radius=8, fg_color=UI["surface2"], border_width=0, text_color=UI["text"], font=ctk.CTkFont(family="Arial", size=10), wrap="word")
        self.preview_oracle.pack(fill="x", padx=16, pady=(0,10))
        self.preview_oracle.configure(state="disabled")
        self.preview_usage = ctk.CTkLabel(p, text="", text_color=UI["muted"], font=ctk.CTkFont(family="Arial", size=9), anchor="w", justify="left", wraplength=270)
        self.preview_usage.pack(fill="x", padx=16, pady=(0,12))
        btns = ctk.CTkFrame(p, fg_color="transparent")
        btns.pack(fill="x", side="bottom", padx=16, pady=16)
        ctk.CTkButton(btns, text="Editar", width=90, height=34, fg_color=UI["surface3"], hover_color=UI["hover"], command=self.edit_selected_card).pack(side="left")
        ctk.CTkButton(btns, text="Mover / mazo", height=34, fg_color=UI["green"], hover_color=UI["green_hover"], text_color="#07120E", command=self.move_selected_copy).pack(side="right")
        self._preview_request_key = None

    def _update_collection_preview(self, event=None):
        if not hasattr(self, "collection_tree"):
            return
        sel=self.collection_tree.selection()
        if not sel:
            return
        try:
            cid=int(sel[0])
        except Exception:
            return
        card=self.db.get_collection_card(cid)
        if not card:
            return
        self.preview_name.configure(text=card["name"] or "—")
        self.preview_meta.configure(text=f"{(card['set_name'] or '')} · {(card['set_code'] or '').upper()} {card['collector_number'] or ''} · {self._row_rarity(card)}")
        self.preview_type.configure(text=card["type_line"] or "")
        self.preview_oracle.configure(state="normal")
        self.preview_oracle.delete("1.0","end")
        self.preview_oracle.insert("1.0", card["oracle_text"] or "Sin texto Oracle guardado.")
        self.preview_oracle.configure(state="disabled")
        usage=self.db.card_usage(cid)
        used=sum(int(u["quantity"] or 0) for u in usage)
        free=max(0,int(card["quantity"] or 0)-used)
        usage_text=f"Tienes {int(card['quantity'] or 0)} · Disponibles {free}"
        if usage:
            usage_text += "\n" + " · ".join(f"{u['deck_name']} ×{u['quantity']}" for u in usage)
        self.preview_usage.configure(text=usage_text)
        self.preview_image.configure(image="", text="Cargando imagen…", bg=UI["surface2"], fg=UI["muted"])

        key=((card["set_code"] or "").lower(),str(card["collector_number"] or ""))
        self._preview_request_key=key
        def worker():
            try:
                live=self._scry_detail_cache.get(key)
                if live is None:
                    live=self.scry.get_by_set_number(key[0],key[1])
                    self._scry_detail_cache[key]=live
                url=self._scry_image_url(live)
                if not url:
                    raise RuntimeError()
                req=urllib.request.Request(url,headers={"User-Agent":"MTG Organizer/3.0"})
                with urllib.request.urlopen(req,timeout=12) as response:
                    raw=response.read()
                encoded=base64.b64encode(raw)
                def apply():
                    if self._preview_request_key != key:
                        return
                    try:
                        if Image is not None and ImageTk is not None:
                            # Escalado de alta calidad: mantiene texto e ilustración más nítidos.
                            pil_img = Image.open(io.BytesIO(raw)).convert("RGB")
                            pil_img.thumbnail((265, 370), Image.Resampling.LANCZOS)
                            img = ImageTk.PhotoImage(pil_img)
                        else:
                            # Fallback sin Pillow: funciona, aunque con menor calidad.
                            img=tk.PhotoImage(data=encoded)
                            factor=max(1,int(max(img.width()/265,img.height()/370)))
                            if factor>1:
                                img=img.subsample(factor,factor)
                        self.preview_image.configure(image=img,text="")
                        self.preview_image.image=img
                    except Exception:
                        self.preview_image.configure(text="Imagen no disponible",image="")
                self.after(0,apply)
            except Exception:
                self.after(0,lambda: self.preview_image.configure(text="Imagen no disponible",image="") if self._preview_request_key==key else None)
        threading.Thread(target=worker,daemon=True).start()

    def _build_private_collection(self):
        filters = ttk.LabelFrame(self.private_collection_tab, text="Filtros")
        filters.pack(fill="x", pady=(0,5))

        self.collection_filter = tk.StringVar()
        self.type_filter = tk.StringVar(value="Todos")
        self.set_filter = tk.StringVar(value="Todas")
        self.lang_filter = tk.StringVar(value="Todos")
        self.finish_filter = tk.StringVar(value="Todos")
        self.subtype_filter = tk.StringVar()
        self.availability_filter = tk.StringVar(value="Todas")

        # Identidad Commander: se pueden marcar varios colores a la vez.
        self.identity_vars = {c: tk.BooleanVar(value=False) for c in "WUBRG"}
        self.identity_colorless = tk.BooleanVar(value=False)
        self.identity_exact = tk.BooleanVar(value=False)

        ttk.Label(filters, text="Buscar:").grid(row=0, column=0, padx=(8,3), pady=(6,3), sticky="w")
        search_entry = ttk.Entry(filters, textvariable=self.collection_filter, width=32)
        search_entry.grid(row=0, column=1, columnspan=3, padx=(0,6), pady=(6,3), sticky="ew")
        search_entry.bind("<KeyRelease>", lambda e: self.refresh_collection())
        search_entry.bind("<Return>", lambda e: self.refresh_collection())
        ttk.Button(filters, text="Buscar", style="Accent.TButton", command=self.refresh_collection).grid(row=0, column=4, padx=(0,8), pady=(6,3))
        ttk.Button(filters, text="Limpiar", command=self.clear_collection_filters).grid(row=0, column=10, columnspan=2, padx=8, pady=(6,3), sticky="e")

        # Identidad de color: por defecto busca cartas que CONTENGAN todos los colores marcados.
        identity_box = ttk.LabelFrame(filters, text="Identidad de color")
        identity_box.grid(row=1, column=0, columnspan=12, padx=8, pady=(2,3), sticky="ew")
        identity_names = [("W","Blanco"),("U","Azul"),("B","Negro"),("R","Rojo"),("G","Verde")]
        for i, (code, name) in enumerate(identity_names):
            ttk.Checkbutton(identity_box, text=name, variable=self.identity_vars[code],
                            command=self._identity_color_changed).pack(side="left", padx=(8 if i == 0 else 4,4), pady=6)
        ttk.Checkbutton(identity_box, text="Incolora", variable=self.identity_colorless,
                        command=self._identity_colorless_changed).pack(side="left", padx=4, pady=6)
        ttk.Separator(identity_box, orient="vertical").pack(side="left", fill="y", padx=8, pady=4)
        ttk.Checkbutton(identity_box, text="Coincidencia exacta", variable=self.identity_exact,
                        command=self.refresh_collection).pack(side="left", padx=4, pady=6)
        ttk.Label(identity_box, text="Sin exacta: contiene los colores marcados", style="Sub.TLabel").pack(side="left", padx=10)

        specs = [
            ("Tipo", ttk.Combobox(filters, textvariable=self.type_filter, state="readonly", width=14,
                values=["Todos","Criatura","Artefacto","Encantamiento","Instantáneo","Conjuro","Planeswalker","Tierra","Batalla"])),
            ("Edición", ttk.Combobox(filters, textvariable=self.set_filter, state="readonly", width=12)),
            ("Idioma", ttk.Combobox(filters, textvariable=self.lang_filter, state="readonly", width=12,
                values=["Todos","Inglés","Español","Portugués","Francés","Italiano","Alemán","Japonés","Otro"])),
            ("Acabado", ttk.Combobox(filters, textvariable=self.finish_filter, state="readonly", width=10,
                values=["Todos","Normal","Foil","Etched"])),
            ("Disponibilidad", ttk.Combobox(filters, textvariable=self.availability_filter, state="readonly", width=13,
                values=["Todas","Disponibles","En mazo","Sin disponibles"])),
        ]
        self.set_box = specs[1][1]
        for i,(lab,widget) in enumerate(specs):
            ttk.Label(filters, text=lab + ":").grid(row=2, column=i*2, padx=(8,3), pady=(3,6), sticky="w")
            widget.grid(row=2, column=i*2+1, padx=(0,10), pady=(3,6), sticky="w")
            widget.bind("<<ComboboxSelected>>", lambda e: self.refresh_collection())
        ttk.Label(filters, text="Subtipo:").grid(row=3, column=0, padx=(8,3), pady=(0,5), sticky="w")
        subtype_entry = ttk.Entry(filters, textvariable=self.subtype_filter, width=24)
        subtype_entry.grid(row=3, column=1, columnspan=3, padx=(0,10), pady=(0,5), sticky="ew")
        subtype_entry.bind("<KeyRelease>", lambda e: self.refresh_collection())
        ttk.Label(filters, text="Ej.: Ángel, Elfo, Dinosaurio, Equipo, Aura", style="Sub.TLabel").grid(
            row=3, column=4, columnspan=6, padx=(0,8), pady=(0,5), sticky="w")
        filters.columnconfigure(1, weight=1)

        actions = ttk.Frame(self.private_collection_tab)
        actions.pack(fill="x", pady=(0,5))
        self.collection_summary = ttk.Label(actions, text="")
        self.collection_summary.pack(side="left")
        ttk.Button(actions, text="Mover copia", style="Primary.TButton", command=self.move_selected_copy).pack(side="right", padx=4)
        ttk.Button(actions, text="Editar", command=self.edit_selected_card).pack(side="right", padx=4)
        ttk.Button(actions, text="Historial", command=self.show_movement_history).pack(side="right", padx=4)
        ttk.Button(actions, text="Quitar 1", command=self.remove_one_selected).pack(side="right", padx=4)
        ttk.Button(actions, text="Exportar CSV", command=self.export_collection).pack(side="right", padx=4)
        ttk.Button(actions, text="Restablecer columnas", command=self._reset_collection_columns).pack(side="right", padx=4)

        cols = ("qty","name","mana","identity","type","rarity","set","number","lang","finish","treatment","assigned","commander")
        tree_frame = ttk.Frame(self.private_collection_tab)
        tree_frame.pack(fill="both", expand=True)
        self.collection_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        headings = {
            "qty":"Cant.","name":"Nombre","mana":"Coste de maná","identity":"Identidad","type":"Tipo","rarity":"Rareza",
            "set":"Edición","number":"Nº","lang":"Idioma","finish":"Acabado","treatment":"Versión",
            "assigned":"Uso","commander":"Commander"
        }
        widths = {"qty":48,"name":225,"mana":100,"identity":82,"type":215,"rarity":72,"set":62,"number":58,
                  "lang":76,"finish":68,"treatment":110,"assigned":250,"commander":76}
        # Evita que las columnas importantes se puedan arrastrar hasta quedar
        # prácticamente invisibles. "Nombre" conserva libertad para agrandarse,
        # pero nunca baja de un ancho cómodo para recuperar el separador.
        minwidths = {
            "qty":42, "name":140, "mana":78, "identity":68, "type":120,
            "rarity":60, "set":52, "number":48, "lang":62, "finish":58,
            "treatment":82, "assigned":130, "commander":68
        }
        for c in cols:
            self.collection_tree.heading(c, text=headings[c], command=lambda col=c: self.sort_collection(col))
            self.collection_tree.column(
                c,
                width=widths[c],
                minwidth=minwidths[c],
                anchor="w",
                # Importante: no permitir que ttk redistribuya automáticamente
                # el ancho. Así cada columna conserva exactamente el ancho
                # que el usuario deja al arrastrar el separador.
                stretch=False
            )
        self._collection_default_widths = dict(widths)

        def _tree_yview(*args):
            self.collection_tree.yview(*args)
            self.after_idle(self._redraw_collection_color_chips)
        def _tree_xview(*args):
            self.collection_tree.xview(*args)
            self.after_idle(self._redraw_collection_color_chips)
        def _yscroll_set(first, last):
            yscroll.set(first, last)
            self.after_idle(self._redraw_collection_color_chips)
        def _xscroll_set(first, last):
            xscroll.set(first, last)
            self.after_idle(self._redraw_collection_color_chips)

        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=_tree_yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=_tree_xview)
        self.collection_tree.configure(yscrollcommand=_yscroll_set, xscrollcommand=_xscroll_set)
        self.collection_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.collection_tree.bind("<Double-1>", lambda e: self.show_selected_card_detail(self.collection_tree, private=True))
        self.collection_tree.bind("<<TreeviewSelect>>", self._update_collection_preview, add="+")
        self.collection_tree.bind("<Button-3>", self._show_collection_context_menu)
        self.collection_tree.bind("<Configure>", lambda e: self.after_idle(self._redraw_collection_color_chips))
        self.collection_tree.bind("<MouseWheel>", lambda e: self.after_idle(self._redraw_collection_color_chips), add="+")
        self.collection_tree.bind("<Button-4>", lambda e: self.after_idle(self._redraw_collection_color_chips), add="+")
        self.collection_tree.bind("<Button-5>", lambda e: self.after_idle(self._redraw_collection_color_chips), add="+")
        self._collection_color_values = {}
        self._collection_color_overlays = []
        self._collection_sort = ("name", False)

    def _reset_collection_columns(self):
        if not hasattr(self, "collection_tree"):
            return
        defaults = getattr(self, "_collection_default_widths", {})
        for column, width in defaults.items():
            try:
                self.collection_tree.column(column, width=width, stretch=False)
            except Exception:
                pass
        self.after_idle(self._redraw_collection_color_chips)
        self.status_var.set("Anchos de columnas restablecidos.")

    def _build_public_collection(self):
        top = ttk.Frame(self.public_collection_tab)
        top.pack(fill="x", pady=(0,8))
        self.public_filter = tk.StringVar()
        ttk.Label(top, text="Buscar disponibles:").pack(side="left")
        e = ttk.Entry(top, textvariable=self.public_filter, width=34)
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda ev: self.refresh_public_collection())
        ttk.Button(top, text="Copiar lista pública", command=self.copy_public_list).pack(side="right")

        self.public_summary = ttk.Label(self.public_collection_tab, text="")
        self.public_summary.pack(fill="x", pady=(0,6))

        cols = ("free","name","set","number","lang","finish","treatment","type")
        public_tree_frame = ttk.Frame(self.public_collection_tab)
        public_tree_frame.pack(fill="both", expand=True)
        self.public_tree = ttk.Treeview(public_tree_frame, columns=cols, show="headings")
        heads = {"free":"Disponibles","name":"Carta","set":"Edición","number":"Nº","lang":"Idioma","finish":"Acabado","treatment":"Versión","type":"Tipo"}
        widths = {"free":85,"name":230,"set":70,"number":65,"lang":90,"finish":80,"treatment":130,"type":260}
        for c in cols:
            self.public_tree.heading(c, text=heads[c])
            self.public_tree.column(c, width=widths[c], anchor="w")
        py = ttk.Scrollbar(public_tree_frame, orient="vertical", command=self.public_tree.yview)
        px = ttk.Scrollbar(public_tree_frame, orient="horizontal", command=self.public_tree.xview)
        self.public_tree.configure(yscrollcommand=py.set, xscrollcommand=px.set)
        self.public_tree.grid(row=0, column=0, sticky="nsew")
        py.grid(row=0, column=1, sticky="ns")
        px.grid(row=1, column=0, sticky="ew")
        public_tree_frame.rowconfigure(0, weight=1)
        public_tree_frame.columnconfigure(0, weight=1)
        self.public_tree.bind("<Double-1>", lambda e: self.show_selected_card_detail(self.public_tree, private=False))

    def _lang_name(self, code):
        return {"en":"Inglés","es":"Español","pt":"Portugués","fr":"Francés","it":"Italiano",
                "de":"Alemán","ja":"Japonés","und":"Otro"}.get(code, code or "Otro")

    def _finish_name(self, code):
        return {"nonfoil":"Normal","foil":"Foil","etched":"Etched"}.get(code, code or "Normal")

    def _treatment_name(self, treatment):
        return treatment or "Normal"


    def clear_collection_filters(self):
        self.collection_filter.set("")
        for var in self.identity_vars.values():
            var.set(False)
        self.identity_colorless.set(False)
        self.identity_exact.set(False)
        self.type_filter.set("Todos")
        self.set_filter.set("Todas")
        self.lang_filter.set("Todos")
        self.finish_filter.set("Todos")
        self.subtype_filter.set("")
        self.availability_filter.set("Todas")
        self.refresh_collection()

    def _identity_color_changed(self):
        # Si se marca un color, deja de buscar exclusivamente cartas incoloras.
        if any(var.get() for var in self.identity_vars.values()):
            self.identity_colorless.set(False)
        self.refresh_collection()

    def _identity_colorless_changed(self):
        # Incolora es una identidad exclusiva; al marcarla se limpian W/U/B/R/G.
        if self.identity_colorless.get():
            for var in self.identity_vars.values():
                var.set(False)
        self.refresh_collection()

    def _identity_matches(self, identity):
        actual = set(self._parse_color_codes(identity))
        selected = {c for c, var in self.identity_vars.items() if var.get()}

        if self.identity_colorless.get():
            return not actual
        if not selected:
            return True
        if self.identity_exact.get():
            return actual == selected
        return selected.issubset(actual)

    def _type_matches(self, type_line, selected):
        if selected == "Todos": return True
        tl = (type_line or "").lower()
        terms = {
            "Criatura":["creature","criatura"], "Artefacto":["artifact","artefacto"],
            "Encantamiento":["enchantment","encantamiento"], "Instantáneo":["instant","instantáneo","instantaneo"],
            "Conjuro":["sorcery","conjuro"], "Planeswalker":["planeswalker"], "Tierra":["land","tierra"],
            "Batalla":["battle","batalla"]
        }
        return any(x in tl for x in terms.get(selected, []))

    def _row_value(self, row, key, default=None):
        try:
            return row[key]
        except Exception:
            return default

    def _rarity_name(self, rarity):
        return {
            "common":"Común", "uncommon":"Infrecuente", "rare":"Rara",
            "mythic":"Mítica", "special":"Especial", "bonus":"Bonus"
        }.get((rarity or "").lower(), rarity or "—")

    def _cached_scry_card(self, row):
        key = ((self._row_value(row, "set_code", "") or "").lower(),
               str(self._row_value(row, "collector_number", "") or ""))
        return self._scry_detail_cache.get(key)

    def _row_rarity(self, row):
        rarity = self._row_value(row, "rarity")
        if not rarity:
            live = self._cached_scry_card(row)
            rarity = live.get("rarity") if live else None
        return self._rarity_name(rarity)

    def sort_collection(self, col):
        current, rev = self._collection_sort
        self._collection_sort = (col, not rev if current == col else False)
        self.refresh_collection()

    def _parse_color_codes(self, value):
        """Normaliza colores MTG desde W/U/B/R/G, listas o texto serializado."""
        raw = str(value or "").upper()
        found = set(re.findall(r"[WUBRG]", raw))
        return [c for c in "WUBRG" if c in found]

    def _clear_collection_color_chips(self):
        for widget in getattr(self, "_collection_color_overlays", []):
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self._collection_color_overlays = []

    def _make_color_chip_canvas(self, iid, column, codes):
        bbox = self.collection_tree.bbox(iid, column)
        if not bbox:
            return
        x, y, w, h = bbox
        if w <= 2 or h <= 2:
            return
        bg = UI["surface"]
        canvas = tk.Canvas(self.collection_tree, width=w, height=h, bg=bg,
                           highlightthickness=0, bd=0, takefocus=0)
        canvas.place(x=x, y=y, width=w, height=h)

        palette = {
            "W": ("#f4efd7", "#b7ad8c"),
            "U": ("#2f7ed8", "#1e5fa8"),
            "B": ("#1f1f1f", "#000000"),
            "R": ("#d94a3a", "#a92f24"),
            "G": ("#3f9b57", "#28723d"),
        }
        if codes:
            radius = max(5, min(8, (h - 6) // 2))
            gap = 5
            total = len(codes) * (2 * radius) + (len(codes) - 1) * gap
            cx = max(radius + 4, (w - total) // 2 + radius)
            cy = h // 2
            for code in codes:
                fill, outline = palette[code]
                canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                                   fill=fill, outline=outline, width=1)
                cx += 2 * radius + gap
        else:
            # Incoloro: rombo neutro, sin letra.
            r = max(5, min(7, (h - 8) // 2))
            cx, cy = w // 2, h // 2
            canvas.create_polygon(cx, cy-r, cx+r, cy, cx, cy+r, cx-r, cy,
                                  fill="#dddddd", outline="#999999")

        def _select(_event=None):
            self.collection_tree.selection_set(iid)
            self.collection_tree.focus(iid)
        canvas.bind("<Button-1>", _select)
        canvas.bind("<Double-1>", lambda e: (self.collection_tree.selection_set(iid),
                                              self.show_selected_card_detail(self.collection_tree, private=True)))
        self._collection_color_overlays.append(canvas)

    def _make_mana_cost_canvas(self, iid, column, mana_cost):
        bbox = self.collection_tree.bbox(iid, column)
        if not bbox:
            return
        x, y, w, h = bbox
        if w <= 2 or h <= 2:
            return
        canvas = tk.Canvas(self.collection_tree, width=w, height=h, bg=UI["surface"],
                           highlightthickness=0, bd=0, takefocus=0)
        canvas.place(x=x, y=y, width=w, height=h)
        tokens = self._mana_tokens(mana_cost)
        if not tokens:
            canvas.create_text(8, h//2, text="—", anchor="w", fill=UI["muted"], font=("Arial", 9))
        else:
            size=max(16,min(22,h-6))
            gap=2
            cx=5
            cy=h//2
            for token in tokens:
                photo=self._get_real_mana_photo(token,size)
                if photo is not None:
                    canvas.create_image(cx,cy,image=photo,anchor="w")
                    canvas._mana_images=getattr(canvas,"_mana_images",[])+[photo]
                else:
                    canvas.create_text(cx,cy,text="{"+token+"}",anchor="w",
                                       fill=UI["muted"],font=("Arial",8))
                cx += size + gap

        def _select(_event=None):
            self.collection_tree.selection_set(iid)
            self.collection_tree.focus(iid)
        canvas.bind("<Button-1>", _select)
        canvas.bind("<Double-1>", lambda e: (self.collection_tree.selection_set(iid),
                                              self.show_selected_card_detail(self.collection_tree, private=True)))
        self._collection_color_overlays.append(canvas)

    def _redraw_collection_color_chips(self):
        if not hasattr(self, "collection_tree"):
            return
        self._clear_collection_color_chips()
        values = getattr(self, "_collection_color_values", {})
        for iid in self.collection_tree.get_children(""):
            pair = values.get(str(iid))
            if not pair:
                continue
            mana_cost, identity = pair
            self._make_mana_cost_canvas(iid, "mana", mana_cost)
            self._make_color_chip_canvas(iid, "identity", identity)


    def refresh_collection(self):
        if not hasattr(self, "collection_tree"): return
        self._clear_collection_color_chips()
        self._collection_color_values = {}
        for i in self.collection_tree.get_children(): self.collection_tree.delete(i)

        rows = list(self.db.list_collection(""))
        editions = sorted({r["set_code"].upper() for r in rows if r["set_code"]})
        self.set_box["values"] = ["Todas"] + editions
        if self.set_filter.get() not in self.set_box["values"]:
            self.set_filter.set("Todas")

        q = self.collection_filter.get().strip().lower()
        out = []
        for r in rows:
            hay = " ".join(str(r[k] or "") for k in ["name","set_code","set_name","collector_number","type_line"]).lower()
            if q and q not in hay: continue
            if not self._identity_matches(r["color_identity"]): continue
            if not self._type_matches(r["type_line"], self.type_filter.get()): continue
            subtype_q = self.subtype_filter.get().strip().lower()
            if subtype_q and subtype_q not in (r["type_line"] or "").lower(): continue
            uses = self.db.card_usage(r["id"])
            used_qty = sum(u["quantity"] for u in uses)
            free_qty = max(0, r["quantity"] - used_qty)
            avail = self.availability_filter.get()
            if avail == "Disponibles" and free_qty <= 0: continue
            if avail == "En mazo" and used_qty <= 0: continue
            if avail == "Sin disponibles" and free_qty > 0: continue
            if self.set_filter.get() != "Todas" and r["set_code"].upper() != self.set_filter.get(): continue
            if self.lang_filter.get() != "Todos" and self._lang_name(r["lang"]) != self.lang_filter.get(): continue
            if self.finish_filter.get() != "Todos" and self._finish_name(r["finish"]) != self.finish_filter.get(): continue
            out.append(r)

        col, rev = self._collection_sort
        def key(r):
            m = {
                "qty": r["quantity"], "name": (r["name"] or "").lower(), "set": (r["set_code"] or "").lower(),
                "number": str(r["collector_number"] or ""), "lang": r["lang"], "finish": r["finish"], "treatment": (r["treatment"] or "").lower(),
                "type": (r["type_line"] or "").lower(), "mana": float(self._row_value(r, "mana_value", 0) or 0), "identity": self._row_value(r, "color_identity", ""),
                "rarity": self._row_rarity(r).lower(), "assigned": self._assignment_text(r).lower(), "commander": r["commander_legal"]
            }
            return m.get(col, (r["name"] or "").lower())
        out.sort(key=key, reverse=rev)

        for r in out:
            iid = str(r["id"])
            self._collection_color_values[iid] = (
                self._row_value(r, "mana_cost", "") or "",
                self._parse_color_codes(self._row_value(r, "color_identity", "")),
            )
            self.collection_tree.insert("", "end", iid=iid,
                values=(r["quantity"], r["name"], "", "",
                        r["type_line"], self._row_rarity(r), r["set_code"].upper(), r["collector_number"], self._lang_name(r["lang"]),
                        self._finish_name(r["finish"]), self._treatment_name(r["treatment"]), self._assignment_text(r),
                        "Sí" if r["commander_legal"] else "No"))
        total_cards = sum(r["quantity"] for r in rows)
        self.collection_summary.config(text=f"{len(rows)} impresiones · {total_cards} cartas físicas · mostrando {len(out)}")
        self.after_idle(self._redraw_collection_color_chips)
        self.refresh_public_collection()
        if hasattr(self, "analytics_labels"):
            self._refresh_analytics()

    def refresh_public_collection(self):
        if not hasattr(self, "public_tree"): return
        for i in self.public_tree.get_children(): self.public_tree.delete(i)
        q = self.public_filter.get().strip().lower() if hasattr(self, "public_filter") else ""
        rows = self.db.available_collection()
        shown = 0
        total_free = 0
        for r in rows:
            hay = " ".join([r["name"] or "", r["set_code"] or "", r["collector_number"] or "", r["type_line"] or ""]).lower()
            if q and q not in hay: continue
            shown += 1
            total_free += r["free_qty"]
            self.public_tree.insert("", "end", values=(
                r["free_qty"], r["name"], r["set_code"].upper(), r["collector_number"],
                self._lang_name(r["lang"]), self._finish_name(r["finish"]), self._treatment_name(r["treatment"]), r["type_line"]
            ))
        self.public_summary.config(text=f"{shown} impresiones disponibles · {total_free} cartas físicas libres")

    def copy_public_list(self):
        rows = self.db.available_collection()
        lines = [
            f"{r['free_qty']} {r['name']} ({r['set_code'].upper()}) {r['collector_number']} · {self._lang_name(r['lang'])} · {self._finish_name(r['finish'])} · {self._treatment_name(r['treatment'])}"
            for r in rows
        ]
        text = "\n".join(lines)
        self.clipboard_clear(); self.clipboard_append(text); self.update()
        self.status_var.set("Lista externa copiada al portapapeles.")

    def remove_one_selected(self):
        sel = self.collection_tree.selection()
        if not sel: return
        self.db.remove_one(int(sel[0]))
        self.refresh_collection()
        self.refresh_deck_cards()

    def edit_selected_card(self):
        sel = self.collection_tree.selection()
        if not sel:
            messagebox.showinfo("Editar carta", "Selecciona primero una carta de la colección.")
            return
        cid = int(sel[0])
        card = self.db.get_collection_card(cid)
        if not card:
            return

        usage = self.db.card_usage(cid)
        used = sum(u["quantity"] for u in usage)
        current_qty = int(card["quantity"] or 0)

        win = tk.Toplevel(self)
        win.title(f"Editar — {card['name']}")
        win.geometry("440x300")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        box = ttk.Frame(win)
        box.pack(fill="both", expand=True, padx=16, pady=16)
        ttk.Label(box, text=card["name"], font=("Arial", 14, "bold")).grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,12))
        qty_var = tk.IntVar(value=current_qty)
        lang_var = tk.StringVar(value=self._lang_name(card["lang"]))
        finish_var = tk.StringVar(value=self._finish_name(card["finish"]))

        ttk.Label(box, text="Cantidad:").grid(row=1,column=0,sticky="w",pady=6)
        ttk.Spinbox(box, from_=max(used,1), to=999, textvariable=qty_var, width=10).grid(row=1,column=1,sticky="w",pady=6)
        ttk.Label(box, text="Idioma:").grid(row=2,column=0,sticky="w",pady=6)
        lang_box = ttk.Combobox(box, textvariable=lang_var, state="readonly", width=18,
            values=["Inglés","Español","Portugués","Francés","Italiano","Alemán","Japonés","Otro"])
        lang_box.grid(row=2,column=1,sticky="w",pady=6)
        ttk.Label(box, text="Acabado:").grid(row=3,column=0,sticky="w",pady=6)
        finish_box = ttk.Combobox(box, textvariable=finish_var, state="readonly", width=18,
            values=["Normal","Foil","Etched"])
        finish_box.grid(row=3,column=1,sticky="w",pady=6)

        if used:
            lang_box.configure(state="disabled")
            finish_box.configure(state="disabled")
            ttk.Label(box, text=f"{used} copia(s) están en mazos. Idioma y acabado se bloquean para proteger esas asignaciones.",
                      style="Sub.TLabel", wraplength=390).grid(row=4,column=0,columnspan=2,sticky="w",pady=(8,4))
        else:
            ttk.Label(box, text="Puedes corregir cantidad, idioma y acabado.", style="Sub.TLabel").grid(
                row=4,column=0,columnspan=2,sticky="w",pady=(8,4))

        def save():
            try:
                new_qty = max(1, int(qty_var.get()))
            except Exception:
                messagebox.showerror("Cantidad", "Escribe una cantidad válida.", parent=win)
                return
            if new_qty < used:
                messagebox.showerror("Cantidad", f"No puedes bajar de {used}: esas copias están asignadas a mazos.", parent=win)
                return

            inv_lang = {"Inglés":"en","Español":"es","Portugués":"pt","Francés":"fr",
                        "Italiano":"it","Alemán":"de","Japonés":"ja","Otro":"und"}
            inv_finish = {"Normal":"nonfoil","Foil":"foil","Etched":"etched"}
            new_lang = inv_lang.get(lang_var.get(), card["lang"])
            new_finish = inv_finish.get(finish_var.get(), card["finish"])
            metadata_change = (new_lang != card["lang"] or new_finish != card["finish"])
            if metadata_change and used:
                messagebox.showerror("En uso", "No se puede cambiar idioma o acabado mientras haya copias en mazos.", parent=win)
                return

            try:
                # Guardamos el estado anterior para que los cambios de cantidad puedan deshacerse.
                undo_state = self._capture_undo_state([cid]) if (new_qty != current_qty and not metadata_change) else None
                live = self.scry.get_by_set_number((card["set_code"] or "").lower(), str(card["collector_number"] or ""))
                # Si cambia metadata sin cartas asignadas, reconstruimos esta impresión de forma controlada.
                if metadata_change:
                    for _ in range(current_qty):
                        self.db.remove_one(cid)
                    try:
                        self.db.add_card(live, new_qty, new_lang, new_finish, card["treatment"] or self._detect_treatment(live))
                    except Exception:
                        # Mejor esfuerzo de restauración si algo excepcional falla.
                        self.db.add_card(live, current_qty, card["lang"], card["finish"], card["treatment"] or self._detect_treatment(live))
                        raise
                else:
                    if new_qty < current_qty:
                        for _ in range(current_qty - new_qty):
                            self.db.remove_one(cid)
                    elif new_qty > current_qty:
                        self.db.add_card(live, new_qty-current_qty, card["lang"], card["finish"], card["treatment"] or self._detect_treatment(live))
                if undo_state:
                    self._push_undo(f"Cambiar cantidad de {card['name']}: {current_qty} → {new_qty}", undo_state)
                win.destroy()
                self.refresh_collection()
                self.refresh_deck_cards()
                if not undo_state:
                    self.status_var.set(f"Actualizada: {card['name']}")
            except Exception as e:
                messagebox.showerror("No se pudo editar", str(e), parent=win)

        btns = ttk.Frame(box)
        btns.grid(row=5,column=0,columnspan=2,sticky="e",pady=(14,0))
        ttk.Button(btns, text="Cancelar", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Guardar cambios", command=save).pack(side="right", padx=6)

    def export_collection(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")],
                                            initialfile="mi_coleccion_mtg.csv")
        if path:
            self.db.export_collection_csv(path)
            messagebox.showinfo("Exportado","Colección exportada.")


    def move_selected_copy(self):
        sel=self.collection_tree.selection()
        if not sel:
            messagebox.showinfo("Mover copia","Selecciona primero una impresión de carta."); return
        cid=int(sel[0]); card=self.db.get_collection_card(cid); uses=self.db.card_usage(cid); decks=self.db.list_decks()
        if not decks:messagebox.showinfo("Sin mazos","No hay mazos creados."); return
        source_choices=["DISPONIBLE"]+[u["deck_name"] for u in uses]
        source_text="\\n".join(f"{i+1}. {x}" for i,x in enumerate(source_choices))
        s=simpledialog.askinteger("Mover copia",f"{card['name']} · {card['set_code'].upper()} {card['collector_number']}\\n\\n¿Desde dónde?\\n{source_text}",minvalue=1,maxvalue=len(source_choices))
        if not s:return
        source=source_choices[s-1]
        target_choices=["DISPONIBLE"]+[d["name"] for d in decks if d["name"]!=source]
        target_text="\\n".join(f"{i+1}. {x}" for i,x in enumerate(target_choices))
        t=simpledialog.askinteger("Mover copia",f"¿A dónde moverla?\\n{target_text}",minvalue=1,maxvalue=len(target_choices))
        if not t:return
        target=target_choices[t-1]
        if source==target:return
        state=self._capture_undo_state([cid])
        try:
            self.db.move_one_copy(cid,source,target)
            self._push_undo(f"Mover {card['name']}: {source} → {target}",state)
            self.refresh_collection(); self.refresh_deck_cards()
        except Exception as e:messagebox.showerror("No se pudo mover",str(e))

    def show_movement_history(self):
        rows = self.db.movement_history(limit=200)
        win = tk.Toplevel(self)
        win.title("Historial de movimientos")
        win.geometry("760x480")
        txt = tk.Text(win, wrap="word", font=("Consolas", 9), bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF", relief="flat")
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        if not rows:
            txt.insert("1.0", "Todavía no hay movimientos registrados.")
        else:
            for r in rows:
                txt.insert("end", f"{r['moved_at']} · {r['card_name']} {r['set_code'].upper()} {r['collector_number']} · {r['from_name']} → {r['to_name']}\n")
        txt.configure(state="disabled")




    def _mana_tokens(self, mana_cost):
        if not mana_cost:
            return []
        return re.findall(r"\{([^}]+)\}", mana_cost)

    def _mana_symbol_filename(self, token):
        # Scryfall's CDN uses compact symbol names: W/U -> WU, W/P -> WP, 2/U -> 2U.
        return re.sub(r"[^A-Za-z0-9]+", "", str(token).upper()) or "UNKNOWN"

    def _mana_symbol_svg_url(self, token):
        name = self._mana_symbol_filename(token)
        return f"https://svgs.scryfall.io/card-symbols/{name}.svg"

    def _mana_symbol_png_path(self, token, size):
        name = self._mana_symbol_filename(token)
        return self._mana_symbol_dir / f"{name}_{int(size)}.png"

    def _load_mana_photo_from_disk(self, token, size):
        if Image is None or ImageTk is None:
            return None
        path = self._mana_symbol_png_path(token, size)
        if not path.exists():
            return None
        try:
            img = Image.open(path).convert("RGBA")
            if img.size != (size, size):
                img = img.resize((size, size), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _schedule_real_mana_redraw(self):
        if self._mana_redraw_scheduled:
            return
        self._mana_redraw_scheduled = True
        def redraw():
            self._mana_redraw_scheduled = False
            try:
                if hasattr(self, "collection_tree"):
                    self._redraw_collection_color_chips()
            except Exception:
                pass
            try:
                if hasattr(self, "deck_tree"):
                    self._redraw_deck_mana_costs()
            except Exception:
                pass
        self.after(100, redraw)

    def _download_real_mana_symbol(self, token, size):
        key=(str(token).upper(), int(size))
        try:
            if resvg_py is None:
                return
            req=urllib.request.Request(
                self._mana_symbol_svg_url(token),
                headers={
                    "User-Agent":"MTG Organizer/3.2",
                    "Accept":"image/svg+xml,*/*;q=0.8",
                }
            )
            with urllib.request.urlopen(req, timeout=12) as response:
                svg=response.read().decode("utf-8")
            png=resvg_py.svg_to_bytes(
                svg_string=svg,
                width=int(size),
                height=int(size),
                shape_rendering="geometric_precision",
                image_rendering="optimize_quality",
            )
            path=self._mana_symbol_png_path(token,size)
            path.write_bytes(png)
            def ready():
                photo=self._load_mana_photo_from_disk(token,size)
                if photo is not None:
                    self._mana_photo_cache[key]=photo
                self._mana_symbol_pending.discard(key)
                self._schedule_real_mana_redraw()
            self.after(0,ready)
        except Exception:
            def failed():
                self._mana_symbol_pending.discard(key)
            try:
                self.after(0,failed)
            except Exception:
                pass

    def _get_real_mana_photo(self, token, size=20):
        key=(str(token).upper(), int(size))
        photo=self._mana_photo_cache.get(key)
        if photo is not None:
            return photo
        photo=self._load_mana_photo_from_disk(token,size)
        if photo is not None:
            self._mana_photo_cache[key]=photo
            return photo
        if resvg_py is not None and key not in self._mana_symbol_pending:
            self._mana_symbol_pending.add(key)
            threading.Thread(
                target=self._download_real_mana_symbol,
                args=(token,int(size)),
                daemon=True
            ).start()
        return None

    def _mana_symbol_colors(self, token):
        """Paleta inspirada en los símbolos impresos de MTG, sin depender de internet."""
        t = token.upper()
        palette = {
            "W": ("#F5EED1", "#22211C"),
            "U": ("#72A9C8", "#0F2430"),
            "B": ("#4B474D", "#FFFFFF"),
            "R": ("#C96C5E", "#2B1110"),
            "G": ("#6FA57B", "#10261A"),
            "C": ("#B9BEC2", "#202428"),
            "S": ("#DCEAF2", "#26343B"),
        }
        if t in palette:
            return palette[t]
        if "/" in t:
            parts=t.split("/")
            first=parts[0] if parts else ""
            return palette.get(first, ("#D8C78F", "#241F12"))
        return "#D1D3D5", "#202124"

    def _mana_symbol_text(self, token):
        """Texto limpio para discos de maná: evita pictogramas ajenos a MTG."""
        t = token.upper()
        # Conservamos la notación reconocible de Magic dentro del disco.
        # Ej.: W U B R G, 1 2 X, C, W/U, 2/R, G/P.
        return t

    def _draw_mana_symbols(self, parent, mana_cost):
        frame = tk.Frame(parent, bg=UI["surface"])
        tokens = self._mana_tokens(mana_cost)
        if not tokens:
            ttk.Label(frame, text="—").pack(side="left")
            return frame

        for tok in tokens:
            photo=self._get_real_mana_photo(tok,24)
            if photo is not None:
                lbl=tk.Label(frame,image=photo,bg=UI["surface"],bd=0,padx=0,pady=0)
                lbl.image=photo
            else:
                # Solo mientras se descarga el SVG real por primera vez.
                lbl=tk.Label(frame,text="{"+tok+"}",bg=UI["surface"],fg=UI["muted"],
                             font=("Arial",9),bd=0,padx=1,pady=0)
            lbl.pack(side="left",padx=1)
        return frame

    def _scry_image_url(self, live_card):
        image_uris = live_card.get("image_uris") or {}
        if image_uris.get("png"):
            return image_uris["png"]
        for face in live_card.get("card_faces") or []:
            face_uris = face.get("image_uris") or {}
            if face_uris.get("png"):
                return face_uris["png"]
        return None

    def _card_detail_window(self, card):
        win = tk.Toplevel(self)
        win.title(card["name"])
        win.geometry("940x650")
        win.minsize(760, 540)
        win.transient(self)

        outer = ttk.Frame(win)
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        ttk.Label(outer, text=card["name"], font=("Arial", 18, "bold")).pack(anchor="w")
        subtitle = (
            f"{(card['set_name'] or '')} · {(card['set_code'] or '').upper()} "
            f"{card['collector_number'] or ''} · {self._lang_name(card['lang'])} · "
            f"{self._finish_name(card['finish'])} · {self._treatment_name(card['treatment'])}"
        )
        ttk.Label(outer, text=subtitle, style="Sub.TLabel").pack(anchor="w", pady=(2,12))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)

        # Izquierda: imagen online de Scryfall. No se guarda en disco.
        image_box = ttk.LabelFrame(body, text="Carta")
        image_box.pack(side="left", fill="y", padx=(0,14))
        image_label = ttk.Label(image_box, text="Cargando imagen…", anchor="center", width=31)
        image_label.pack(fill="both", expand=True, padx=10, pady=10)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        info = ttk.Frame(right)
        info.pack(fill="x", pady=(0,5))

        cost_box = ttk.Frame(info)
        cost_box.pack(side="left", padx=(0,18))
        ttk.Label(cost_box, text="Coste:").pack(side="left", padx=(0,5))
        self._draw_mana_symbols(cost_box, card["mana_cost"] or "").pack(side="left")

        ttk.Label(info, text=f"Valor de maná: {card['mana_value'] or 0}").pack(side="left", padx=(0,18))

        ident_box = ttk.Frame(info)
        ident_box.pack(side="left")
        ttk.Label(ident_box, text="Identidad:").pack(side="left", padx=(0,5))
        identity_cost = "".join("{" + c + "}" for c in (card["color_identity"] or "").split())
        if not identity_cost:
            identity_cost = "{C}"
        self._draw_mana_symbols(ident_box, identity_cost).pack(side="left")

        meta = ttk.Frame(right)
        meta.pack(fill="x", pady=(0,8))
        ttk.Label(meta, text=card["type_line"] or "", font=("Arial", 10, "bold")).pack(side="left")
        rarity_label = ttk.Label(meta, text=f"Rareza: {self._row_rarity(card)}", style="Sub.TLabel")
        rarity_label.pack(side="right")

        box = ttk.LabelFrame(right, text="Texto de la carta")
        box.pack(fill="both", expand=True)
        txt = tk.Text(box, wrap="word", font=("Arial", 11), bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF",
                      relief="flat", padx=14, pady=14)
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        oracle = card["oracle_text"] or "Esta impresión no tiene texto Oracle guardado."
        txt.insert("1.0", oracle)
        txt.configure(state="disabled")

        usage = self.db.card_usage(card["id"])
        used = sum(u["quantity"] for u in usage)
        free = max(0, card["quantity"] - used)
        usage_text = f"Tienes {card['quantity']} copia(s) · {free} disponible(s)"
        if usage:
            usage_text += " · " + ", ".join(f"{u['deck_name']} ×{u['quantity']}" for u in usage)
        ttk.Label(right, text=usage_text, style="Sub.TLabel", wraplength=560).pack(anchor="w", pady=(10,0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10,0))
        if str(card.get("id", "") if hasattr(card, "get") else card["id"]).isdigit():
            ttk.Button(buttons, text="Editar", command=lambda: (win.destroy(), self._select_and_edit_card(card["id"]))).pack(side="left")
        ttk.Button(buttons, text="Cerrar", command=win.destroy).pack(side="right")

        def worker():
            try:
                key = ((card["set_code"] or "").lower(), str(card["collector_number"] or ""))
                live = self._scry_detail_cache.get(key)
                if live is None:
                    live = self.scry.get_by_set_number(key[0], key[1])
                    self._scry_detail_cache[key] = live
                image_url = self._scry_image_url(live)
                if not image_url:
                    raise RuntimeError("Esta impresión no tiene imagen disponible.")
                req = urllib.request.Request(image_url, headers={"User-Agent":"MTG Organizer/1.9.5a"})
                with urllib.request.urlopen(req, timeout=12) as response:
                    raw = response.read()
                encoded = base64.b64encode(raw)

                def apply_image():
                    if not win.winfo_exists():
                        return
                    try:
                        img = tk.PhotoImage(data=encoded)
                        # Scryfall PNG suele ser grande; reducimos solo para visualizar.
                        factor = max(1, int(max(img.width()/250, img.height()/350)))
                        if factor > 1:
                            img = img.subsample(factor, factor)
                        image_label.configure(image=img, text="")
                        image_label.image = img
                        rarity_label.configure(text=f"Rareza: {self._rarity_name(live.get('rarity'))}")
                        self.after_idle(self.refresh_collection)
                    except Exception:
                        image_label.configure(text="Imagen no disponible")
                self.after(0, apply_image)
            except Exception:
                def fail():
                    if win.winfo_exists():
                        image_label.configure(text="Imagen no disponible\nLa ficha sigue funcionando sin internet.")
                self.after(0, fail)

        threading.Thread(target=worker, daemon=True).start()

    def _select_and_edit_card(self, card_id):
        if hasattr(self, "collection_tree") and self.collection_tree.exists(str(card_id)):
            self.collection_tree.selection_set(str(card_id))
            self.collection_tree.focus(str(card_id))
            self.edit_selected_card()

    def show_selected_card_detail(self, tree, private=True):
        sel = tree.selection()
        if not sel:
            return
        if private:
            try:
                cid = int(sel[0])
            except Exception:
                return
            card = self.db.get_collection_card(cid)
        else:
            vals = tree.item(sel[0], "values")
            if len(vals) < 4:
                return
            name, set_code, number = vals[1], vals[2], vals[3]
            card = self.db.find_collection_variant(name, set_code, number)
        if card:
            self._card_detail_window(card)

    def show_deck_card_detail(self):
        sel = self.deck_tree.selection()
        if not sel:
            return
        row = self.db.get_deck_card_collection(int(sel[0]))
        if row:
            self._card_detail_window(row)


    # ---------------- Deshacer y construcción desde colección ----------------
    def _capture_undo_state(self, collection_ids):
        ids=sorted({int(x) for x in collection_ids if x is not None})
        state={}
        with self.db.con() as c:
            for cid in ids:
                col=c.execute("SELECT id,quantity FROM collection WHERE id=?",(cid,)).fetchone()
                if not col:
                    state[cid]=None
                    continue
                decks=c.execute("SELECT deck_id,quantity,is_commander FROM deck_cards WHERE collection_id=? ORDER BY id",(cid,)).fetchall()
                state[cid]={"quantity":int(col["quantity"] or 0),
                            "deck_cards":[(int(r["deck_id"]),int(r["quantity"]),int(r["is_commander"])) for r in decks]}
        return state

    def _push_undo(self,label,state):
        if not state:return
        self._undo_stack.append({"label":label,"state":state})
        if len(self._undo_stack)>50:self._undo_stack=self._undo_stack[-50:]
        if hasattr(self,"undo_button"):
            self.undo_button.configure(state="normal",text=f"↶ Deshacer: {label[:26]}")
        self.status_var.set(f"{label} · Ctrl+Z para deshacer")

    def undo_last_action(self):
        if not self._undo_stack:
            self.status_var.set("No hay acciones para deshacer."); return
        action=self._undo_stack.pop()
        try:
            with self.db.con() as c:
                for cid,prior in action["state"].items():
                    if prior is None:
                        c.execute("DELETE FROM deck_cards WHERE collection_id=?",(cid,))
                        c.execute("DELETE FROM collection WHERE id=?",(cid,))
                        continue
                    if not c.execute("SELECT id FROM collection WHERE id=?",(cid,)).fetchone():
                        raise ValueError("No se puede restaurar una impresión eliminada completamente.")
                    c.execute("UPDATE collection SET quantity=? WHERE id=?",(prior["quantity"],cid))
                    c.execute("DELETE FROM deck_cards WHERE collection_id=?",(cid,))
                    for deck_id,qty,is_cmd in prior["deck_cards"]:
                        c.execute("INSERT INTO deck_cards(deck_id,collection_id,quantity,is_commander) VALUES (?,?,?,?)",
                                  (deck_id,cid,qty,is_cmd))
            self.refresh_collection(); self.refresh_deck_cards()
            if self._undo_stack:
                self.undo_button.configure(state="normal",text=f"↶ Deshacer: {self._undo_stack[-1]['label'][:26]}")
            else:
                self.undo_button.configure(state="disabled",text="↶ Deshacer")
            self.status_var.set(f"Deshecho: {action['label']}")
        except Exception as e:
            self._undo_stack.append(action)
            messagebox.showerror("Deshacer",str(e))

    def _show_collection_context_menu(self,event):
        iid=self.collection_tree.identify_row(event.y)
        if not iid:return
        self.collection_tree.selection_set(iid); self.collection_tree.focus(iid)
        menu=tk.Menu(self,tearoff=0)
        menu.add_command(label="Ver detalle",command=lambda:self.show_selected_card_detail(self.collection_tree,private=True))
        addmenu=tk.Menu(menu,tearoff=0)
        decks=list(self.db.list_decks())
        if decks:
            for d in decks:
                addmenu.add_command(label=d["name"],command=lambda did=d["id"]:self._collection_add_selected_to_deck(did,1))
        else:
            addmenu.add_command(label="No hay mazos creados",state="disabled")
        menu.add_cascade(label="Añadir a mazo",menu=addmenu)
        menu.add_command(label="Añadir varias a un mazo…",command=self._collection_add_many_dialog)
        menu.add_separator()
        menu.add_command(label="Mover copia…",command=self.move_selected_copy)
        menu.add_command(label="Editar impresión…",command=self.edit_selected_card)
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:
            try:menu.grab_release()
            except Exception:pass

    def _collection_add_selected_to_deck(self,did,qty=1):
        sel=self.collection_tree.selection()
        if not sel:return
        cid=int(sel[0]); row=self.db.get_collection_card(cid)
        if not row:return
        deck=self.db.get_deck(did)
        if self._deck_status(did)=="Desarmado":
            self._plan_add(did,cid,max(1,int(qty)),False)
            self.refresh_deck_cards(); self.status_var.set(f"{row['name']} añadido al plan de {deck['name']}.")
            return
        state=self._capture_undo_state([cid])
        try:
            result=self._assign_existing_copies(did,row,max(1,int(qty)),False)
            if result is False:return
            self._push_undo(f"{row['name']} → {deck['name']}",state)
            self.refresh_collection(); self.refresh_deck_cards()
        except Exception as e:messagebox.showerror("Añadir a mazo",str(e))

    def _collection_add_many_dialog(self):
        sel=self.collection_tree.selection()
        if not sel:return
        cid=int(sel[0]); row=self.db.get_collection_card(cid); decks=list(self.db.list_decks())
        if not decks:messagebox.showinfo("Sin mazos","Crea un mazo primero."); return
        win=tk.Toplevel(self); win.title(f"Añadir — {row['name']}"); win.resizable(False,False); win.transient(self); win.grab_set()
        box=ttk.Frame(win); box.pack(padx=14,pady=14)
        deck_var=tk.StringVar(value=decks[0]["name"]); qty_var=tk.IntVar(value=1)
        ttk.Label(box,text=row["name"],font=("Arial",12,"bold")).grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,10))
        ttk.Label(box,text="Mazo:").grid(row=1,column=0,sticky="w",pady=5)
        ttk.Combobox(box,textvariable=deck_var,state="readonly",width=28,values=[d["name"] for d in decks]).grid(row=1,column=1,pady=5)
        ttk.Label(box,text="Cantidad:").grid(row=2,column=0,sticky="w",pady=5)
        ttk.Spinbox(box,from_=1,to=99,textvariable=qty_var,width=8).grid(row=2,column=1,sticky="w",pady=5)
        ttk.Label(box,text=self._assignment_text(row),style="Sub.TLabel",wraplength=360).grid(row=3,column=0,columnspan=2,sticky="w",pady=(6,4))
        def go():
            did=next(d["id"] for d in decks if d["name"]==deck_var.get())
            win.destroy(); self._collection_add_selected_to_deck(did,qty_var.get())
        ttk.Button(box,text="Cancelar",command=win.destroy).grid(row=4,column=0,sticky="e",pady=(10,0))
        ttk.Button(box,text="Añadir",style="Accent.TButton",command=go).grid(row=4,column=1,sticky="e",pady=(10,0))


    # ---------------- Commander ----------------
    def _build_decks_tab(self):
        self._deck_outer = ttk.Panedwindow(self.tab_decks, orient="horizontal")
        self._deck_outer.pack(fill="both", expand=True, padx=14, pady=12)
        self._deck_left = ttk.LabelFrame(self._deck_outer, text="Mis mazos")
        right = ttk.Frame(self._deck_outer)
        self._deck_outer.add(self._deck_left, weight=1); self._deck_outer.add(right, weight=6)
        self._deck_sidebar_visible = True
        self.deck_list = tk.Listbox(self._deck_left, width=21, exportselection=False, relief="flat", font=("Arial",10))
        self.deck_list.pack(fill="both", expand=True, padx=5, pady=5)
        self.deck_list.bind("<<ListboxSelect>>", lambda e: self.on_deck_select())
        lbtn=ttk.Frame(self._deck_left); lbtn.pack(fill="x",padx=5,pady=(0,5))
        ttk.Button(lbtn,text="Nuevo",command=self.new_deck).pack(side="left")
        ttk.Button(lbtn,text="Eliminar",command=self.delete_deck).pack(side="left",padx=5)

        head=ttk.Frame(right); head.pack(fill="x",pady=(0,4))
        ttk.Button(head,text="◀",width=3,command=self._toggle_deck_sidebar).pack(side="left",padx=(0,6))
        self.deck_title=ttk.Label(head,text="Selecciona un mazo",font=("Arial",16,"bold")); self.deck_title.pack(side="left")
        self.deck_count=ttk.Label(head,text=""); self.deck_count.pack(side="left",padx=(10,6))
        self.deck_bracket_badge=ttk.Label(head,text="B—",font=("Arial",10,"bold")); self.deck_bracket_badge.pack(side="left",padx=(0,8))
        self.deck_analysis_badge=ttk.Label(head,text="",style="Sub.TLabel"); self.deck_analysis_badge.pack(side="left",padx=(0,10))
        ttk.Label(head,text="Estado:").pack(side="left",padx=(2,3))
        self.deck_status_var=tk.StringVar(value="Montado")
        self.deck_status_box=ttk.Combobox(head,textvariable=self.deck_status_var,state="readonly",width=13,
            values=["Montado","En construcción","Desarmado"])
        self.deck_status_box.pack(side="left",padx=(0,6))
        self.deck_status_box.bind("<<ComboboxSelected>>",self._set_deck_status)
        self.deck_tools_button=ttk.Menubutton(head,text="Herramientas ▾"); self.deck_tools_button.pack(side="right")
        self.deck_tools_menu=tk.Menu(self.deck_tools_button,tearoff=0); self.deck_tools_button["menu"]=self.deck_tools_menu
        self.deck_tools_menu.add_command(label="Añadir tierras básicas…",command=self._basic_land_dialog)
        self.deck_tools_menu.add_separator(); self.deck_tools_menu.add_command(label="Importar lista…",command=self._show_import_tab)
        self.deck_tools_menu.add_command(label="Copiar lista",command=self.copy_plain_deck); self.deck_tools_menu.add_command(label="Exportar lista…",command=self.export_plain_deck)
        self.deck_tools_menu.add_separator(); self.deck_tools_menu.add_command(label="Copiar Moxfield",command=self.copy_moxfield); self.deck_tools_menu.add_command(label="Exportar Moxfield…",command=self.export_moxfield)
        self.deck_tools_menu.add_separator(); self.deck_tools_menu.add_command(label="Lista faltante",command=self.show_missing)
        self.deck_tools_menu.add_separator()
        self.deck_tools_menu.add_command(label="Duplicar mazo…",command=self.duplicate_deck)
        self.deck_tools_menu.add_command(label="Crear respaldo",command=self.create_backup)
        self.deck_tools_menu.add_command(label="Restaurar respaldo…",command=self.restore_backup)
        self.deck_tools_menu.add_command(label="Exportar todo JSON…",command=self.export_all_json)

        add=ttk.LabelFrame(right,text="Añadir carta"); add.pack(fill="x",pady=(0,5))
        self.deck_set_code=tk.StringVar(); self.deck_collector_number=tk.StringVar(); self.deck_lang=tk.StringVar(value="Inglés")
        self.deck_finish=tk.StringVar(value="Normal"); self.deck_qty=tk.IntVar(value=1); self.as_commander=tk.BooleanVar(value=False)
        self.deck_keep_set=tk.BooleanVar(value=False); self.deck_card_query=tk.StringVar()
        ttk.Label(add,text="Edición").grid(row=0,column=0,padx=(7,3),pady=(5,2)); de1=ttk.Entry(add,textvariable=self.deck_set_code,width=8); de1.grid(row=0,column=1,padx=(0,7),pady=(5,2))
        ttk.Label(add,text="Nº").grid(row=0,column=2,padx=(2,3),pady=(5,2)); de2=ttk.Entry(add,textvariable=self.deck_collector_number,width=9); de2.grid(row=0,column=3,padx=(0,7),pady=(5,2))
        ttk.Label(add,text="Cant.").grid(row=0,column=4,padx=(2,3),pady=(5,2)); ttk.Spinbox(add,from_=1,to=99,textvariable=self.deck_qty,width=4).grid(row=0,column=5,padx=(0,7),pady=(5,2))
        ttk.Label(add,text="Idioma").grid(row=0,column=6,padx=(2,3),pady=(5,2)); ttk.Combobox(add,textvariable=self.deck_lang,state="readonly",width=9,values=["Inglés","Español","Portugués","Francés","Italiano","Alemán","Japonés","Otro"]).grid(row=0,column=7,padx=(0,7),pady=(5,2))
        ttk.Label(add,text="Acabado").grid(row=0,column=8,padx=(2,3),pady=(5,2)); ttk.Combobox(add,textvariable=self.deck_finish,state="readonly",width=8,values=["Normal","Foil","Etched"]).grid(row=0,column=9,padx=(0,7),pady=(5,2))
        ttk.Button(add,text="Agregar",style="Accent.TButton",command=self.add_direct_to_deck).grid(row=0,column=10,padx=(3,7),pady=(5,2))
        ttk.Checkbutton(add,text="Comandante",variable=self.as_commander).grid(row=1,column=0,columnspan=2,padx=(7,3),pady=(2,5),sticky="w")
        ttk.Checkbutton(add,text="Mantener edición",variable=self.deck_keep_set).grid(row=1,column=2,columnspan=2,padx=(2,6),pady=(2,5),sticky="w")
        ttk.Label(add,text="Buscar").grid(row=1,column=4,padx=(3,3),pady=(2,5)); ce=ttk.Entry(add,textvariable=self.deck_card_query); ce.grid(row=1,column=5,columnspan=5,padx=(0,6),pady=(2,5),sticky="ew")
        ttk.Button(add,text="Añadir desde colección",command=self.add_to_deck).grid(row=1,column=10,padx=(3,7),pady=(2,5)); add.columnconfigure(8,weight=1)
        self.deck_set_entry=de1; self.deck_number_entry=de2; de1.bind("<Return>",lambda e:de2.focus_set()); de2.bind("<Return>",lambda e:self.add_direct_to_deck()); ce.bind("<Return>",lambda e:self.add_to_deck())

        self.deck_notebook=ttk.Notebook(right); self.deck_notebook.pack(fill="both",expand=True)
        list_tab=ttk.Frame(self.deck_notebook); builder_tab=ttk.Frame(self.deck_notebook); considering_tab=ttk.Frame(self.deck_notebook); stats_tab=ttk.Frame(self.deck_notebook)
        bracket_tab=ttk.Frame(self.deck_notebook); validation_tab=ttk.Frame(self.deck_notebook); notes_tab=ttk.Frame(self.deck_notebook); import_tab=ttk.Frame(self.deck_notebook)
        self.deck_notebook.add(list_tab,text="Lista")
        self.deck_notebook.add(builder_tab,text="Constructor")
        self.deck_notebook.add(considering_tab,text="Considering")
        self.deck_notebook.add(stats_tab,text="Estadísticas")
        self.deck_notebook.add(bracket_tab,text="Bracket")
        self.deck_notebook.add(validation_tab,text="Validación")
        self.deck_notebook.add(notes_tab,text="Notas")
        self.deck_notebook.add(import_tab,text="Importar")
        self._deck_import_tab=import_tab

        # Constructor V3.2: construir el mazo navegando directamente por la colección física.
        builder_top=ttk.Frame(builder_tab)
        builder_top.pack(fill="x",padx=8,pady=(8,5))
        self.builder_query=tk.StringVar()
        self.builder_availability=tk.StringVar(value="Disponibles")
        self.builder_qty=tk.IntVar(value=1)
        ttk.Label(builder_top,text="Buscar en tu colección:").pack(side="left")
        builder_search=ttk.Entry(builder_top,textvariable=self.builder_query,width=34)
        builder_search.pack(side="left",fill="x",expand=True,padx=(6,10))
        ttk.Label(builder_top,text="Mostrar:").pack(side="left")
        builder_av=ttk.Combobox(
            builder_top,textvariable=self.builder_availability,state="readonly",width=14,
            values=["Disponibles","Todas","En otros mazos"]
        )
        builder_av.pack(side="left",padx=(5,8))
        ttk.Button(builder_top,text="Limpiar",command=self._clear_deck_builder).pack(side="left")

        builder_actions=ttk.Frame(builder_tab)
        builder_actions.pack(fill="x",padx=8,pady=(0,6))
        self.builder_summary=ttk.Label(builder_actions,text="",style="Sub.TLabel")
        self.builder_summary.pack(side="left",fill="x",expand=True)
        ttk.Label(builder_actions,text="Cantidad:").pack(side="left",padx=(8,3))
        ttk.Spinbox(builder_actions,from_=1,to=99,textvariable=self.builder_qty,width=5).pack(side="left",padx=(0,7))
        ttk.Button(builder_actions,text="+1 al mazo",style="Primary.TButton",command=lambda:self._builder_add_selected(1,False)).pack(side="left",padx=3)
        ttk.Button(builder_actions,text="Añadir cantidad",style="Accent.TButton",command=self._builder_add_quantity).pack(side="left",padx=3)
        ttk.Button(builder_actions,text="Marcar comandante",command=lambda:self._builder_add_selected(1,True)).pack(side="left",padx=(3,0))

        builder_frame=ttk.Frame(builder_tab)
        builder_frame.pack(fill="both",expand=True,padx=8,pady=(0,8))
        builder_cols=("free","owned","name","mana","set","lang","finish","type","usage")
        self.builder_tree=ttk.Treeview(builder_frame,columns=builder_cols,show="headings",selectmode="browse")
        builder_heads={
            "free":"Libres","owned":"Tienes","name":"Carta","mana":"Coste","set":"Ed.",
            "lang":"Idioma","finish":"Acabado","type":"Tipo","usage":"Uso actual"
        }
        builder_widths={"free":52,"owned":52,"name":230,"mana":92,"set":60,"lang":72,"finish":70,"type":205,"usage":230}
        for c in builder_cols:
            self.builder_tree.heading(c,text=builder_heads[c])
            self.builder_tree.column(c,width=builder_widths[c],anchor="w",stretch=(c in {"name","type","usage"}))
        builder_ys=ttk.Scrollbar(builder_frame,orient="vertical",command=self.builder_tree.yview)
        builder_xs=ttk.Scrollbar(builder_frame,orient="horizontal",command=self.builder_tree.xview)
        self.builder_tree.configure(yscrollcommand=builder_ys.set,xscrollcommand=builder_xs.set)
        self.builder_tree.grid(row=0,column=0,sticky="nsew")
        builder_ys.grid(row=0,column=1,sticky="ns")
        builder_xs.grid(row=1,column=0,sticky="ew")
        builder_frame.rowconfigure(0,weight=1); builder_frame.columnconfigure(0,weight=1)
        self.builder_tree.tag_configure("free",foreground=UI["text"])
        self.builder_tree.tag_configure("occupied",foreground=UI["muted"])
        self.builder_tree.bind("<Double-1>",lambda e:self._builder_add_selected(1,False))
        self.builder_tree.bind("<Return>",lambda e:self._builder_add_selected(1,False))
        self.builder_tree.bind("<<TreeviewSelect>>",lambda e:self._update_builder_summary())
        builder_search.bind("<KeyRelease>",lambda e:self._schedule_builder_refresh())
        builder_search.bind("<Return>",lambda e:self._builder_add_selected(1,False))
        builder_av.bind("<<ComboboxSelected>>",lambda e:self.refresh_deck_builder())
        self._builder_refresh_job=None

        filters=ttk.Frame(list_tab); filters.pack(fill="x",pady=(2,4))
        self.deck_filter=tk.StringVar(); self.deck_type_filter=tk.StringVar(value="Todos"); self.deck_identity_filter=tk.StringVar(value="Todos"); self.deck_set_filter=tk.StringVar(value="Todas"); self.deck_lang_filter=tk.StringVar(value="Todos"); self.deck_finish_filter=tk.StringVar(value="Todos")
        ttk.Label(filters,text="Buscar:").pack(side="left"); se=ttk.Entry(filters,textvariable=self.deck_filter,width=28); se.pack(side="left",fill="x",expand=True,padx=(4,8)); se.bind("<KeyRelease>",lambda e:self.refresh_deck_cards())
        ttk.Label(filters,text="Tipo:").pack(side="left"); tb=ttk.Combobox(filters,textvariable=self.deck_type_filter,state="readonly",width=10,values=["Todos","Criatura","Artefacto","Encantamiento","Instantáneo","Conjuro","Planeswalker","Tierra","Batalla"]); tb.pack(side="left",padx=(3,7))
        ttk.Label(filters,text="Ident.:").pack(side="left"); ib=ttk.Combobox(filters,textvariable=self.deck_identity_filter,state="readonly",width=8,values=["Todos","Blanco","Azul","Negro","Rojo","Verde","Incolora"]); ib.pack(side="left",padx=(3,7))
        ttk.Label(filters,text="Ed.:").pack(side="left"); self.deck_filter_set_box=ttk.Combobox(filters,textvariable=self.deck_set_filter,state="readonly",width=7); self.deck_filter_set_box.pack(side="left",padx=(3,7))
        ttk.Label(filters,text="Organizar:").pack(side="left")
        self.deck_sort_mode=tk.StringVar(value="Por tipo")
        sort_box=ttk.Combobox(filters,textvariable=self.deck_sort_mode,state="readonly",width=12,values=["Por tipo","Alfabético","Valor de maná"])
        sort_box.pack(side="left",padx=(3,7))
        ttk.Button(filters,text="Más filtros…",command=self._deck_more_filters).pack(side="left",padx=(0,5)); ttk.Button(filters,text="Limpiar",command=self.clear_deck_filters).pack(side="left")
        for box in (tb,ib,self.deck_filter_set_box): box.bind("<<ComboboxSelected>>",lambda e:self.refresh_deck_cards())
        sort_box.bind("<<ComboboxSelected>>",lambda e:self.refresh_deck_cards())

        cols=("cmd","qty","name","mana","set","lang","finish","treatment","type","owned","other")
        frame=ttk.Frame(list_tab); frame.pack(fill="both",expand=True); self.deck_tree=ttk.Treeview(frame,columns=cols,show="headings")
        heads={"cmd":"Cmd.","qty":"Cant.","name":"Carta","mana":"Coste","set":"Ed.","lang":"Idioma","finish":"Acabado","treatment":"Versión","type":"Tipo","owned":"Tienes","other":"Otros mazos"}; widths={"cmd":40,"qty":48,"name":225,"mana":90,"set":58,"lang":70,"finish":68,"treatment":105,"type":190,"owned":58,"other":78}
        for c in cols:self.deck_tree.heading(c,text=heads[c]); self.deck_tree.column(c,width=widths[c],anchor="w",stretch=(c in {"name","type","treatment"}))
        def _deck_yview(*args):
            self.deck_tree.yview(*args)
            self.after_idle(self._redraw_deck_mana_costs)
        def _deck_xview(*args):
            self.deck_tree.xview(*args)
            self.after_idle(self._redraw_deck_mana_costs)
        def _deck_yscroll_set(first,last):
            ys.set(first,last)
            self.after_idle(self._redraw_deck_mana_costs)
        def _deck_xscroll_set(first,last):
            xs.set(first,last)
            self.after_idle(self._redraw_deck_mana_costs)

        ys=ttk.Scrollbar(frame,orient="vertical",command=_deck_yview)
        xs=ttk.Scrollbar(frame,orient="horizontal",command=_deck_xview)
        self.deck_tree.configure(yscrollcommand=_deck_yscroll_set,xscrollcommand=_deck_xscroll_set)
        self.deck_tree.grid(row=0,column=0,sticky="nsew"); ys.grid(row=0,column=1,sticky="ns"); xs.grid(row=1,column=0,sticky="ew"); frame.rowconfigure(0,weight=1); frame.columnconfigure(0,weight=1)
        self.deck_tree.tag_configure("section",background="#303947",foreground="#F7F9FC",font=("Arial",10,"bold"))
        self.deck_tree.tag_configure("commander",background="#51451C",foreground="#FFF4B8",font=("Arial",10,"bold"))
        self.deck_tree.bind("<Double-1>",lambda e:self._deck_context_detail())
        self.deck_tree.bind("<Button-3>",self._show_deck_context_menu)
        self.deck_tree.bind("<Configure>",lambda e:self.after_idle(self._redraw_deck_mana_costs))
        self.deck_tree.bind("<MouseWheel>",lambda e:self.after_idle(self._redraw_deck_mana_costs),add="+")
        self.deck_tree.bind("<Button-4>",lambda e:self.after_idle(self._redraw_deck_mana_costs),add="+")
        self.deck_tree.bind("<Button-5>",lambda e:self.after_idle(self._redraw_deck_mana_costs),add="+")
        self.deck_tree.bind("<KeyRelease>",lambda e:self.after_idle(self._redraw_deck_mana_costs),add="+")
        self._deck_mana_values={}; self._deck_mana_overlays=[]; self._deck_display_rows={}
        self.deck_context_menu=tk.Menu(self,tearoff=0)
        self.deck_context_menu.add_command(label="Ver detalle / desglose",command=self._deck_context_detail)
        self.deck_context_menu.add_separator()
        self.deck_context_menu.add_command(label="+1 copia",command=lambda:self._deck_adjust_delta(1))
        self.deck_context_menu.add_command(label="−1 copia",command=lambda:self._deck_adjust_delta(-1))
        self.deck_context_menu.add_command(label="Cambiar cantidad…",command=self._deck_change_quantity)
        self.deck_context_menu.add_separator()
        self.deck_context_menu.add_command(label="Mover copias…",command=self._deck_move_copies)
        self.deck_context_menu.add_command(label="Devolver 1 a disponibles",command=self.remove_from_deck)
        # Considering: no reserva copias físicas.
        ctop=ttk.Frame(considering_tab); ctop.pack(fill="x",padx=8,pady=8)
        self.consider_query=tk.StringVar()
        ttk.Label(ctop,text="Carta:").pack(side="left")
        centry=ttk.Entry(ctop,textvariable=self.consider_query,width=32); centry.pack(side="left",fill="x",expand=True,padx=5)
        ttk.Button(ctop,text="Añadir",command=self.add_considering).pack(side="left")
        ccols=("qty","name","set","type")
        self.consider_tree=ttk.Treeview(considering_tab,columns=ccols,show="headings")
        for c,h,w in [("qty","Cant.",55),("name","Carta",260),("set","Ed.",65),("type","Tipo",300)]:
            self.consider_tree.heading(c,text=h); self.consider_tree.column(c,width=w,anchor="w")
        self.consider_tree.pack(fill="both",expand=True,padx=8,pady=(0,8))
        self.consider_tree.bind("<Button-3>",self._considering_menu)

        self.stats_text=tk.Text(stats_tab,wrap="word",font=("Consolas",10),bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF",relief="flat",padx=9,pady=9); self.stats_text.pack(fill="both",expand=True); self.stats_text.configure(state="disabled")

        btop=ttk.Frame(bracket_tab); btop.pack(fill="x",padx=8,pady=8)
        ttk.Label(btop,text="Bracket declarado:").pack(side="left")
        self.declared_bracket_var=tk.StringVar(value="Sin declarar")
        self.declared_bracket_box=ttk.Combobox(btop,textvariable=self.declared_bracket_var,state="readonly",width=15,
            values=["Sin declarar","B1","B2","B3","B4","B5"])
        self.declared_bracket_box.pack(side="left",padx=(4,10))
        self.cedh_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(btop,text="Intención cEDH",variable=self.cedh_var,command=self._save_bracket_settings).pack(side="left")
        self.chain_turns_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(btop,text="Planea encadenar turnos extra",variable=self.chain_turns_var,command=self._save_bracket_settings).pack(side="left",padx=10)
        self.declared_bracket_box.bind("<<ComboboxSelected>>",lambda e:self._save_bracket_settings())

        combo_box=ttk.LabelFrame(bracket_tab,text="Combos intencionados de 2 cartas")
        combo_box.pack(fill="x",padx=8,pady=(0,6))
        self.combo_card1=tk.StringVar(); self.combo_card2=tk.StringVar(); self.combo_early=tk.BooleanVar(value=False)
        self.combo_card1_box=ttk.Combobox(combo_box,textvariable=self.combo_card1,state="readonly",width=30)
        self.combo_card2_box=ttk.Combobox(combo_box,textvariable=self.combo_card2,state="readonly",width=30)
        self.combo_card1_box.grid(row=0,column=0,padx=5,pady=6); self.combo_card2_box.grid(row=0,column=1,padx=5,pady=6)
        ttk.Checkbutton(combo_box,text="Temprano / bajo coste (≈ turnos 1–6)",variable=self.combo_early).grid(row=0,column=2,padx=5)
        ttk.Button(combo_box,text="Añadir combo",command=self.add_declared_combo).grid(row=0,column=3,padx=5)
        ttk.Button(combo_box,text="Buscar en Commander Spellbook",command=self.open_commander_spellbook).grid(row=0,column=4,padx=5)
        self.combo_list=tk.Listbox(combo_box,height=4); self.combo_list.grid(row=1,column=0,columnspan=4,sticky="ew",padx=5,pady=(0,6))
        ttk.Button(combo_box,text="Eliminar seleccionado",command=self.remove_declared_combo).grid(row=1,column=4,padx=5,pady=(0,6))
        combo_box.columnconfigure(0,weight=1); combo_box.columnconfigure(1,weight=1)
        self.bracket_text=tk.Text(bracket_tab,wrap="word",font=("Arial",10),bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF",relief="flat",padx=9,pady=9)
        self.bracket_text.pack(fill="both",expand=True,padx=8,pady=(0,8)); self.bracket_text.configure(state="disabled")

        self.validation_text=tk.Text(validation_tab,wrap="word",font=("Arial",10),bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF",relief="flat",padx=9,pady=9); self.validation_text.pack(fill="both",expand=True); self.validation_text.configure(state="disabled")

        ntop=ttk.Frame(notes_tab); ntop.pack(fill="x",padx=8,pady=(8,4))
        ttk.Button(ntop,text="Guardar notas",style="Accent.TButton",command=self.save_deck_notes).pack(side="right")
        self.deck_notes=tk.Text(notes_tab,wrap="word",font=("Arial",10),bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF",relief="solid",bd=1)
        self.deck_notes.pack(fill="both",expand=True,padx=8,pady=(0,8))
        top=ttk.Frame(import_tab); top.pack(fill="x",padx=10,pady=(10,5)); ttk.Label(top,text="Pega una lista: cantidad + nombre. También acepta (SET) número.",style="Sub.TLabel").pack(side="left"); ttk.Button(top,text="Analizar e importar…",style="Accent.TButton",command=self.import_bulk_deck).pack(side="right")
        self.bulk_deck_text=tk.Text(import_tab,wrap="none",font=("Consolas",10),bg=UI["surface2"], fg=UI["text"], insertbackground=UI["text"], selectbackground=UI["purple"], selectforeground="#FFFFFF",relief="solid",bd=1); self.bulk_deck_text.pack(fill="both",expand=True,padx=10,pady=(0,10))


    # Game Changers: lista vigente conocida tras la actualización del 9-feb-2026.
    # Bracket 1/2: 0; Bracket 3: hasta 3; Bracket 4/5: sin límite.
    GAME_CHANGERS = {
        "Drannith Magistrate","Humility","Serra's Sanctum","Smothering Tithe","Enlightened Tutor","Teferi's Protection",
        "Consecrated Sphinx","Cyclonic Rift","Force of Will","Fierce Guardianship","Gifts Ungiven","Intuition",
        "Mystical Tutor","Narset, Parter of Veils","Rhystic Study","Thassa's Oracle","Ad Nauseam","Bolas's Citadel",
        "Braids, Cabal Minion","Demonic Tutor","Imperial Seal","Necropotence","Opposition Agent","Orcish Bowmasters",
        "Tergrid, God of Fright","Vampiric Tutor","Gamble","Jeska's Will","Underworld Breach","Crop Rotation",
        "Gaea's Cradle","Natural Order","Seedborn Muse","Survival of the Fittest","Worldly Tutor","Aura Shards",
        "Coalition Victory","Grand Arbiter Augustin IV","Notion Thief","Ancient Tomb","Chrome Mox","Field of the Dead",
        "Glacial Chasm","Grim Monolith","Lion's Eye Diamond","Mana Vault","Mishra's Workshop","Mox Diamond",
        "Panoptic Mirror","The One Ring","The Tabernacle at Pendrell Vale","Farewell","Biorhythm"
    }

    def duplicate_deck(self):
        did=self.current_deck_id()
        if did is None:return
        self._sync_plan_from_assignments(did)
        deck=self.db.get_deck(did)
        name=simpledialog.askstring("Duplicar mazo","Nombre de la copia:",initialvalue=deck["name"]+" - copia",parent=self)
        if not name:return
        try:
            meta=self._deck_meta(did)
            self.db.create_deck(name.strip())
            with self.db.con() as c:
                row=c.execute("SELECT id FROM decks WHERE name=?",(name.strip(),)).fetchone()
                if not row: raise ValueError("No pude recuperar el mazo duplicado.")
                new_id=int(row["id"])
                c.execute("INSERT OR IGNORE INTO deck_meta(deck_id,status) VALUES (?,'Desarmado')",(new_id,))
                c.execute("""INSERT INTO deck_plan(deck_id,collection_id,quantity,is_commander)
                             SELECT ?,collection_id,quantity,is_commander FROM deck_plan WHERE deck_id=?""",(new_id,did))
                c.execute("""UPDATE deck_meta SET status='Desarmado',notes=?,declared_bracket=?,cedh_intent=?,chain_extra_turns=?
                             WHERE deck_id=?""",(meta["notes"],meta["declared_bracket"],meta["cedh_intent"],meta["chain_extra_turns"],new_id))
            self.refresh_decks()
            messagebox.showinfo("Duplicar mazo","Copia creada como Desarmado: no reserva cartas físicas.")
        except Exception as e:messagebox.showerror("Duplicar mazo",str(e))

    def save_deck_notes(self):
        did=self.current_deck_id()
        if did is None:return
        notes=self.deck_notes.get("1.0","end").rstrip()
        with self.db.con() as c:
            c.execute("INSERT OR IGNORE INTO deck_meta(deck_id) VALUES (?)",(did,))
            c.execute("UPDATE deck_meta SET notes=? WHERE deck_id=?",(notes,did))
        self.status_var.set("Notas guardadas.")

    def _considering_rows(self,did):
        with self.db.con() as c:
            return c.execute("""SELECT cg.id considering_id,cg.quantity,cg.notes,col.*
                                FROM considering cg JOIN collection col ON col.id=cg.collection_id
                                WHERE cg.deck_id=? ORDER BY col.name COLLATE NOCASE""",(did,)).fetchall()

    def refresh_considering(self):
        if not hasattr(self,"consider_tree"):return
        for i in self.consider_tree.get_children():self.consider_tree.delete(i)
        did=self.current_deck_id()
        if did is None:return
        for r in self._considering_rows(did):
            self.consider_tree.insert("","end",iid=str(r["considering_id"]),
                values=(r["quantity"],r["name"],(r["set_code"] or "").upper(),r["type_line"]))

    def add_considering(self):
        did=self.current_deck_id()
        if did is None:return
        q=self.consider_query.get().strip()
        if not q:return
        rows=self.db.find_collection_cards(q,limit=30)
        if not rows:
            messagebox.showinfo("Considering","La carta debe existir en tu colección para añadirla aquí.");return
        chosen=rows[0]
        if len(rows)>1:
            labels="\n".join(f"{i+1}. {r['name']} — {r['set_code'].upper()} {r['collector_number']}" for i,r in enumerate(rows))
            n=simpledialog.askinteger("Elegir impresión",labels,minvalue=1,maxvalue=len(rows),parent=self)
            if not n:return
            chosen=rows[n-1]
        with self.db.con() as c:
            row=c.execute("SELECT id FROM considering WHERE deck_id=? AND collection_id=?",(did,chosen["id"])).fetchone()
            if row:c.execute("UPDATE considering SET quantity=quantity+1 WHERE id=?",(row["id"],))
            else:c.execute("INSERT INTO considering(deck_id,collection_id,quantity) VALUES (?,?,1)",(did,chosen["id"]))
        self.consider_query.set(""); self.refresh_considering()

    def _considering_menu(self,event):
        iid=self.consider_tree.identify_row(event.y)
        if not iid:return
        self.consider_tree.selection_set(iid)
        menu=tk.Menu(self,tearoff=0)
        menu.add_command(label="Mover 1 al mazo",command=self.move_considering_to_deck)
        menu.add_command(label="Quitar de Considering",command=self.remove_considering)
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:
            try:menu.grab_release()
            except Exception:pass

    def remove_considering(self):
        sel=self.consider_tree.selection()
        if not sel:return
        with self.db.con() as c:c.execute("DELETE FROM considering WHERE id=?",(int(sel[0]),))
        self.refresh_considering()

    def move_considering_to_deck(self):
        sel=self.consider_tree.selection(); did=self.current_deck_id()
        if not sel or did is None:return
        with self.db.con() as c:
            r=c.execute("""SELECT cg.*,col.* FROM considering cg JOIN collection col ON col.id=cg.collection_id
                           WHERE cg.id=?""",(int(sel[0]),)).fetchone()
        if not r:return
        if self._deck_status(did)=="Desarmado":
            self._plan_add(did,r["collection_id"],1,False)
        else:
            result=self._assign_existing_copies(did,self.db.get_collection_card(r["collection_id"]),1,False)
            if result is False:return
        with self.db.con() as c:
            if int(r["quantity"])<=1:c.execute("DELETE FROM considering WHERE id=?",(int(sel[0]),))
            else:c.execute("UPDATE considering SET quantity=quantity-1 WHERE id=?",(int(sel[0]),))
        self.refresh_considering(); self.refresh_deck_cards(); self.refresh_collection()

    def _save_bracket_settings(self):
        did=self.current_deck_id()
        if did is None:return
        val=self.declared_bracket_var.get()
        declared=0 if val=="Sin declarar" else int(val[1:])
        with self.db.con() as c:
            c.execute("INSERT OR IGNORE INTO deck_meta(deck_id) VALUES (?)",(did,))
            c.execute("""UPDATE deck_meta SET declared_bracket=?,cedh_intent=?,chain_extra_turns=? WHERE deck_id=?""",
                      (declared,1 if self.cedh_var.get() else 0,1 if self.chain_turns_var.get() else 0,did))
        self._refresh_bracket(self._deck_rows(did))

    def _combo_rows(self,did):
        with self.db.con() as c:return c.execute("SELECT * FROM deck_combos WHERE deck_id=? ORDER BY id",(did,)).fetchall()

    def refresh_combo_list(self):
        if not hasattr(self,"combo_list"):return
        self.combo_list.delete(0,"end")
        did=self.current_deck_id()
        if did is None:return
        for r in self._combo_rows(did):
            self.combo_list.insert("end",f"{r['card1']} + {r['card2']}"+(" · TEMPRANO" if r["early"] else ""))

    def add_declared_combo(self):
        did=self.current_deck_id()
        if did is None:return
        a=self.combo_card1.get().strip(); b=self.combo_card2.get().strip()
        if not a or not b or a==b:return
        with self.db.con() as c:c.execute("INSERT INTO deck_combos(deck_id,card1,card2,early) VALUES (?,?,?,?)",
                                          (did,a,b,1 if self.combo_early.get() else 0))
        self.combo_early.set(False); self.refresh_combo_list(); self._refresh_bracket(self._deck_rows(did))

    def remove_declared_combo(self):
        did=self.current_deck_id(); sel=self.combo_list.curselection()
        if did is None or not sel:return
        rows=self._combo_rows(did)
        if sel[0] < len(rows):
            with self.db.con() as c:c.execute("DELETE FROM deck_combos WHERE id=?",(rows[sel[0]]["id"],))
        self.refresh_combo_list(); self._refresh_bracket(self._deck_rows(did))

    def open_commander_spellbook(self):
        did=self.current_deck_id()
        if did is None:return
        self.clipboard_clear(); self.clipboard_append(self._plain_deck_text(did)); self.update()
        webbrowser.open("https://commanderspellbook.com/find-my-combos/")
        self.status_var.set("Lista copiada. Pégala en Commander Spellbook para buscar combos.")

    FAST_MANA_NAMES = {
        "Sol Ring","Mana Crypt","Mana Vault","Jeweled Lotus","Chrome Mox","Mox Diamond",
        "Mox Opal","Lotus Petal","Lion's Eye Diamond","Grim Monolith",
        "Dark Ritual","Cabal Ritual","Jeska's Will"
    }

    def _row_text(self,r):
        return (r["oracle_text"] or "").lower()

    def _is_land_row(self,r):
        tl=(r["type_line"] or "").lower()
        return "land" in tl or "tierra" in tl

    def _detect_card_roles(self,r):
        """Heurística central reutilizada por Estadísticas y Bracket."""
        o=self._row_text(r)
        name=r["name"] or ""
        roles=set()
        nonland=not self._is_land_row(r)

        if nonland and (
            "add {" in o or
            "search your library for a basic land" in o or
            "search your library for a land card" in o or
            "search your library for up to two basic land" in o or
            "put a land card from your hand onto the battlefield" in o
        ):
            roles.add("Ramp")

        if re.search(r"\bdraw (?:a|one|two|three|four|five|x|that many|cards?)\b",o):
            roles.add("Robo")
        if (("exile the top" in o and ("you may play" in o or "you may cast" in o))
            or ("look at the top" in o and "put" in o)):
            roles.add("Ventaja/selección")

        if "counter target spell" in o or "counter target activated" in o or "counter target triggered" in o:
            roles.add("Counters")
        if any(x in o for x in [
            "destroy target","exile target","return target permanent",
            "return target creature","target creature gets -","target permanent gets -"
        ]):
            roles.add("Removal")

        if any(x in o for x in [
            "destroy all creatures","exile all creatures","destroy all nonland permanents",
            "exile all nonland permanents","destroy all artifacts","destroy all enchantments",
            "all creatures get -","each creature gets -"
        ]):
            roles.add("Board wipes")

        if any(x in o for x in [
            "hexproof","indestructible","phase out","phases out","protection from",
            "counter target spell or ability that targets"
        ]):
            roles.add("Protección")

        if any(x in o for x in [
            "search your library for a card",
            "search your library for an artifact",
            "search your library for an enchantment",
            "search your library for a creature card",
            "search your library for an instant",
            "search your library for a sorcery",
            "search your library for a planeswalker"
        ]):
            roles.add("Tutor")

        if any(x in o for x in [
            "return target card from your graveyard",
            "return target creature card from your graveyard",
            "return target permanent card from your graveyard",
            "return an artifact card from your graveyard",
            "return an enchantment card from your graveyard",
            "cast target instant or sorcery card from your graveyard"
        ]):
            roles.add("Recurrencia")

        if "extra turn" in o:
            roles.add("Turno extra")

        if name in self.FAST_MANA_NAMES or (nonland and float(r["mana_value"] or 0)<=1 and "add {" in o):
            roles.add("Fast mana")

        if ("you win the game" in o or "target player loses the game" in o or
            "each opponent loses the game" in o or "each opponent loses life equal to" in o):
            roles.add("Wincon explícita")

        if any(x in o for x in [
            "exile target card from a graveyard","exile all cards from all graveyards",
            "cards in graveyards can't","players can't cast spells from graveyards"
        ]):
            roles.add("Grave hate")

        if any(x in o for x in [
            "destroy all lands","lands don't untap","nonbasic lands are mountains",
            "players can't untap more than","return all lands","exile all lands"
        ]):
            roles.add("MLD")

        return roles

    def _analyze_deck(self,rows):
        counts=Counter()
        roles=Counter()
        role_cards={}
        curve=Counter()
        need=Counter()
        sources=Counter()
        mana_values=[]
        basic_map={
            "Plains":"W","Island":"U","Swamp":"B","Mountain":"R","Forest":"G",
            "Snow-Covered Plains":"W","Snow-Covered Island":"U","Snow-Covered Swamp":"B",
            "Snow-Covered Mountain":"R","Snow-Covered Forest":"G"
        }

        for r in rows:
            qty=int(r["quantity"] or 0)
            tl=(r["type_line"] or "").lower()
            is_land=self._is_land_row(r)

            checks=[
                ("Criaturas",("creature","criatura")),
                ("Tierras",("land","tierra")),
                ("Artefactos",("artifact","artefacto")),
                ("Encantamientos",("enchantment","encantamiento")),
                ("Instantáneos",("instant","instantáneo","instantaneo")),
                ("Conjuros",("sorcery","conjuro")),
                ("Planeswalkers",("planeswalker",)),
                ("Batallas",("battle","batalla")),
            ]
            for label,terms in checks:
                if any(t in tl for t in terms):
                    counts[label]+=qty

            detected=self._detect_card_roles(r)
            for role in detected:
                roles[role]+=qty
                role_cards.setdefault(role,[]).append(r["name"])

            if not is_land:
                mv=float(r["mana_value"] or 0)
                mana_values.extend([mv]*qty)
                curve["7+" if mv>=7 else str(int(mv))]+=qty
                for tok in self._mana_tokens(r["mana_cost"] or ""):
                    for c in "WUBRG":
                        if c in tok:
                            need[c]+=qty
            else:
                found=set()
                if r["name"] in basic_map:
                    found.add(basic_map[r["name"]])
                oracle=(r["oracle_text"] or "").upper()
                found.update(re.findall(r"ADD[^\n]*\{([WUBRG])\}",oracle))
                if "ANY COLOR" in oracle or "ANY ONE COLOR" in oracle:
                    found.update("WUBRG")
                if not found:
                    found.update(self._parse_color_codes(r["color_identity"]))
                for c in found:
                    sources[c]+=qty

        total=sum(int(r["quantity"] or 0) for r in rows)
        avg=sum(mana_values)/len(mana_values) if mana_values else 0
        interaction=roles["Removal"]+roles["Counters"]+roles["Board wipes"]+roles["Grave hate"]

        total_need=sum(need.values())
        total_sources=sum(sources.values())
        mana_warnings=[]
        for c in "WUBRG":
            if not need[c]:
                continue
            need_pct=need[c]/total_need if total_need else 0
            source_pct=sources[c]/total_sources if total_sources else 0
            if total_sources and need_pct-source_pct>=0.10:
                mana_warnings.append(c)

        return {
            "total":total,"counts":counts,"roles":roles,"role_cards":role_cards,
            "curve":curve,"need":need,"sources":sources,"avg_mv":avg,
            "interaction":interaction,"mana_warnings":mana_warnings
        }

    def _auto_role_counts(self,rows):
        a=self._analyze_deck(rows)
        rc=a["role_cards"]
        return (
            a["roles"],
            list(dict.fromkeys(rc.get("Turno extra",[]))),
            list(dict.fromkeys(rc.get("MLD",[]))),
            list(dict.fromkeys(rc.get("Tutor",[])))
        )

    def _mana_analysis(self,rows):
        a=self._analyze_deck(rows)
        return a["need"],a["sources"],a["curve"]

    def _refresh_bracket(self,rows):
        if not hasattr(self,"bracket_text"):return
        did=self.current_deck_id()
        if did is None:return
        meta=self._deck_meta(did)
        names={r["name"] for r in rows}
        gcs=sorted(names & self.GAME_CHANGERS)
        roles,extra,mld,tutors=self._auto_role_counts(rows)
        combos=list(self._combo_rows(did))
        early=sum(1 for c in combos if c["early"])
        late=len(combos)-early

        minimum=2
        reasons=[]
        if gcs:
            minimum=max(minimum,3); reasons.append(f"{len(gcs)} Game Changer(s)")
        if len(gcs)>3:
            minimum=4; reasons.append("más de 3 Game Changers")
        if late:
            minimum=max(minimum,3); reasons.append(f"{late} combo(s) de 2 cartas no temprano(s)")
        if early:
            minimum=4; reasons.append(f"{early} combo(s) temprano(s) de 2 cartas")
        if mld:
            minimum=4; reasons.append("neutralización masiva de tierras detectada")
        if meta["chain_extra_turns"]:
            minimum=4; reasons.append("intención de encadenar turnos extra")
        if meta["cedh_intent"]:
            minimum=5; reasons.append("intención cEDH declarada")

        declared=int(meta["declared_bracket"] or 0)
        title={2:"B2 · Core",3:"B3 · Upgraded",4:"B4 · Optimized",5:"B5 · cEDH"}.get(minimum,"B2 · Core")
        lines=[
            "ANALIZADOR DE BRACKET",
            "",
            f"Bracket mínimo detectado: {title}",
            f"Bracket declarado: {'B'+str(declared) if declared else 'Sin declarar'}",
            "",
            "Barómetros:",
            f"  Game Changers: {len(gcs)}",
            f"  Combos declarados de 2 cartas: {len(combos)} (tempranos: {early})",
            f"  Cartas de turnos extra detectadas: {len(extra)}",
            f"  Candidatas a MLD: {len(mld)}",
            f"  Tutores no-tierra detectados: {len(tutors)}",
        ]
        if reasons:lines += ["","Motivos del mínimo:"]+[f"  • {x}" for x in reasons]
        if gcs:lines += ["","Game Changers:"]+[f"  • {x}" for x in gcs]
        if extra:lines += ["","Turnos extra detectados:"]+[f"  • {x}" for x in sorted(set(extra))]
        if mld:lines += ["","MLD detectado (revisar manualmente):"]+[f"  • {x}" for x in sorted(set(mld))]
        if declared and declared<minimum:
            lines += ["",f"⚠ El mazo está declarado B{declared}, pero los barómetros detectados exigen al menos B{minimum}."]
        lines += ["",
            "Nota: B1 es principalmente una intención de juego/tema y no puede inferirse de forma fiable.",
            "B5 también depende de intención competitiva/metajuego. El resultado es una ayuda para la conversación prepartida, no una sentencia."
        ]
        self._set_text(self.bracket_text,"\n".join(lines))
        if hasattr(self,"deck_bracket_badge"):self.deck_bracket_badge.config(text=f"B{minimum}+")

    def create_backup(self):
        src=Path(self.db.path); folder=src.parent/"backups"; folder.mkdir(exist_ok=True)
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
        dest=folder/f"mtg_organizer_{stamp}.db"
        shutil.copy2(src,dest)
        messagebox.showinfo("Respaldo creado",f"Respaldo guardado en:\n{dest}")

    def restore_backup(self):
        path=filedialog.askopenfilename(title="Restaurar respaldo",filetypes=[("Base MTG","*.db"),("Todos","*.*")])
        if not path:return
        if not messagebox.askyesno("Restaurar respaldo","Esto reemplazará la base actual. ¿Continuar?"):return
        current=Path(self.db.path)
        safety=current.with_name("mtg_organizer_antes_restaurar_"+datetime.now().strftime("%Y%m%d_%H%M%S")+".db")
        shutil.copy2(current,safety); shutil.copy2(path,current)
        self._init_extended_schema()
        self.refresh_decks(); self.refresh_collection(); self.refresh_deck_cards()
        messagebox.showinfo("Restaurado",f"Base restaurada.\nCopia de seguridad previa: {safety.name}")

    def export_all_json(self):
        path=filedialog.asksaveasfilename(defaultextension=".json",filetypes=[("JSON","*.json")],initialfile="mtg_organizer_export.json")
        if not path:return
        tables=["collection","decks","deck_cards","movement_history","deck_meta","deck_plan","considering","deck_combos"]
        data={"exported_at":datetime.now().isoformat(),"version":"2.0"}
        with self.db.con() as c:
            for t in tables:
                rows=c.execute(f"SELECT * FROM {t}").fetchall()
                data[t]=[dict(r) for r in rows]
        Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        messagebox.showinfo("Exportación completa","Datos exportados a JSON.")


    BASIC_LANDS = {"Plains","Island","Swamp","Mountain","Forest","Wastes","Snow-Covered Plains","Snow-Covered Island","Snow-Covered Swamp","Snow-Covered Mountain","Snow-Covered Forest"}

    def _toggle_deck_sidebar(self):
        if getattr(self,"_deck_sidebar_visible",True):
            try:self._deck_outer.forget(self._deck_left); self._deck_sidebar_visible=False
            except Exception:pass
        else:
            try:self._deck_outer.insert(0,self._deck_left,weight=1); self._deck_sidebar_visible=True
            except Exception:pass

    def _show_import_tab(self):
        if hasattr(self,"_deck_import_tab"):self.deck_notebook.select(self._deck_import_tab)

    def _deck_more_filters(self):
        win=tk.Toplevel(self); win.title("Más filtros"); win.resizable(False,False); win.transient(self)
        box=ttk.Frame(win); box.pack(padx=12,pady=12)
        ttk.Label(box,text="Idioma:").grid(row=0,column=0,sticky="w",pady=5); lb=ttk.Combobox(box,textvariable=self.deck_lang_filter,state="readonly",width=15,values=["Todos","Inglés","Español","Portugués","Francés","Italiano","Alemán","Japonés","Otro"]); lb.grid(row=0,column=1,padx=6,pady=5)
        ttk.Label(box,text="Acabado:").grid(row=1,column=0,sticky="w",pady=5); fb=ttk.Combobox(box,textvariable=self.deck_finish_filter,state="readonly",width=15,values=["Todos","Normal","Foil","Etched"]); fb.grid(row=1,column=1,padx=6,pady=5)
        def apply():self.refresh_deck_cards(); win.destroy()
        ttk.Button(box,text="Aplicar",command=apply).grid(row=2,column=0,columnspan=2,sticky="e",pady=(8,0))

    def _is_basic_land_name(self,name):return (name or "") in self.BASIC_LANDS

    def _basic_land_variants(self,name):
        out=[]
        for r in self.db.list_collection(""):
            if (r["name"] or "")!=name:continue
            used=sum(u["quantity"] for u in self.db.card_usage(r["id"])); free=max(0,int(r["quantity"] or 0)-used)
            if free>0:out.append((r,free))
        return out

    def _add_basic_quantity(self,did,name,qty,preferred_id=None):
        qty=max(0,int(qty)); variants=self._basic_land_variants(name)
        if preferred_id is not None:variants=[p for p in variants if int(p[0]["id"])==int(preferred_id)]
        available=sum(f for _,f in variants)
        if available<qty:raise ValueError(f"Solo hay {available} copia(s) disponible(s) de {name}.")
        left=qty
        for r,free in variants:
            take=min(left,free)
            if take:self.db.add_to_deck(did,r["id"],take,False); left-=take
            if left<=0:break

    def _basic_land_dialog(self):
        did=self.current_deck_id()
        if did is None:
            messagebox.showerror("Sin mazo","Selecciona un mazo primero.")
            return
        win=tk.Toplevel(self)
        win.title("Gestionar tierras básicas")
        win.geometry("650x520")
        win.minsize(620,480)
        win.transient(self)
        win.grab_set()

        nb=ttk.Notebook(win)
        nb.pack(fill="both",expand=True,padx=12,pady=12)
        quick=ttk.Frame(nb); exact=ttk.Frame(nb)
        nb.add(quick,text="Añadir varias")
        nb.add(exact,text="Por edición")

        # Vista rápida: varias tierras de una vez, usando cualquier edición libre.
        ttk.Label(quick,text="Añade varias tierras de una sola vez. El programa usa las impresiones libres que tengas.",style="Sub.TLabel").pack(anchor="w",pady=(8,10),padx=8)
        grid=ttk.Frame(quick); grid.pack(fill="x",padx=8)
        quick_vars={}
        current={}
        rows=list(self.db.deck_cards(did))
        for r in rows:
            if self._is_basic_land_name(r["name"]):
                current[r["name"]]=current.get(r["name"],0)+int(r["quantity"] or 0)
        lands=sorted(self.BASIC_LANDS)
        for i,name in enumerate(lands):
            row=i%6; group=i//6
            base_col=group*4
            ttk.Label(grid,text=name).grid(row=row,column=base_col,sticky="w",padx=(0,6),pady=6)
            ttk.Label(grid,text=f"actual {current.get(name,0)}",style="Sub.TLabel").grid(row=row,column=base_col+1,sticky="w",padx=(0,8))
            v=tk.IntVar(value=0); quick_vars[name]=v
            ttk.Spinbox(grid,from_=0,to=99,textvariable=v,width=5).grid(row=row,column=base_col+2,padx=(0,14))
        def add_many():
            wanted={n:max(0,int(v.get() or 0)) for n,v in quick_vars.items() if int(v.get() or 0)>0}
            if not wanted:
                return
            try:
                affected=[]
                for n,q in wanted.items():
                    variants=self._basic_land_variants(n)
                    available=sum(free for _,free in variants)
                    if available<q:
                        raise ValueError(f"{n}: pediste {q}, pero solo hay {available} copia(s) libre(s).")
                    affected.extend(r["id"] for r,_ in variants)
                state=self._capture_undo_state(affected)
                for n,q in wanted.items():
                    self._add_basic_quantity(did,n,q)
                self._push_undo("Añadir tierras básicas",state)
                self.refresh_deck_cards(); self.refresh_collection()
                win.destroy()
            except Exception as e:
                messagebox.showerror("Tierras básicas",str(e),parent=win)
        qbtn=ttk.Frame(quick); qbtn.pack(fill="x",padx=8,pady=12)
        ttk.Button(qbtn,text="Cancelar",command=win.destroy).pack(side="right")
        ttk.Button(qbtn,text="Añadir cantidades",style="Accent.TButton",command=add_many).pack(side="right",padx=6)

        # Selección exacta por impresión.
        box=ttk.Frame(exact); box.pack(fill="both",expand=True,padx=12,pady=12)
        land=tk.StringVar(value="Plains"); qty=tk.IntVar(value=1); edition=tk.StringVar(); mapping={}
        ttk.Label(box,text="Tierra:").grid(row=0,column=0,sticky="w",pady=6)
        cb=ttk.Combobox(box,textvariable=land,state="readonly",width=26,values=sorted(self.BASIC_LANDS)); cb.grid(row=0,column=1,sticky="w",pady=6)
        ttk.Label(box,text="Cantidad:").grid(row=1,column=0,sticky="w",pady=6)
        ttk.Spinbox(box,from_=1,to=99,textvariable=qty,width=8).grid(row=1,column=1,sticky="w",pady=6)
        ttk.Label(box,text="Impresión:").grid(row=2,column=0,sticky="w",pady=6)
        edbox=ttk.Combobox(box,textvariable=edition,state="readonly",width=50); edbox.grid(row=2,column=1,sticky="ew",pady=6)
        def refresh(*_):
            mapping.clear(); vals=[]
            for r,free in self._basic_land_variants(land.get()):
                label=f"{r['set_code'].upper()} {r['collector_number']} · {self._lang_name(r['lang'])} · {self._finish_name(r['finish'])} · libres {free}"
                mapping[label]=r["id"]; vals.append(label)
            edbox["values"]=vals; edition.set(vals[0] if vals else "")
        cb.bind("<<ComboboxSelected>>",refresh); refresh()
        def add_exact():
            try:
                pref=mapping.get(edition.get())
                if pref is None: raise ValueError("Selecciona una impresión disponible.")
                state=self._capture_undo_state([pref])
                self._add_basic_quantity(did,land.get(),qty.get(),pref)
                self._push_undo(f"Añadir {qty.get()} × {land.get()}",state)
                self.refresh_deck_cards(); self.refresh_collection(); win.destroy()
            except Exception as e: messagebox.showerror("Tierras básicas",str(e),parent=win)
        btn=ttk.Frame(box); btn.grid(row=3,column=0,columnspan=2,sticky="e",pady=(14,0))
        ttk.Button(btn,text="Cancelar",command=win.destroy).pack(side="right")
        ttk.Button(btn,text="Añadir",style="Accent.TButton",command=add_exact).pack(side="right",padx=6)

    def _selected_deck_info(self):
        sel=self.deck_tree.selection(); return getattr(self,"_deck_display_rows",{}).get(sel[0]) if sel else None

    def _show_deck_context_menu(self,event):
        iid=self.deck_tree.identify_row(event.y)
        if not iid or iid.startswith("section::"):return
        if self._deck_status(self.current_deck_id())=="Desarmado":
            self.deck_tree.selection_set(iid); self.deck_tree.focus(iid)
            menu=tk.Menu(self,tearoff=0)
            menu.add_command(label="Ver detalle / desglose",command=self._deck_context_detail)
            menu.add_separator()
            menu.add_command(label="+1 al plan",command=lambda:self._plan_adjust_selected(1))
            menu.add_command(label="−1 del plan",command=lambda:self._plan_adjust_selected(-1))
            try:menu.tk_popup(event.x_root,event.y_root)
            finally:
                try:menu.grab_release()
                except Exception:pass
            return
        self.deck_tree.selection_set(iid); self.deck_tree.focus(iid)
        try:self.deck_context_menu.tk_popup(event.x_root,event.y_root)
        finally:
            try:self.deck_context_menu.grab_release()
            except Exception:pass

    def _plan_adjust_selected(self,delta):
        did=self.current_deck_id(); info=self._selected_deck_info()
        if did is None or not info or self._deck_status(did)!="Desarmado":return
        rows=info.get("rows",[])
        if not rows:return
        row=rows[-1]; cid=int(row["id"]); iscmd=bool(row["is_commander"])
        with self.db.con() as c:
            cur=c.execute("SELECT quantity FROM deck_plan WHERE deck_id=? AND collection_id=? AND is_commander=?",
                          (did,cid,1 if iscmd else 0)).fetchone()
            q=int(cur["quantity"] if cur else 0)+int(delta)
            if q<=0:
                c.execute("DELETE FROM deck_plan WHERE deck_id=? AND collection_id=? AND is_commander=?",
                          (did,cid,1 if iscmd else 0))
            elif cur:
                c.execute("UPDATE deck_plan SET quantity=? WHERE deck_id=? AND collection_id=? AND is_commander=?",
                          (q,did,cid,1 if iscmd else 0))
            else:
                c.execute("INSERT INTO deck_plan(deck_id,collection_id,quantity,is_commander) VALUES (?,?,?,?)",
                          (did,cid,q,1 if iscmd else 0))
        self.refresh_deck_cards()

    def _deck_context_detail(self):
        info=self._selected_deck_info()
        if not info:return
        if info.get("group"):
            lines=[f"{r['quantity']} × {r['name']} — {r['set_code'].upper()} {r['collector_number']} · {self._lang_name(r['lang'])} · {self._finish_name(r['finish'])}" for r in info["rows"]]
            messagebox.showinfo(info["name"],"\n".join(lines))
        else:
            row=info["rows"][0]
            if self._deck_status(self.current_deck_id())=="Desarmado":
                card=self.db.get_collection_card(int(row["id"]))
            else:
                card=self.db.get_deck_card_collection(int(row["deck_card_id"]))
            if card:self._card_detail_window(card)

    def _deck_remove_one_info(self,info):
        did=self.current_deck_id()
        if did is None:return
        deck=self.db.get_deck(did)
        rows=info.get("rows",[])
        # Quita primero de la última impresión mostrada; en tierras agrupadas esto mantiene el desglose físico.
        for row in reversed(rows):
            if int(row["quantity"] or 0)>0:
                self.db.move_one_copy(row["id"],deck["name"],"DISPONIBLE")
                return
        raise ValueError("No quedan copias de esa carta en el mazo.")

    def _free_qty_for_collection_row(self,row):
        used=sum(int(u["quantity"] or 0) for u in self.db.card_usage(row["id"]))
        return max(0,int(row["quantity"] or row["owned"] or 0)-used)

    def _usage_elsewhere_text(self,row,did):
        parts=[]
        for u in self.db.card_usage(row["id"]):
            if int(u["deck_id"])!=int(did):
                parts.append(f"{u['deck_name']} ×{u['quantity']}")
        return ", ".join(parts)

    def _assign_existing_copies(self,did,row,qty,is_commander=False,ask_move=True):
        qty=max(1,int(qty))
        free=self._free_qty_for_collection_row(row)
        if free>=qty:
            self.db.add_to_deck(did,row["id"],qty,is_commander)
            return
        need=qty-free
        elsewhere=[u for u in self.db.card_usage(row["id"]) if int(u["deck_id"])!=int(did) and int(u["quantity"] or 0)>0]
        movable=sum(int(u["quantity"] or 0) for u in elsewhere)
        if is_commander:
            raise ValueError(f"Solo hay {free} copia(s) libre(s). Para marcarla como comandante, libera primero una copia desde otro mazo.")
        if not ask_move or movable<need:
            where=self._usage_elsewhere_text(row,did)
            extra=f"\nEn otros mazos: {where}." if where else ""
            raise ValueError(f"Solo hay {free} copia(s) libre(s); faltan {need}.{extra}")
        where=self._usage_elsewhere_text(row,did)
        ok=messagebox.askyesno("Carta ocupada",
            f"Solo hay {free} copia(s) libre(s) de {row['name']}.\n\n"
            f"Faltan {need} y hay copias en: {where}.\n\n"
            "¿Mover automáticamente las copias necesarias al mazo actual?")
        if not ok:return False
        # Usa primero las libres y luego mueve solo lo que falta.
        if free:self.db.add_to_deck(did,row["id"],free,False)
        left=need
        target=self.db.get_deck(did)["name"]
        for u in elsewhere:
            for _ in range(min(left,int(u["quantity"] or 0))):
                self.db.move_one_copy(row["id"],u["deck_name"],target)
                left-=1
                if left<=0:break
            if left<=0:break
        return True



    def _deck_adjust_delta(self,delta):
        info=self._selected_deck_info()
        if not info or not delta:return
        did=self.current_deck_id()
        if did is None:return
        state=self._capture_undo_state([r["id"] for r in info["rows"]])
        try:
            if delta<0:
                for _ in range(abs(delta)):self._deck_remove_one_info(info)
            elif info.get("group"):
                self._add_basic_quantity(did,info["name"],delta)
            else:
                r=info["rows"][0]
                result=self._assign_existing_copies(did,r,delta,bool(r["is_commander"]))
                if result is False:return
            self._push_undo(f"{info['name']} {'+' if delta>0 else ''}{delta}",state)
            self.refresh_deck_cards(); self.refresh_collection()
        except Exception as e:messagebox.showerror("Cantidad",str(e))

    def _deck_change_quantity(self):
        info=self._selected_deck_info()
        if not info:return
        current=sum(int(r["quantity"] or 0) for r in info["rows"])
        new=simpledialog.askinteger("Cambiar cantidad",f"{info['name']}\nCantidad actual: {current}\nNueva cantidad:",minvalue=0,maxvalue=99,parent=self)
        if new is None or new==current:return
        self._deck_adjust_delta(new-current)


    def _deck_move_copies(self):
        info=self._selected_deck_info(); did=self.current_deck_id()
        if not info or did is None:return
        current=self.db.get_deck(did); total=sum(int(r["quantity"] or 0) for r in info["rows"])
        if total<=0:return
        targets=["DISPONIBLE"]+[d["name"] for d in self.db.list_decks() if int(d["id"])!=int(did)]
        choices="\\n".join(f"{i+1}. {n}" for i,n in enumerate(targets))
        n=simpledialog.askinteger("Mover copias",f"{info['name']} · {total} en este mazo\\n\\nDestino:\\n{choices}",minvalue=1,maxvalue=len(targets),parent=self)
        if not n:return
        qty=simpledialog.askinteger("Mover copias",f"¿Cuántas copias mover a {targets[n-1]}?",minvalue=1,maxvalue=total,parent=self)
        if not qty:return
        dest=targets[n-1]; left=qty; state=self._capture_undo_state([r["id"] for r in info["rows"]])
        try:
            for r in reversed(info["rows"]):
                take=min(left,int(r["quantity"] or 0))
                for _ in range(take):
                    self.db.move_one_copy(r["id"],current["name"],dest); left-=1
                if left<=0:break
            self._push_undo(f"Mover {qty} × {info['name']} → {dest}",state)
            self.refresh_deck_cards(); self.refresh_collection()
        except Exception as e:messagebox.showerror("Mover copias",str(e))

    def _deck_move_one(self):
        self._deck_move_copies()

    def export_plain_deck(self):
        did=self.current_deck_id()
        if did is None:return
        deck=self.db.get_deck(did); safe=re.sub(r'[\/:*?"<>|]+',"_",deck["name"] or "mazo"); path=filedialog.asksaveasfilename(defaultextension=".txt",filetypes=[("Texto","*.txt")],initialfile=f"{safe}.txt")
        if path:Path(path).write_text(self._plain_deck_text(did),encoding="utf-8"); self.status_var.set("Lista exportada.")

    def _clear_deck_mana_overlays(self):
        for widget in getattr(self, "_deck_mana_overlays", []):
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self._deck_mana_overlays = []

    def _make_deck_mana_cost_canvas(self, iid, mana_cost):
        bbox = self.deck_tree.bbox(iid, "mana")
        if not bbox:
            return
        x, y, w, h = bbox
        if w <= 2 or h <= 2:
            return
        canvas = tk.Canvas(self.deck_tree, width=w, height=h, bg=UI["surface"],
                           highlightthickness=0, bd=0, takefocus=0)
        canvas.place(x=x, y=y, width=w, height=h)
        tokens = self._mana_tokens(mana_cost)
        if not tokens:
            canvas.create_text(8, h//2, text="—", anchor="w", fill=UI["muted"], font=("Arial", 9))
        else:
            size=max(16,min(22,h-6))
            gap=2
            cx=5
            cy=h//2
            for token in tokens:
                photo=self._get_real_mana_photo(token,size)
                if photo is not None:
                    canvas.create_image(cx,cy,image=photo,anchor="w")
                    canvas._mana_images=getattr(canvas,"_mana_images",[])+[photo]
                else:
                    canvas.create_text(cx,cy,text="{"+token+"}",anchor="w",
                                       fill=UI["muted"],font=("Arial",8))
                cx += size + gap

        def _select(_event=None):
            self.deck_tree.selection_set(iid)
            self.deck_tree.focus(iid)
        canvas.bind("<Button-1>", _select)
        canvas.bind("<Double-1>", lambda e: (self.deck_tree.selection_set(iid),
                                                self.show_deck_card_detail()))
        self._deck_mana_overlays.append(canvas)

    def _redraw_deck_mana_costs(self):
        if not hasattr(self, "deck_tree"):
            return
        self._clear_deck_mana_overlays()
        values = getattr(self, "_deck_mana_values", {})
        for iid in self.deck_tree.get_children(""):
            mana_cost = values.get(str(iid))
            if mana_cost is None:
                continue
            self._make_deck_mana_cost_canvas(iid, mana_cost)

    def clear_deck_filters(self):
        if hasattr(self, "deck_filter"):
            self.deck_filter.set("")
            self.deck_type_filter.set("Todos")
            self.deck_identity_filter.set("Todos")
            self.deck_set_filter.set("Todas")
            self.deck_lang_filter.set("Todos")
            self.deck_finish_filter.set("Todos")
        self.refresh_deck_cards()

    def _deck_row_matches_filters(self, row):
        q = self.deck_filter.get().strip().lower() if hasattr(self, "deck_filter") else ""
        hay = " ".join(str(row[k] or "") for k in ["name","set_code","type_line"]).lower()
        if q and q not in hay:
            return False
        if hasattr(self, "deck_type_filter") and not self._type_matches(row["type_line"], self.deck_type_filter.get()):
            return False

        identity = self.deck_identity_filter.get() if hasattr(self, "deck_identity_filter") else "Todos"
        color_code = {"Blanco":"W","Azul":"U","Negro":"B","Rojo":"R","Verde":"G"}.get(identity)
        actual = set(self._parse_color_codes(row["color_identity"]))
        if identity == "Incolora" and actual:
            return False
        if color_code and color_code not in actual:
            return False

        if hasattr(self, "deck_set_filter") and self.deck_set_filter.get() != "Todas":
            if (row["set_code"] or "").upper() != self.deck_set_filter.get():
                return False
        if hasattr(self, "deck_lang_filter") and self.deck_lang_filter.get() != "Todos":
            if self._lang_name(row["lang"]) != self.deck_lang_filter.get():
                return False
        if hasattr(self, "deck_finish_filter") and self.deck_finish_filter.get() != "Todos":
            if self._finish_name(row["finish"]) != self.deck_finish_filter.get():
                return False
        return True

    def refresh_decks(self):
        self.deck_rows=self.db.list_decks()
        self.deck_list.delete(0,"end")
        for d in self.deck_rows:
            st=self._deck_status(d["id"])
            marker={"Montado":"●","En construcción":"◐","Desarmado":"○"}.get(st,"")
            self.deck_list.insert("end",f"{marker} {d['name']}")

    def current_deck_id(self):
        sel=self.deck_list.curselection()
        return self.deck_rows[sel[0]]["id"] if sel else None

    def new_deck(self):
        name=simpledialog.askstring("Nuevo Commander","Nombre del mazo:")
        if not name:return
        try:
            self.db.create_deck(name.strip())
            with self.db.con() as c:
                row=c.execute("SELECT id FROM decks WHERE name=?",(name.strip(),)).fetchone()
                if not row: raise ValueError("No pude recuperar el mazo creado.")
                did=int(row["id"])
                c.execute("INSERT OR IGNORE INTO deck_meta(deck_id) VALUES (?)",(did,))
        except Exception:
            messagebox.showerror("Nombre repetido","Ya existe un mazo con ese nombre.")
            return
        self.refresh_decks()
        self.deck_list.selection_set("end"); self.deck_list.activate("end"); self.on_deck_select()

    def delete_deck(self):
        did=self.current_deck_id()
        if did is None:return
        if messagebox.askyesno("Eliminar mazo","¿Eliminar este mazo? La colección no se modifica."):
            self.db.delete_deck(did); self.refresh_decks(); self.refresh_deck_cards()

    def _deck_category(self,row):
        if row["is_commander"]: return "Commander"
        tl=(row["type_line"] or "").lower()
        if "land" in tl or "tierra" in tl:return "Tierras"
        if "creature" in tl or "criatura" in tl:return "Criaturas"
        if "planeswalker" in tl:return "Planeswalkers"
        if "instant" in tl:return "Instantáneos"
        if "sorcery" in tl or "conjuro" in tl:return "Conjuros"
        if "artifact" in tl or "artefacto" in tl:return "Artefactos"
        if "enchantment" in tl or "encantamiento" in tl:return "Encantamientos"
        if "battle" in tl or "batalla" in tl:return "Batallas"
        return "Otros"

    def _deck_section_order(self):
        return ["Commander","Criaturas","Artefactos","Encantamientos","Instantáneos","Conjuros","Planeswalkers","Batallas","Tierras","Otros"]

    def _insert_deck_section(self,title,qty,key):
        iid=f"section::{key}"
        self.deck_tree.insert("","end",iid=iid,values=("",qty,f"── {title} ({qty}) ──","","","","","","","",""),tags=("section",))

    def _deck_display_units(self,rows):
        basics={}; normal=[]
        for r in rows:
            if self._is_basic_land_name(r["name"]) and not r["is_commander"]: basics.setdefault(r["name"],[]).append(r)
            else: normal.append(r)
        units=[{"kind":"row","name":r["name"],"rows":[r],"sample":r} for r in normal]
        for name,group in basics.items(): units.append({"kind":"basic","name":name,"rows":group,"sample":group[0]})
        return units

    def _insert_deck_unit(self,unit,commander=False):
        group=unit["rows"]; name=unit["name"]; qty=sum(int(r["quantity"] or 0) for r in group)
        if unit["kind"]=="basic":
            iid="basic::"+name
            sets={r["set_code"].upper() for r in group}; langs={self._lang_name(r["lang"]) for r in group}; fins={self._finish_name(r["finish"]) for r in group}
            self.deck_tree.insert("","end",iid=iid,values=("",qty,name,"","Varias" if len(sets)>1 else next(iter(sets),""),"Varias" if len(langs)>1 else next(iter(langs),""),"Varias" if len(fins)>1 else next(iter(fins),""),"Agrupadas",group[0]["type_line"],sum(int(r["owned"] or 0) for r in group),sum(int(r["used_elsewhere"] or 0) for r in group)))
            self._deck_display_rows[iid]={"group":True,"name":name,"rows":group}
            return
        r=group[0]; iid=str(r["deck_card_id"]); self._deck_mana_values[iid]=r["mana_cost"] or ""
        self.deck_tree.insert("","end",iid=iid,values=("★" if r["is_commander"] else "",r["quantity"],r["name"],"",r["set_code"].upper(),self._lang_name(r["lang"]),self._finish_name(r["finish"]),self._treatment_name(r["treatment"]),r["type_line"],r["owned"],r["used_elsewhere"]),tags=("commander",) if commander else ())
        self._deck_display_rows[iid]={"group":False,"name":r["name"],"rows":[r]}

    def on_deck_select(self):
        did=self.current_deck_id()
        if did is None:return
        deck=self.db.get_deck(did); meta=self._deck_meta(did)
        self.deck_title.config(text=deck["name"])
        self.deck_status_var.set(meta["status"] or "Montado")
        self.declared_bracket_var.set("Sin declarar" if not meta["declared_bracket"] else f"B{meta['declared_bracket']}")
        self.cedh_var.set(bool(meta["cedh_intent"])); self.chain_turns_var.set(bool(meta["chain_extra_turns"]))
        self.deck_notes.delete("1.0","end"); self.deck_notes.insert("1.0",meta["notes"] or "")
        rows=self._deck_rows(did)
        names=sorted({r["name"] for r in rows})
        self.combo_card1_box["values"]=names; self.combo_card2_box["values"]=names
        self.refresh_combo_list(); self.refresh_considering()
        self.refresh_deck_cards()
        self.refresh_deck_builder()

    def _schedule_builder_refresh(self):
        if not hasattr(self,"builder_tree"):
            return
        if getattr(self,"_builder_refresh_job",None):
            try:self.after_cancel(self._builder_refresh_job)
            except Exception:pass
        self._builder_refresh_job=self.after(140,self.refresh_deck_builder)

    def _clear_deck_builder(self):
        if hasattr(self,"builder_query"):
            self.builder_query.set("")
            self.builder_availability.set("Disponibles")
        self.refresh_deck_builder()

    def _builder_usage_text(self,row):
        uses=list(self.db.card_usage(row["id"]))
        if not uses:
            return "Disponible"
        return " · ".join(f"{u['deck_name']} ×{int(u['quantity'] or 0)}" for u in uses)

    def refresh_deck_builder(self):
        if not hasattr(self,"builder_tree"):
            return
        self._builder_refresh_job=None
        for iid in self.builder_tree.get_children():
            self.builder_tree.delete(iid)

        did=self.current_deck_id()
        if did is None:
            self.builder_summary.config(text="Selecciona un mazo para empezar a construir.")
            return

        q=self.builder_query.get().strip().lower() if hasattr(self,"builder_query") else ""
        mode=self.builder_availability.get() if hasattr(self,"builder_availability") else "Disponibles"
        rows=list(self.db.list_collection(""))
        shown=[]
        total_free=0
        for row in rows:
            hay=" ".join(str(self._row_value(row,k,"") or "") for k in
                         ["name","set_code","set_name","collector_number","type_line"]).lower()
            if q and q not in hay:
                continue
            free=self._free_qty_for_collection_row(row)
            uses=list(self.db.card_usage(row["id"]))
            used_elsewhere=sum(int(u["quantity"] or 0) for u in uses if int(u["deck_id"])!=int(did))
            if mode=="Disponibles" and free<=0:
                continue
            if mode=="En otros mazos" and used_elsewhere<=0:
                continue
            shown.append((row,free,used_elsewhere))
            total_free+=free

        shown.sort(key=lambda item:((item[0]["name"] or "").lower(),(item[0]["set_code"] or "").lower(),str(item[0]["collector_number"] or "")))
        for row,free,used_elsewhere in shown:
            iid=str(row["id"])
            values=(
                free,
                int(row["quantity"] or 0),
                row["name"] or "",
                row["mana_cost"] or "—",
                (row["set_code"] or "").upper(),
                self._lang_name(row["lang"]),
                self._finish_name(row["finish"]),
                row["type_line"] or "",
                self._builder_usage_text(row),
            )
            self.builder_tree.insert("","end",iid=iid,values=values,tags=("free",) if free>0 else ("occupied",))

        deck=self.db.get_deck(did)
        label=f"{len(shown)} impresiones · {total_free} copia(s) libre(s)"
        if q:
            label+=f" · búsqueda: «{self.builder_query.get().strip()}»"
        if deck:
            label+=f" · destino: {deck['name']}"
        self.builder_summary.config(text=label)

    def _selected_builder_row(self):
        if not hasattr(self,"builder_tree"):
            return None
        sel=self.builder_tree.selection()
        if not sel:
            return None
        try:
            return self.db.get_collection_card(int(sel[0]))
        except Exception:
            return None

    def _update_builder_summary(self):
        row=self._selected_builder_row()
        did=self.current_deck_id()
        if not row or did is None:
            return
        free=self._free_qty_for_collection_row(row)
        self.builder_summary.config(
            text=f"{row['name']} · {(row['set_code'] or '').upper()} {row['collector_number'] or ''} · "
                 f"{free} libre(s) de {int(row['quantity'] or 0)} · {self._builder_usage_text(row)}"
        )

    def _builder_add_quantity(self):
        qty=max(1,int(self.builder_qty.get() or 1))
        self._builder_add_selected(qty,False)

    def _builder_add_selected(self,qty=1,is_commander=False):
        did=self.current_deck_id()
        if did is None:
            messagebox.showerror("Sin mazo","Selecciona primero un mazo.")
            return
        row=self._selected_builder_row()
        if not row:
            messagebox.showinfo("Constructor","Selecciona una carta de tu colección.")
            return
        qty=max(1,int(qty))
        deck=self.db.get_deck(did)

        if self._deck_status(did)=="Desarmado":
            self._plan_add(did,row["id"],qty,bool(is_commander))
            self.refresh_deck_cards()
            self.refresh_deck_builder()
            self.status_var.set(f"{row['name']} añadido al plan de {deck['name']}.")
            return

        state=self._capture_undo_state([row["id"]])
        try:
            result=self._assign_existing_copies(did,row,qty,bool(is_commander))
            if result is False:
                return
            action=("como comandante" if is_commander else f"×{qty}")
            self._push_undo(f"{row['name']} {action} → {deck['name']}",state)
            self.refresh_deck_cards()
            self.refresh_collection()
            self.refresh_deck_builder()
            self.status_var.set(
                f"{row['name']} añadido {'como comandante' if is_commander else 'al mazo'}."
            )
        except Exception as e:
            messagebox.showerror("Constructor",str(e))

    def refresh_deck_cards(self):
        if not hasattr(self,"deck_tree"):return
        self._clear_deck_mana_overlays(); self._deck_mana_values={}; self._deck_display_rows={}
        for i in self.deck_tree.get_children():self.deck_tree.delete(i)
        did=self.current_deck_id()
        if did is None:
            self.deck_count.config(text=""); self._set_text(self.stats_text,""); self._set_text(self.validation_text,""); return
        rows=list(self._deck_rows(did)); total=sum(int(r["quantity"] or 0) for r in rows); cmds=sum(1 for r in rows if r["is_commander"])
        editions=sorted({(r["set_code"] or "").upper() for r in rows if r["set_code"]}); self.deck_filter_set_box["values"]=["Todas"]+editions
        if self.deck_set_filter.get() not in self.deck_filter_set_box["values"]:self.deck_set_filter.set("Todas")
        shown=[r for r in rows if self._deck_row_matches_filters(r)]
        commanders=[r for r in shown if r["is_commander"]]; main=[r for r in shown if not r["is_commander"]]
        shown_qty=sum(int(r["quantity"] or 0) for r in shown)
        if commanders:
            cqty=sum(int(r["quantity"] or 0) for r in commanders); self._insert_deck_section("Commander",cqty,"commander")
            for r in sorted(commanders,key=lambda x:(x["name"] or "").lower()): self._insert_deck_unit({"kind":"row","name":r["name"],"rows":[r],"sample":r},True)
        mode=self.deck_sort_mode.get() if hasattr(self,"deck_sort_mode") else "Por tipo"; units=self._deck_display_units(main)
        if mode=="Alfabético":
            for unit in sorted(units,key=lambda u:u["name"].lower()): self._insert_deck_unit(unit)
        elif mode=="Valor de maná":
            mana_groups={}; lands=[]
            for unit in units:
                sample=unit["sample"]; tl=(sample["type_line"] or "").lower()
                if "land" in tl or "tierra" in tl: lands.append(unit); continue
                mv=float(sample["mana_value"] or 0); label=int(mv) if mv.is_integer() else mv; mana_groups.setdefault(label,[]).append(unit)
            for mv in sorted(mana_groups,key=float):
                group=mana_groups[mv]; qty=sum(sum(int(r["quantity"] or 0) for r in u["rows"]) for u in group); self._insert_deck_section(f"Valor de maná {mv}",qty,f"mv{mv}")
                for unit in sorted(group,key=lambda u:u["name"].lower()): self._insert_deck_unit(unit)
            if lands:
                qty=sum(sum(int(r["quantity"] or 0) for r in u["rows"]) for u in lands); self._insert_deck_section("Tierras",qty,"lands")
                for unit in sorted(lands,key=lambda u:u["name"].lower()): self._insert_deck_unit(unit)
        else:
            cats={}
            for unit in units: cats.setdefault(self._deck_category(unit["sample"]),[]).append(unit)
            for cat in self._deck_section_order():
                if cat=="Commander":continue
                group=cats.get(cat,[])
                if not group:continue
                qty=sum(sum(int(r["quantity"] or 0) for r in u["rows"]) for u in group); self._insert_deck_section(cat,qty,re.sub(r"[^a-z0-9]+","_",cat.lower()))
                for unit in sorted(group,key=lambda u:u["name"].lower()): self._insert_deck_unit(unit)
        self.deck_count.config(text=f"{total}/100 · {cmds} comandante(s) · mostrando {shown_qty}")
        self._refresh_stats(rows); self._refresh_bracket(rows); self._refresh_validation(rows); self.after_idle(self._redraw_deck_mana_costs)

    def _set_text(self, widget, text):
        widget.configure(state="normal"); widget.delete("1.0","end"); widget.insert("1.0",text); widget.configure(state="disabled")

    def _refresh_stats(self, rows):
        if not rows:
            self._set_text(self.stats_text,"Aún no hay cartas en este mazo.")
            if hasattr(self,"deck_analysis_badge"):
                self.deck_analysis_badge.config(text="")
            return

        a=self._analyze_deck(rows)
        counts=a["counts"]; roles=a["roles"]; curve=a["curve"]
        need=a["need"]; sources=a["sources"]

        summary=f"Ramp {roles['Ramp']} · Robo {roles['Robo']} · Interacción {a['interaction']}"
        if hasattr(self,"deck_analysis_badge"):
            self.deck_analysis_badge.config(text=summary)

        lines=[
            "ANÁLISIS DEL MAZO","",
            f"Cartas: {a['total']}  ·  Valor de maná medio sin tierras: {a['avg_mv']:.2f}",
            "","TIPOS"
        ]
        for k in ["Criaturas","Tierras","Artefactos","Encantamientos","Instantáneos","Conjuros","Planeswalkers","Batallas"]:
            if counts[k]:
                lines.append(f"{k:<18} {counts[k]:>3}")

        lines += ["","CURVA DE MANÁ"]
        max_curve=max([curve[k] for k in ["0","1","2","3","4","5","6","7+"]] or [1])
        for k in ["0","1","2","3","4","5","6","7+"]:
            n=curve[k]
            bar_len=0 if not n else max(1,round(24*n/max_curve))
            lines.append(f"{k:>2}  {'█'*bar_len:<24} {n}")

        lines += ["","FUNCIONES — detección automática"]
        role_order=[
            "Ramp","Robo","Ventaja/selección","Tutor","Recurrencia",
            "Removal","Counters","Board wipes","Protección","Grave hate",
            "Fast mana","Turno extra","Wincon explícita"
        ]
        for k in role_order:
            lines.append(f"{k:<20} {roles[k]:>3}")
        lines.append(f"{'Interacción total':<20} {a['interaction']:>3}")

        lines += ["","LECTURA RÁPIDA"]
        def indicator(label,value,low,high):
            state="BAJO" if value<low else ("ALTO" if value>=high else "MEDIO")
            lines.append(f"{label:<18} {value:>3}  {state}")
        indicator("Ramp",roles["Ramp"],7,11)
        indicator("Robo",roles["Robo"]+roles["Ventaja/selección"],7,11)
        indicator("Interacción",a["interaction"],6,10)
        indicator("Protección",roles["Protección"],3,6)

        lines += ["","BASE DE MANÁ — demanda vs fuentes estimadas"]
        names={"W":"Blanco","U":"Azul","B":"Negro","R":"Rojo","G":"Verde"}
        total_need=sum(need.values()); total_sources=sum(sources.values())
        for c in "WUBRG":
            if not (need[c] or sources[c]):
                continue
            need_pct=(100*need[c]/total_need) if total_need else 0
            source_pct=(100*sources[c]/total_sources) if total_sources else 0
            warn="  ⚠" if c in a["mana_warnings"] else ""
            lines.append(
                f"{names[c]:<8} símbolos {need[c]:>3} ({need_pct:4.0f}%) · "
                f"fuentes {sources[c]:>3} ({source_pct:4.0f}%){warn}"
            )

        if a["mana_warnings"]:
            colors=", ".join(names[c] for c in a["mana_warnings"])
            lines += ["",f"⚠ Revisa la base de maná: parecen faltar fuentes relativas de {colors}."]

        notable=[]
        for role in ["Fast mana","Tutor","Turno extra","Wincon explícita","MLD"]:
            cards=sorted(set(a["role_cards"].get(role,[])))
            if cards:
                notable.append((role,cards))
        if notable:
            lines += ["","CARTAS DESTACADAS POR EL ANALIZADOR"]
            for role,cards in notable:
                lines.append(f"{role}: "+", ".join(cards))

        lines += [
            "",
            "Nota: el análisis usa reglas heurísticas sobre el texto Oracle. Sirve como diagnóstico rápido,",
            "pero algunas cartas cumplen funciones solo por contexto o por interacción con otras cartas."
        ]
        self._set_text(self.stats_text,"\n".join(lines))

    def _refresh_validation(self, rows):
        if not rows:
            self._set_text(self.validation_text,"Añade cartas para validar el mazo."); return
        issues=[]
        total=sum(r["quantity"] for r in rows)
        commanders=[r for r in rows if r["is_commander"]]
        cmd_identity=set()
        for r in commanders: cmd_identity.update((r["color_identity"] or "").split())

        if total != 100: issues.append(f"✗ El mazo tiene {total} cartas; Commander normalmente requiere 100.")
        else: issues.append("✓ Tiene 100 cartas.")

        if not commanders:
            issues.append("✗ No hay comandante marcado.")
        elif len(commanders)>2:
            issues.append("✗ Hay más de dos comandantes marcados.")
        elif len(commanders)==2:
            texts=[(r["oracle_text"] or "").lower() for r in commanders]
            types=[(r["type_line"] or "").lower() for r in commanders]
            pair_ok=(
                all(("partner" in t or "friends forever" in t) for t in texts) or
                (("choose a background" in texts[0] and "background" in types[1]) or ("choose a background" in texts[1] and "background" in types[0])) or
                ("doctor's companion" in texts[0] or "doctor's companion" in texts[1])
            )
            issues.append(("✓" if pair_ok else "⚠")+" Dos comandantes: "+("veo una regla compatible de pareja." if pair_ok else "revisa Partner/Background/Doctor's Companion/Friends Forever."))
        else:
            issues.append("✓ Un comandante marcado.")

        illegal=[r["name"] for r in rows if not r["commander_legal"]]
        if illegal: issues.append("✗ No legales en Commander: " + ", ".join(illegal))
        else: issues.append("✓ Todas las cartas registradas figuran legales en Commander.")

        color_bad=[]
        if commanders:
            for r in rows:
                if r["is_commander"]: continue
                ident=set((r["color_identity"] or "").split())
                if not ident.issubset(cmd_identity):
                    color_bad.append(r["name"])
            if color_bad: issues.append("✗ Fuera de la identidad de color: " + ", ".join(color_bad))
            else: issues.append("✓ Identidad de color compatible.")

        singleton=[]
        by_oracle={}
        for r in rows:
            key=r["oracle_id"] or r["name"]
            by_oracle.setdefault(key,[]).append(r)
        for entries in by_oracle.values():
            qty=sum(x["quantity"] for x in entries)
            sample=entries[0]
            tl=(sample["type_line"] or "").lower()
            oracle=(sample["oracle_text"] or "").lower()
            exempt=("basic land" in tl or "tierra básica" in tl or "a deck can have" in oracle)
            if qty>1 and not exempt:
                singleton.append(f"{sample['name']} ×{qty}")
        if singleton: issues.append("✗ Posibles infracciones de singleton: " + ", ".join(singleton))
        else: issues.append("✓ No veo infracciones evidentes de singleton.")

        missing=[]
        for r in rows:
            available=max(0, r["owned"]-r["used_elsewhere"])
            if r["quantity"]>available:
                missing.append(f"{r['name']}: faltan {r['quantity']-available}")
        if missing: issues.append("✗ Copias físicas insuficientes: " + "; ".join(missing))
        else: issues.append("✓ Tienes suficientes copias físicas para este mazo.")

        issues += ["","\nNota: la validación automática ayuda a detectar problemas comunes, pero no sustituye todas las reglas especiales de cartas o comandantes."]
        self._set_text(self.validation_text,"\n".join(issues))

    def add_to_deck(self):
        did=self.current_deck_id()
        if did is None:
            messagebox.showerror("Sin mazo","Crea o selecciona un mazo primero.")
            return
        q=self.deck_card_query.get().strip()
        if not q:return
        matches=self.db.find_collection_cards(q,limit=30)
        if not matches:
            messagebox.showerror("No está en la colección","No encontré esa carta en tu colección.")
            return
        chosen=matches[0]
        if len(matches)>1:
            names="\n".join(f"{i+1}. {r['name']} — {r['set_code'].upper()} {r['collector_number']} · {self._lang_name(r['lang'])} · libres {self._free_qty_for_collection_row(r)} · {self._assignment_text(r)}" for i,r in enumerate(matches))
            n=simpledialog.askinteger("Elige copia","Hay varias coincidencias:\n\n"+names+"\n\nNúmero:",minvalue=1,maxvalue=len(matches))
            if not n:return
            chosen=matches[n-1]
        qty=max(1,int(self.deck_qty.get() or 1))
        if self._deck_status(did)=="Desarmado":
            self._plan_add(did,chosen["id"],qty,bool(self.as_commander.get()))
            self.deck_card_query.set(""); self.as_commander.set(False)
            self.refresh_deck_cards(); self.status_var.set("Añadida al plan del mazo (sin reservar copia física).")
            return
        state=self._capture_undo_state([chosen["id"]])
        try:
            result=self._assign_existing_copies(did,chosen,qty,bool(self.as_commander.get()))
            if result is False:return
            self._push_undo(f"{chosen['name']} → {self.db.get_deck(did)['name']}",state)
            self.deck_card_query.set(""); self.as_commander.set(False)
            self.refresh_deck_cards(); self.refresh_collection()
            self.status_var.set(f"Añadida al mazo: {chosen['name']}")
        except Exception as e:
            messagebox.showerror("No se pudo añadir",str(e))


    def remove_from_deck(self):
        info=self._selected_deck_info()
        if not info:return
        state=self._capture_undo_state([r["id"] for r in info["rows"]])
        try:
            self._deck_remove_one_info(info)
            self._push_undo(f"Quitar 1 × {info['name']}",state)
            self.refresh_deck_cards(); self.refresh_collection()
            self.status_var.set(f"1 × {info['name']} devuelta a disponibles.")
        except Exception as e:messagebox.showerror("Quitar del mazo",str(e))

    def _deck_lang_code(self):
        return {
            "Inglés":"en","Español":"es","Portugués":"pt","Francés":"fr",
            "Italiano":"it","Alemán":"de","Japonés":"ja","Otro":"und"
        }.get(self.deck_lang.get(), "en")

    def _deck_finish_code(self):
        return {"Normal":"nonfoil","Foil":"foil","Etched":"etched"}.get(self.deck_finish.get(),"nonfoil")

    def add_direct_to_deck(self):
        did=self.current_deck_id()
        if did is None:
            messagebox.showerror("Sin mazo","Crea o selecciona un mazo primero.")
            return
        code=self.deck_set_code.get().strip().lower(); number=self.deck_collector_number.get().strip()
        if not code or not number:
            messagebox.showerror("Faltan datos","Escribe edición y número.")
            return
        qty=max(1,int(self.deck_qty.get() or 1)); lang=self._deck_lang_code(); finish=self._deck_finish_code()
        try:
            undo_state=None
            card=self.scry.get_by_set_number(code,number)
            exact=self.db.find_exact_variant(card["id"],lang,finish,self._detect_treatment(card))
            is_cmd=bool(self.as_commander.get())
            if exact:
                undo_state=self._capture_undo_state([exact["id"]])
                free=self._free_qty_for_collection_row(exact)
                if free>=qty:
                    self.db.add_to_deck(did,exact["id"],qty,is_cmd)
                else:
                    short=qty-free
                    answer=messagebox.askyesnocancel("Copia ocupada",
                        f"Esta impresión ya está registrada, pero solo hay {free} copia(s) libre(s).\n\n"
                        f"Para añadir {qty}, faltan {short}.\n\n"
                        "Sí: registrar esas copias como nuevas cartas físicas.\n"
                        "No: intentar mover copias desde otros mazos.\n"
                        "Cancelar: no hacer cambios.")
                    if answer is None:return
                    if answer:
                        self.db.add_card(card,qty=short,lang=lang,finish=finish,treatment=self._detect_treatment(card))
                        self.db.add_to_deck(did,exact["id"],qty,is_cmd)
                    else:
                        result=self._assign_existing_copies(did,exact,qty,is_cmd)
                        if result is False:return
            else:
                # Entrada directa representa una carta física nueva: se registra y se asigna.
                cid=self.db.add_card(card,qty=qty,lang=lang,finish=finish,treatment=self._detect_treatment(card))
                undo_state={int(cid):None}
                self.db.add_to_deck(did,cid,qty,is_cmd)
            self._push_undo(f"Agregar {card['name']} al mazo",undo_state)
            self.deck_collector_number.set("")
            if self.deck_keep_set.get():self.deck_number_entry.focus_set()
            else:self.deck_set_code.set(""); self.deck_set_entry.focus_set()
            self.as_commander.set(False)
            self.refresh_deck_cards(); self.refresh_collection()
            self.status_var.set(f"Agregada al mazo: {card['name']}")
        except Exception as e:messagebox.showerror("No se pudo agregar",str(e))

    def _plain_deck_text(self, did):
        rows=list(self._deck_rows(did)); commanders=[r for r in rows if r["is_commander"]]; main=[r for r in rows if not r["is_commander"]]; lines=[]
        if commanders:
            lines.append("Commander:")
            for r in sorted(commanders,key=lambda x:(x["name"] or "").lower()): lines.append(f"{r['quantity']} {r['name']}")
            lines.append("")
        units=self._deck_display_units(main); cats={}
        for unit in units: cats.setdefault(self._deck_category(unit["sample"]),[]).append(unit)
        for cat in self._deck_section_order():
            if cat=="Commander":continue
            group=cats.get(cat,[])
            if not group:continue
            lines.append(cat+":")
            for unit in sorted(group,key=lambda u:u["name"].lower()):
                qty=sum(int(r["quantity"] or 0) for r in unit["rows"]); lines.append(f"{qty} {unit['name']}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def copy_plain_deck(self):
        did = self.current_deck_id()
        if did is None:
            return
        text = self._plain_deck_text(did)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.status_var.set("Lista genérica copiada al portapapeles.")

    def _moxfield_text(self, did):
        rows=list(self._deck_rows(did)); commanders=[r for r in rows if r["is_commander"]]; main=[r for r in rows if not r["is_commander"]]; lines=[]
        if commanders:
            lines.append("Commander:")
            for r in sorted(commanders,key=lambda x:(x["name"] or "").lower()): lines.append(f"{r['quantity']} {r['name']} ({r['set_code'].upper()}) {r['collector_number']}")
            lines.append("")
        cats={}
        for r in main: cats.setdefault(self._deck_category(r),[]).append(r)
        for cat in self._deck_section_order():
            if cat=="Commander":continue
            group=cats.get(cat,[])
            if not group:continue
            lines.append(cat+":")
            for r in sorted(group,key=lambda x:(x["name"] or "").lower()): lines.append(f"{r['quantity']} {r['name']} ({r['set_code'].upper()}) {r['collector_number']}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def copy_moxfield(self):
        did=self.current_deck_id()
        if did is None:return
        text=self._moxfield_text(did)
        self.clipboard_clear(); self.clipboard_append(text); self.update()
        self.status_var.set("Lista copiada al portapapeles para pegar en Moxfield.")

    def export_moxfield(self):
        did=self.current_deck_id()
        if did is None:return
        deck=self.db.get_deck(did)
        path=filedialog.asksaveasfilename(defaultextension=".txt",filetypes=[("Texto","*.txt")],initialfile=f"{deck['name']}_Moxfield.txt")
        if path:
            Path(path).write_text(self._moxfield_text(did),encoding="utf-8")
            messagebox.showinfo("Exportado","Lista para Moxfield exportada.")

    def show_missing(self):
        did=self.current_deck_id()
        if did is None:return
        rows=self._deck_rows(did)
        no_owned=[]; occupied=[]; short=[]
        for r in rows:
            need=int(r["quantity"] or 0); owned=int(r["owned"] or 0); elsewhere=int(r["used_elsewhere"] or 0)
            if owned<=0:no_owned.append(f"{need} {r['name']}")
            elif owned-elsewhere<=0:occupied.append(f"{need} {r['name']} — todas las copias están en otros mazos")
            elif need>owned-elsewhere:short.append(f"{need-(owned-elsewhere)} {r['name']} — faltan copias libres")
        if not (no_owned or occupied or short):
            messagebox.showinfo("Lista faltante","Tienes todas las copias necesarias.");return
        parts=[]
        if no_owned:parts+=["NO TIENES:"]+no_owned+[""]
        if occupied:parts+=["TIENES, PERO ESTÁN OCUPADAS:"]+occupied+[""]
        if short:parts+=["CANTIDAD INSUFICIENTE:"]+short
        out="\n".join(parts).strip()
        self.clipboard_clear(); self.clipboard_append(out); self.update()
        messagebox.showinfo("Lista faltante",out+"\n\nLa lista quedó copiada al portapapeles.")

    def _assignment_text(self, row):
        uses = self.db.card_usage(row["id"])
        used = sum(u["quantity"] for u in uses)
        free = max(0, row["quantity"] - used)
        if not uses:
            return f"{free} disponible(s)"
        names = ", ".join(f"{u['deck_name']} ×{u['quantity']}" for u in uses)
        if free > 0:
            return f"{free} disponible(s) · {used} en mazo(s): {names}"
        return f"0 disponibles · {used} en mazo(s): {names}"

    def _ensure_plan_placeholder(self,card,qty,did,is_commander=False):
        name=card.get("name","").strip()
        with self.db.con() as c:
            row=c.execute("SELECT * FROM collection WHERE quantity=0 AND lower(name)=lower(?) ORDER BY id LIMIT 1",(name,)).fetchone()
        if row: cid=int(row["id"])
        else:
            cid=self.db.add_card(card,1,"und","nonfoil","Plan / Faltante")
            with self.db.con() as c: c.execute("UPDATE collection SET quantity=0 WHERE id=?",(cid,))
        self._plan_add(did,cid,qty,is_commander)
        return cid

    def _missing_plan_targets_for_name(self,name):
        out=[]
        with self.db.con() as c:
            for d in c.execute("SELECT id,name FROM decks ORDER BY name COLLATE NOCASE").fetchall():
                did=int(d["id"])
                need=int(c.execute("""SELECT COALESCE(SUM(dp.quantity),0) n FROM deck_plan dp JOIN collection col ON col.id=dp.collection_id WHERE dp.deck_id=? AND lower(col.name)=lower(?)""",(did,name)).fetchone()["n"] or 0)
                have=int(c.execute("""SELECT COALESCE(SUM(dc.quantity),0) n FROM deck_cards dc JOIN collection col ON col.id=dc.collection_id WHERE dc.deck_id=? AND lower(col.name)=lower(?)""",(did,name)).fetchone()["n"] or 0)
                if need>have: out.append((did,d["name"],need-have))
        return out

    def _auto_assign_new_physical_card(self,cid,card_name,qty):
        remaining=int(qty or 0)
        while remaining>0:
            targets=self._missing_plan_targets_for_name(card_name)
            if not targets: break
            if len(targets)==1: did,deck_name,missing=targets[0]
            else:
                labels="\n".join(f"{i+1}. {name} — faltan {missing}" for i,(_,name,missing) in enumerate(targets))
                pick=simpledialog.askinteger("Carta necesaria en varios mazos",f"{card_name} falta en más de un mazo:\n\n{labels}\n\n¿A cuál quieres asignar esta copia?",minvalue=1,maxvalue=len(targets))
                if not pick: break
                did,deck_name,missing=targets[pick-1]
            take=min(remaining,missing)
            if self._deck_status(did)!="Desarmado": self.db.add_to_deck(did,cid,take,False)
            with self.db.con() as c:
                ps=c.execute("""SELECT dp.rowid rid,dp.quantity,dp.is_commander FROM deck_plan dp JOIN collection col ON col.id=dp.collection_id WHERE dp.deck_id=? AND lower(col.name)=lower(?) AND col.quantity=0 ORDER BY dp.is_commander DESC,dp.rowid""",(did,card_name)).fetchall()
                left=take
                for p in ps:
                    if left<=0: break
                    use=min(left,int(p["quantity"])); newq=int(p["quantity"])-use
                    if newq: c.execute("UPDATE deck_plan SET quantity=? WHERE rowid=?",(newq,p["rid"]))
                    else: c.execute("DELETE FROM deck_plan WHERE rowid=?",(p["rid"],))
                    ex=c.execute("SELECT quantity FROM deck_plan WHERE deck_id=? AND collection_id=? AND is_commander=?",(did,cid,int(p["is_commander"]))).fetchone()
                    if ex: c.execute("UPDATE deck_plan SET quantity=quantity+? WHERE deck_id=? AND collection_id=? AND is_commander=?",(use,did,cid,int(p["is_commander"])))
                    else: c.execute("INSERT INTO deck_plan(deck_id,collection_id,quantity,is_commander) VALUES (?,?,?,?)",(did,cid,use,int(p["is_commander"])))
                    left-=use
            remaining-=take
            self.status_var.set(f"{card_name}: asignada automáticamente a {deck_name}.")

    def _parse_import_lines(self,raw):
        parsed=[]; current_commander=False
        for line in [x.strip() for x in raw.splitlines() if x.strip()]:
            low=line.lower().rstrip(":")
            if low in {"commander","commanders","comandante","comandantes"}:current_commander=True;continue
            if low in {"deck","mainboard","mazo","lista","creatures","criaturas","artifacts","artefactos","enchantments","encantamientos",
                       "instants","instantáneos","sorceries","conjuros","lands","tierras","planeswalkers","battles","batallas"}:
                current_commander=False;continue
            m=re.match(r"^\s*(\d+)\s+(.+?)\s*$",line)
            if not m:continue
            qty=int(m.group(1)); rest=m.group(2).strip(); set_code=None; collector_number=None
            m2=re.match(r"^(.*?)\s+\(([A-Za-z0-9]+)\)\s+([A-Za-z0-9\-]+)\s*$",rest)
            if m2:name=m2.group(1).strip(); set_code=m2.group(2).strip(); collector_number=m2.group(3).strip()
            else:name=rest
            parsed.append({"line":line,"qty":qty,"name":name,"set":set_code,"number":collector_number,"commander":current_commander})
        return parsed

    def import_bulk_deck(self):
        did=self.current_deck_id()
        if did is None: messagebox.showerror("Sin mazo","Crea o selecciona un mazo primero."); return
        raw=self.bulk_deck_text.get("1.0","end").strip()
        if not raw:return
        parsed=self._parse_import_lines(raw)
        if not parsed: messagebox.showerror("Importación","No pude reconocer cartas en la lista."); return
        total=sum(int(x["qty"]) for x in parsed); enough=0
        for item in parsed:
            matches=self.db.find_collection_cards_exact(item["name"]); free=sum(max(0,self._free_qty_for_collection_row(r)) for r in matches)
            if free>=item["qty"]: enough+=1
        msg=(f"Se detectaron {len(parsed)} líneas / {total} cartas.\n\n"
             f"Con copias físicas suficientes: {enough}\n"
             f"Faltantes o incompletas: {len(parsed)-enough}\n\n"
             "La edición de Moxfield será solo una referencia.\n"
             "El mazo se guardará COMPLETO por nombre aunque todavía no tengas todas las cartas.\n\n"
             "¿Importar el mazo?")
        if not messagebox.askyesno("Previsualización de importación",msg): return
        imported=0; errors=[]
        for item in parsed:
            remaining=int(item["qty"]); matches=list(self.db.find_collection_cards_exact(item["name"]))
            if item["set"] and item["number"]:
                exact=[r for r in matches if (r["set_code"] or "").lower()==item["set"].lower() and str(r["collector_number"])==str(item["number"])]
                matches=exact+[r for r in matches if r not in exact]
            for chosen in matches:
                if remaining<=0: break
                free=self._free_qty_for_collection_row(chosen)
                if free<=0: continue
                take=min(remaining,free); self._plan_add(did,chosen["id"],take,bool(item["commander"]))
                if self._deck_status(did)!="Desarmado": self.db.add_to_deck(did,chosen["id"],take,bool(item["commander"]))
                remaining-=take
            if remaining>0:
                try: self._ensure_plan_placeholder(self.scry.get_by_name(item["name"]),remaining,did,bool(item["commander"]))
                except Exception as e: errors.append(f"{item['name']}: {e}"); continue
            imported+=1
        self.bulk_deck_text.delete("1.0","end"); self.refresh_deck_cards(); self.refresh_collection()
        msg=f"Mazo importado: {imported}/{len(parsed)} líneas · {total} cartas planificadas."
        if errors: msg+="\n\nNo pude registrar:\n"+"\n".join(errors[:20])
        else: msg+="\n\nLas cartas que no posees quedaron como faltantes."
        messagebox.showinfo("Importación terminada",msg)



if __name__ == "__main__":
    import traceback
    try:
        app = App()
        if ctk is not None and app.winfo_exists():
            app.mainloop()
    except Exception:
        err = traceback.format_exc()
        try:
            Path(__file__).resolve().with_name("ERROR_V3_2C.txt").write_text(err, encoding="utf-8")
        except Exception:
            pass
        print("\n=== ERROR AL INICIAR MTG ORGANIZER V3.2c ===\n")
        print(err)
        print("\nTambién se guardó como ERROR_V3_2C.txt en la carpeta del programa.")
        try:
            input("\nPresiona ENTER para cerrar...")
        except Exception:
            pass
