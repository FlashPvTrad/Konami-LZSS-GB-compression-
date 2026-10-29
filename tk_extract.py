#!/usr/bin/env python3
"""
tk_extract.py - extrait les ressources LZSS de Konami GB Collection Vol.2 (Track'n Field).

Principe : on cherche dans la ROM les appels au decodeur du jeu
  * CALL $23C4  (HL = liste de commandes de 7 octets, voir ci-dessous)
  * CALL $25C3  (HL = source, DE = destination VRAM, BC = longueur de sortie) -> tilemaps
precedes de `LD HL,nn` immediat, et de l'appel de selection de banque ($22B0, $22CC, $22D1...).

Liste de commandes ($23C4) :  [cmd][banque][src lo,hi][dest lo,hi][n_tuiles]
    cmd 0 = fin de liste, 1 = tuiles 2bpp compressees LZSS, 2 = tuiles brutes (copie),
    cmd 3 = entree de blit/sprite (8 ou 7 octets, non extraite)
    taille de sortie = n_tuiles * 16 octets (verifie : la fin du flux compresse tombe
    pile sur la source de l'entree suivante)

Usage : tk_extract.py ROM.gb [dossier_sortie] [--verify]
        --verify : recompresse chaque bloc avec tk_lzss.compress, controle l'aller-retour
                   et compare la taille avec celle du flux d'origine
Sortie : dossier/tiles/*.bin|*.png, dossier/maps/*.bin, dossier/lz/*.lz (flux d'origine),
         dossier/index.csv
"""
import os, sys, struct, zlib, csv
from tk_lzss import decompress, decompress_limited, compress, rom_offset

CALL_LIST = b"\xCD\xC4\x23"      # CALL $23C4
CALL_MAP = b"\xCD\xC3\x25"       # CALL $25C3


