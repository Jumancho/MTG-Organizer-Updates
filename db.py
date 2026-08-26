from __future__ import annotations
import sqlite3, csv
from pathlib import Path

class Database:
    def __init__(self, path):
        self.path=str(path)
        self._init()

    def con(self):
        c=sqlite3.connect(self.path)
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _columns(self, c, table):
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]

    def _init(self):
        with self.con() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS movement_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL,
                card_name TEXT NOT NULL,
                set_code TEXT,
                collector_number TEXT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                moved_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS deck_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                is_commander INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
            );
            """)

            exists=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='collection'").fetchone()
            if not exists:
                self._create_collection(c)
            else:
                cols=self._columns(c,"collection")
                # Migra la base antigua para permitir idioma/acabado y guardar valor de maná.
                if "finish" not in cols or "mana_value" not in cols or "treatment" not in cols:
                    self._migrate_collection(c, cols)

            # V1.9: guarda el color real de la carta aparte de su identidad de color.
            cols=self._columns(c,"collection")
            if "colors" not in cols:
                c.execute("ALTER TABLE collection ADD COLUMN colors TEXT DEFAULT ''")
                rows=c.execute("SELECT id,mana_cost FROM collection").fetchall()
                for r in rows:
                    mana=(r["mana_cost"] or "").upper()
                    found=[x for x in "WUBRG" if ("{"+x+"}") in mana or ("/"+x+"}") in mana or ("{"+x+"/") in mana]
                    c.execute("UPDATE collection SET colors=? WHERE id=?", (" ".join(found), r["id"]))

            self._merge_duplicate_variants(c)
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_collection_variant ON collection(scryfall_id, lang, finish, treatment)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_deck_variant ON deck_cards(deck_id, collection_id, is_commander)")

    def _merge_duplicate_variants(self, c):
        """Merge rows representing the same exact physical variant."""
        rows = c.execute("""
            SELECT scryfall_id, lang, finish, treatment,
                   GROUP_CONCAT(id) AS ids, SUM(quantity) AS total_qty, COUNT(*) AS n
            FROM collection
            GROUP BY scryfall_id, lang, finish, treatment
            HAVING COUNT(*) > 1
        """).fetchall()
        for r in rows:
            ids = [int(x) for x in r["ids"].split(",")]
            keep = ids[0]
            others = ids[1:]
            c.execute("UPDATE collection SET quantity=? WHERE id=?", (r["total_qty"], keep))
            for oid in others:
                # Re-point deck assignments to the surviving variant.
                deck_rows = c.execute("SELECT * FROM deck_cards WHERE collection_id=?", (oid,)).fetchall()
                for dr in deck_rows:
                    existing = c.execute("""
                        SELECT id, quantity FROM deck_cards
                        WHERE deck_id=? AND collection_id=? AND is_commander=?
                    """, (dr["deck_id"], keep, dr["is_commander"])).fetchone()
                    if existing:
                        c.execute("UPDATE deck_cards SET quantity=quantity+? WHERE id=?",
                                  (dr["quantity"], existing["id"]))
                        c.execute("DELETE FROM deck_cards WHERE id=?", (dr["id"],))
                    else:
                        c.execute("UPDATE deck_cards SET collection_id=? WHERE id=?", (keep, dr["id"]))
                c.execute("UPDATE movement_history SET collection_id=? WHERE collection_id=?", (keep, oid))
                c.execute("DELETE FROM collection WHERE id=?", (oid,))

    def _create_collection(self,c):
        c.execute("""
        CREATE TABLE collection (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scryfall_id TEXT NOT NULL,
            oracle_id TEXT,
            name TEXT NOT NULL,
            set_code TEXT,
            set_name TEXT,
            collector_number TEXT,
            lang TEXT NOT NULL DEFAULT 'en',
            finish TEXT NOT NULL DEFAULT 'nonfoil',
            treatment TEXT NOT NULL DEFAULT 'Normal',
            quantity INTEGER NOT NULL DEFAULT 1,
            mana_cost TEXT,
            mana_value REAL DEFAULT 0,
            type_line TEXT,
            oracle_text TEXT,
            colors TEXT,
            color_identity TEXT,
            commander_legal INTEGER NOT NULL DEFAULT 0
        )""")

    def _migrate_collection(self,c,cols):
        c.execute("ALTER TABLE collection RENAME TO collection_old")
        self._create_collection(c)
        def has(x): return x in cols
        fields=["scryfall_id","oracle_id","name","set_code","set_name","collector_number","lang","quantity",
                "mana_cost","type_line","oracle_text","colors","color_identity","commander_legal"]
        available=[f for f in fields if has(f)]
        select=", ".join(available)
        oldrows=c.execute(f"SELECT {select} FROM collection_old").fetchall()
        for r in oldrows:
            d=dict(r)
            c.execute("""
            INSERT INTO collection
            (scryfall_id,oracle_id,name,set_code,set_name,collector_number,lang,finish,treatment,quantity,mana_cost,mana_value,
             type_line,oracle_text,colors,color_identity,commander_legal)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,(d.get("scryfall_id"),d.get("oracle_id"),d.get("name"),d.get("set_code"),d.get("set_name"),
                 d.get("collector_number"),d.get("lang") or "en","nonfoil",d.get("treatment") or "Normal",d.get("quantity") or 1,d.get("mana_cost") or "",
                 0,d.get("type_line") or "",d.get("oracle_text") or "",d.get("colors") or "",d.get("color_identity") or "",d.get("commander_legal") or 0))
        c.execute("DROP TABLE collection_old")

    def add_card(self,card,qty=1,lang="en",finish="nonfoil",treatment="Normal"):
        colors=" ".join(card.get("colors") or [])
        ci=" ".join(card.get("color_identity") or [])
        legal=1 if card.get("legalities",{}).get("commander")=="legal" else 0
        with self.con() as c:
            row=c.execute("SELECT id FROM collection WHERE scryfall_id=? AND lang=? AND finish=? AND treatment=?",
                          (card["id"],lang,finish,treatment)).fetchone()
            if row:
                c.execute("UPDATE collection SET quantity=quantity+? WHERE id=?",(qty,row["id"]))
                return row["id"]
            cur=c.execute("""
            INSERT INTO collection
            (scryfall_id,oracle_id,name,set_code,set_name,collector_number,lang,finish,treatment,quantity,mana_cost,mana_value,
             type_line,oracle_text,colors,color_identity,commander_legal)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,(card["id"],card.get("oracle_id"),card["name"],card.get("set",""),card.get("set_name",""),
                 card.get("collector_number",""),lang,finish,treatment,qty,card.get("mana_cost",""),float(card.get("cmc") or 0),
                 card.get("type_line",""),card.get("oracle_text","") or card.get("printed_text",""),colors,ci,legal))
            return cur.lastrowid

    def list_collection(self,q=""):
        with self.con() as c:
            if q:
                p=f"%{q}%"
                return c.execute("""SELECT * FROM collection WHERE quantity>0 AND (name LIKE ? OR set_code LIKE ? OR collector_number LIKE ?)
                                  ORDER BY name COLLATE NOCASE,set_code,collector_number""",(p,p,p)).fetchall()
            return c.execute("SELECT * FROM collection WHERE quantity>0 ORDER BY name COLLATE NOCASE,set_code,collector_number").fetchall()

    def find_collection_cards(self,q,limit=20):
        with self.con() as c:
            p=f"%{q}%"
            return c.execute("""SELECT * FROM collection
                WHERE quantity>0 AND (name LIKE ? OR (set_code || ' ' || collector_number) LIKE ?)
                ORDER BY CASE WHEN lower(name)=lower(?) THEN 0 ELSE 1 END,name COLLATE NOCASE LIMIT ?""",
                (p,p,q,limit)).fetchall()

    def remove_one(self,cid):
        with self.con() as c:
            r=c.execute("SELECT quantity FROM collection WHERE id=?",(cid,)).fetchone()
            if not r:return
            if r["quantity"]>1:c.execute("UPDATE collection SET quantity=quantity-1 WHERE id=?",(cid,))
            else:
                used=c.execute("SELECT COUNT(*) n FROM deck_cards WHERE collection_id=?",(cid,)).fetchone()["n"]
                if used:return
                c.execute("DELETE FROM collection WHERE id=?",(cid,))

    def export_collection_csv(self,path):
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f); w.writerow(["Cantidad","Carta","Edición","Código","Número","Idioma","Acabado","Versión","Tipo","Color","Identidad","Commander"])
            for r in self.list_collection():
                w.writerow([r["quantity"],r["name"],r["set_name"],r["set_code"].upper(),r["collector_number"],r["lang"],
                            r["finish"],r["treatment"],r["type_line"],r["colors"],r["color_identity"],"Sí" if r["commander_legal"] else "No"])

    def create_deck(self,name):
        with self.con() as c:c.execute("INSERT INTO decks(name) VALUES (?)",(name,))
    def list_decks(self):
        with self.con() as c:return c.execute("SELECT * FROM decks ORDER BY name COLLATE NOCASE").fetchall()
    def get_deck(self,did):
        with self.con() as c:return c.execute("SELECT * FROM decks WHERE id=?",(did,)).fetchone()
    def delete_deck(self,did):
        with self.con() as c:c.execute("DELETE FROM decks WHERE id=?",(did,))
    def add_to_deck(self,did,cid,qty=1,is_commander=False):
        with self.con() as c:
            r=c.execute("SELECT id FROM deck_cards WHERE deck_id=? AND collection_id=? AND is_commander=?",
                        (did,cid,1 if is_commander else 0)).fetchone()
            if r:c.execute("UPDATE deck_cards SET quantity=quantity+? WHERE id=?",(qty,r["id"]))
            else:c.execute("INSERT INTO deck_cards(deck_id,collection_id,quantity,is_commander) VALUES (?,?,?,?)",
                           (did,cid,qty,1 if is_commander else 0))
    def deck_cards(self,did):
        with self.con() as c:
            return c.execute("""
            SELECT dc.id deck_card_id,dc.quantity,dc.is_commander,
                   col.*,col.quantity owned,
                   COALESCE((SELECT SUM(dc2.quantity) FROM deck_cards dc2
                             WHERE dc2.collection_id=col.id AND dc2.deck_id<>?),0) used_elsewhere
            FROM deck_cards dc JOIN collection col ON col.id=dc.collection_id
            WHERE dc.deck_id=? ORDER BY dc.is_commander DESC,col.name COLLATE NOCASE
            """,(did,did)).fetchall()

    def get_collection_card(self, cid):
        with self.con() as c:
            return c.execute("SELECT * FROM collection WHERE id=?", (cid,)).fetchone()

    def find_collection_cards_exact(self, name, set_code=None, collector_number=None):
        with self.con() as c:
            if set_code and collector_number:
                return c.execute("""
                    SELECT * FROM collection
                    WHERE quantity>0 AND lower(name)=lower(?) AND lower(set_code)=lower(?) AND collector_number=?
                    ORDER BY quantity DESC
                """, (name, set_code, str(collector_number))).fetchall()
            return c.execute("""
                SELECT * FROM collection
                WHERE quantity>0 AND lower(name)=lower(?)
                ORDER BY quantity DESC, set_code, collector_number
            """, (name,)).fetchall()

    def card_usage(self, collection_id):
        with self.con() as c:
            return c.execute("""
                SELECT d.name AS deck_name, SUM(dc.quantity) AS quantity
                FROM deck_cards dc
                JOIN decks d ON d.id=dc.deck_id
                WHERE dc.collection_id=?
                GROUP BY d.id, d.name
                ORDER BY d.name COLLATE NOCASE
            """, (collection_id,)).fetchall()


    def find_exact_variant(self, scryfall_id, lang, finish, treatment=None):
        with self.con() as c:
            if treatment is not None:
                return c.execute("""
                    SELECT * FROM collection
                    WHERE scryfall_id=? AND lang=? AND finish=? AND treatment=?
                    ORDER BY id LIMIT 1
                """, (scryfall_id, lang, finish, treatment)).fetchone()
            return c.execute("""
                SELECT * FROM collection
                WHERE scryfall_id=? AND lang=? AND finish=?
                ORDER BY id LIMIT 1
            """, (scryfall_id, lang, finish)).fetchone()

    def get_collection_card(self, cid):
        with self.con() as c:
            return c.execute("SELECT * FROM collection WHERE id=?", (cid,)).fetchone()

    def card_usage(self, collection_id):
        with self.con() as c:
            return c.execute("""
                SELECT d.id AS deck_id, d.name AS deck_name, SUM(dc.quantity) AS quantity
                FROM deck_cards dc
                JOIN decks d ON d.id=dc.deck_id
                WHERE dc.collection_id=?
                GROUP BY d.id,d.name
                ORDER BY d.name COLLATE NOCASE
            """, (collection_id,)).fetchall()

    def available_collection(self):
        with self.con() as c:
            return c.execute("""
                SELECT col.*,
                       col.quantity - COALESCE((
                           SELECT SUM(dc.quantity)
                           FROM deck_cards dc
                           WHERE dc.collection_id=col.id
                       ),0) AS free_qty
                FROM collection col
                WHERE (col.quantity - COALESCE((
                           SELECT SUM(dc.quantity)
                           FROM deck_cards dc
                           WHERE dc.collection_id=col.id
                       ),0)) > 0
                ORDER BY col.name COLLATE NOCASE,col.set_code,col.collector_number
            """).fetchall()

    def _deck_id_by_name(self, c, name):
        if name == "DISPONIBLE":
            return None
        row = c.execute("SELECT id FROM decks WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    def move_one_copy(self, collection_id, source_name, target_name):
        with self.con() as c:
            card = c.execute("SELECT * FROM collection WHERE id=?", (collection_id,)).fetchone()
            if not card:
                raise ValueError("No encontré la carta.")

            source_id = self._deck_id_by_name(c, source_name)
            target_id = self._deck_id_by_name(c, target_name)

            used_total = c.execute("SELECT COALESCE(SUM(quantity),0) n FROM deck_cards WHERE collection_id=?",
                                   (collection_id,)).fetchone()["n"]
            free = card["quantity"] - used_total

            if source_name == "DISPONIBLE":
                if free < 1:
                    raise ValueError("No hay copias disponibles para mover.")
            else:
                row = c.execute("""
                    SELECT * FROM deck_cards
                    WHERE deck_id=? AND collection_id=?
                    ORDER BY is_commander DESC LIMIT 1
                """, (source_id, collection_id)).fetchone()
                if not row or row["quantity"] < 1:
                    raise ValueError("Esa copia no está en el mazo de origen.")
                if row["quantity"] == 1:
                    c.execute("DELETE FROM deck_cards WHERE id=?", (row["id"],))
                else:
                    c.execute("UPDATE deck_cards SET quantity=quantity-1 WHERE id=?", (row["id"],))

            if target_name != "DISPONIBLE":
                row = c.execute("""
                    SELECT * FROM deck_cards
                    WHERE deck_id=? AND collection_id=? AND is_commander=0
                """, (target_id, collection_id)).fetchone()
                if row:
                    c.execute("UPDATE deck_cards SET quantity=quantity+1 WHERE id=?", (row["id"],))
                else:
                    c.execute("""
                        INSERT INTO deck_cards(deck_id,collection_id,quantity,is_commander)
                        VALUES (?,?,1,0)
                    """, (target_id, collection_id))

            c.execute("""
                INSERT INTO movement_history
                (collection_id,card_name,set_code,collector_number,from_name,to_name)
                VALUES (?,?,?,?,?,?)
            """, (collection_id, card["name"], card["set_code"], card["collector_number"], source_name, target_name))

    def movement_history(self, limit=200):
        with self.con() as c:
            return c.execute("""
                SELECT * FROM movement_history
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()


    def find_collection_variant(self, name, set_code, collector_number):
        with self.con() as c:
            return c.execute("""
                SELECT * FROM collection
                WHERE quantity>0 AND lower(name)=lower(?) AND lower(set_code)=lower(?) AND collector_number=?
                ORDER BY id LIMIT 1
            """, (name, set_code, str(collector_number))).fetchone()

    def get_deck_card_collection(self, deck_card_id):
        with self.con() as c:
            return c.execute("""
                SELECT col.*
                FROM deck_cards dc
                JOIN collection col ON col.id=dc.collection_id
                WHERE dc.id=?
            """, (deck_card_id,)).fetchone()

    def remove_deck_card(self,dcid):
        with self.con() as c:c.execute("DELETE FROM deck_cards WHERE id=?",(dcid,))