def png_gray(path, width, height, pixels):
    """Ecrit un PNG 8 bits niveaux de gris (sans dependance)."""
    raw = b"".join(b"\x00" + bytes(pixels[y * width:(y + 1) * width]) for y in range(height))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def tiles_to_png(path, data, per_row=16):
    """Feuille de tuiles 2bpp Game Boy -> PNG (4 niveaux de gris)."""
    n = len(data) // 16
    if n == 0:
        return
    rows = (n + per_row - 1) // per_row
    W, H = per_row * 8, rows * 8
    pix = bytearray([255]) * (W * H)
    shades = (255, 170, 85, 0)
    for t in range(n):
        tx, ty = (t % per_row) * 8, (t // per_row) * 8
        for y in range(8):
            lo, hi = data[t * 16 + 2 * y], data[t * 16 + 2 * y + 1]
            for x in range(8):
                v = ((hi >> (7 - x)) & 1) << 1 | ((lo >> (7 - x)) & 1)
                pix[(ty + y) * W + tx + x] = shades[v]
    png_gray(path, W, H, pix)


def find_all(rom, pat):
    i = 0
    while True:
        i = rom.find(pat, i)
        if i < 0:
            return
        yield i
        i += 1


class Rom:
    def __init__(self, path):
        self.d = open(path, "rb").read()
        # stubs de selection de banque : F5 3E nn  (PUSH AF ; LD A,nn) entre $22B0 et $22E0
        self.stubs = {}
        for a in range(0x22B0, 0x22E0):
            if self.d[a] == 0xF5 and self.d[a + 1] == 0x3E:
                self.stubs[a] = self.d[a + 2]

    def code_bank(self, off):
        return off // 0x4000

    def bank_before(self, off, window=40):
        """Banque selectionnee par le dernier CALL stub avant `off` (None si introuvable)."""
        for p in range(off - 3, max(0, off - window) - 1, -1):
            if self.d[p] == 0xCD:
                tgt = self.d[p + 1] | (self.d[p + 2] << 8)
                if tgt in self.stubs:
                    return self.stubs[tgt]
        return None

    def window(self, bank, addr):
        o = rom_offset(bank, addr)
        end = o + (0x8000 - addr if addr >= 0x4000 else 0x4000 - addr)
        return self.d[o:end]


def parse_list(rom, bank, addr, maxentries=64):
    """Parse une liste de commandes. Retourne (entrees, valide)."""
    ents = []
    a = addr
    for _ in range(maxentries):
        if a > 0x7FF0 or a < 0x4000:
            return ents, False
        o = rom_offset(bank, a)
        cmd = rom.d[o]
        if cmd == 0:
            return ents, bool(ents)
        if cmd in (1, 2):
            b, src, dst, cnt = rom.d[o + 1], rom.d[o + 2] | rom.d[o + 3] << 8, \
                               rom.d[o + 4] | rom.d[o + 5] << 8, rom.d[o + 6]
            if not (0x15 <= b < 0x40 or b == 0x08) or not (0x4000 <= src < 0x8000) or cnt == 0:
                return ents, False
            ents.append(dict(cmd=cmd, bank=b, src=src, dst=dst, ntiles=cnt, at=a))
            a += 7
        elif cmd == 3:
            flag = rom.d[o + 6]
            ents.append(dict(cmd=3, at=a, raw=rom.d[o:o + 8].hex()))
            a += 8 if flag & 0x80 else 7
        else:
            return ents, False
    return ents, False


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verify = "--verify" in sys.argv
    rom = Rom(args[0])
    out = args[1] if len(args) > 1 else "extract"
    for sub in ("tiles", "maps", "lz"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    rows = []
    seen = set()

    # ------------------------------------------------ listes de commandes ($23C4)
    lists = {}
    for site in find_all(rom.d, CALL_LIST):
        if site < 3 or rom.d[site - 3] != 0x21:                       # LD HL,nn juste avant
            continue
        hl = rom.d[site - 2] | rom.d[site - 1] << 8
        if not 0x4000 <= hl < 0x8000:
            continue
        bank = rom.bank_before(site - 3)
        assumed = False
        if bank is None:
            bank = rom.code_bank(site) if site >= 0x4000 else 0x15
            assumed = True
        ents, ok = parse_list(rom, bank, hl)
        if ok:
            lists[(bank, hl)] = (ents, site, assumed)

    # frontieres connues par banque : un flux ne peut pas empieter sur une autre structure
    bounds = {}
    for (lb, la), (ents, _, _) in lists.items():
        bounds.setdefault(lb, set()).add(la)
        for e in ents:
            bounds.setdefault(lb, set()).add(e["at"])
            if e["cmd"] in (1, 2):
                bounds.setdefault(e["bank"], set()).add(e["src"])

    stats = dict(n=0, orig=0, ours=0, same=0, bad=0, oku=0, oku_same=0, oku_size=0)
    for (lb, la), (ents, site, assumed) in sorted(lists.items()):
        for k, e in enumerate(ents):
            if e["cmd"] == 3:
                rows.append(dict(kind="blit", list="%02X:%04X" % (lb, la), entry=k, note=e["raw"]))
                continue
            key = (e["cmd"], e["bank"], e["src"], e["ntiles"])
            if key in seen:
                continue
            seen.add(key)
            n = e["ntiles"] * 16
            name = "b%02X_%04X_to_%04X" % (e["bank"], e["src"], e["dst"])
            row = dict(kind="tiles_lz" if e["cmd"] == 1 else "tiles_raw", list="%02X:%04X" % (lb, la),
                       entry=k, bank="%02X" % e["bank"], src="%04X" % e["src"], dest_tile="%04X" % e["dst"],
                       ntiles=e["ntiles"], out_bytes=n, note="")
            win = rom.window(e["bank"], e["src"])
            if e["cmd"] == 2:
                data = win[:n]
                row["lz_bytes"] = n
            else:
                nxt_b = min((x for x in bounds.get(e["bank"], ()) if x > e["src"]), default=None)
                limit = (nxt_b - e["src"]) if nxt_b else None
                data, used, ok = decompress_limited(win, n, 0, limit)
                row["lz_bytes"], row["src_end"] = used, "%04X" % (e["src"] + used)
                if not ok:
                    row["out_bytes"] = len(data)
                    row["note"] = ("flux valide = %d octets sur %d declares : le jeu lit des octets "
                                   "parasites au-dela" % (len(data), n))
                elif nxt_b == e["src"] + used:
                    row["note"] = "contigu avec la structure suivante"
                open(os.path.join(out, "lz", name + ".lz"), "wb").write(win[:used])
                if verify:
                    z = compress(data)
                    back, _ = decompress(z, len(data))
                    stats["n"] += 1
                    stats["orig"] += used
                    stats["ours"] += len(z)
                    stats["same"] += (z == win[:used])
                    stats["bad"] += (back != data)
                    zo = compress(data, "okumura")
                    stats["oku"] += 1
                    stats["oku_same"] += (zo == win[:used])
                    stats["oku_size"] += len(zo)
                    stats["bad"] += (decompress(zo, len(data))[0] != data)
                    row["recompressed"] = len(z)
            open(os.path.join(out, "tiles", name + ".bin"), "wb").write(data)
            tiles_to_png(os.path.join(out, "tiles", name + ".png"), data)
            rows.append(row)

    # ------------------------------------------------ tilemaps ($25C3 : LD HL / LD DE / LD BC)
    for site in find_all(rom.d, CALL_MAP):
        if site < 9:
            continue
        ops = rom.d[site - 9], rom.d[site - 6], rom.d[site - 3]
        if sorted(ops) != [0x01, 0x11, 0x21]:
            continue
        val = {}
        for q in (site - 9, site - 6, site - 3):
            val[rom.d[q]] = rom.d[q + 1] | rom.d[q + 2] << 8
        hl, de, bc = val[0x21], val[0x11], val[0x01]
        if not (0x4000 <= hl < 0x8000 and 0x8000 <= de < 0xA000 and 0 < bc <= 0x800):
            continue
        bank = rom.bank_before(site - 9)
        assumed = False
        if bank is None:
            bank = rom.code_bank(site) if site >= 0x4000 else 0x15
            assumed = True
        key = ("map", bank, hl, bc)
        if key in seen:
            continue
        seen.add(key)
        name = "b%02X_%04X_to_%04X" % (bank, hl, de)
        row = dict(kind="tilemap_lz", list="code@%05X" % site, bank="%02X" % bank, src="%04X" % hl,
                   dest_tile="%04X" % de, ntiles="", out_bytes=bc)
        try:
            data, used = decompress(rom.window(bank, hl), bc)
            row["lz_bytes"], row["src_end"] = used, "%04X" % (hl + used)
            open(os.path.join(out, "maps", name + ".bin"), "wb").write(data)
            open(os.path.join(out, "lz", "map_" + name + ".lz"), "wb").write(rom.window(bank, hl)[:used])
            row["note"] = "banque supposee" if assumed else ""
        except ValueError as ex:
            row["note"] = "ERREUR : %s" % ex
        rows.append(row)

    # ------------------------------------------------ index
    cols = ["kind", "list", "entry", "bank", "src", "src_end", "dest_tile", "ntiles", "out_bytes", "lz_bytes", "recompressed", "note"]
    with open(os.path.join(out, "index.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    nt = sum(1 for r in rows if r["kind"] in ("tiles_lz", "tiles_raw"))
    nm = sum(1 for r in rows if r["kind"] == "tilemap_lz")
    errs = sum(1 for r in rows if str(r.get("note", "")).startswith("ERREUR"))
    print("%d listes de commandes, %d blocs de tuiles, %d tilemaps, %d erreurs -> %s/" %
          (len(lists), nt, nm, errs, out))
    if verify and stats["n"]:
        print("verification : %d blocs LZSS recompresses, %d aller-retours incorrects" % (stats["n"], stats["bad"]))
        print("  mode optimal : %d -> %d octets (%.1f%% de l'original), %d flux identiques" %
              (stats["orig"], stats["ours"], 100.0 * stats["ours"] / stats["orig"], stats["same"]))
        print("  mode okumura : %d -> %d octets (%.1f%% de l'original), %d flux identiques sur %d" %
              (stats["orig"], stats["oku_size"], 100.0 * stats["oku_size"] / stats["orig"], stats["oku_same"], stats["oku"]))


if __name__ == "__main__":
    main()
