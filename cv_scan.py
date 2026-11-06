#!/usr/bin/env python3
"""
cv_scan.py - inventaire des donnees compressees (variante C) de Castlevania Legends (USA/Europe, SGB).

Principe : le jeu charge ses ressources avec un chargeur de listes de commandes (bank 0) :
    $2E53  choisit le mode selon l'etat du LCD (LCDC bit 7) puis saute en $2E59 (ecran allume) ou $2E5D
    $2E59 / $2E5D  chargeur direct ; le decodeur LZSS lui-meme est en $2ECD
Chaque liste est une suite d'entrees de 7 octets [cmd][banque][source][destination][nb tuiles]
terminee par $00 ; cmd 1 = donnees compressees (variante C), cmd 2 = copie brute.
Le programme retrouve tous les appelants du chargeur, suit les listes directes (LD HL,liste) et les tables
de pointeurs (LD HL,table ; CALL $DDD6), lit chaque liste, puis VALIDE chaque flux compresse en le decodant
(le flux doit etre coherent avec l'encodeur glouton d'Okumura, F=34, sur toute sa longueur).

Usage : cv_scan.py ROM.gb [dossier_sortie] [--no-png] [--verify]
   --verify : rejoue chaque flux (et sa recompression) avec le vrai decodeur du jeu, via tk_sm83_check.py

Sortie : streams_castlevania_legends.csv, lists_castlevania_legends.csv,
         data/ (donnees decompressees .bin, flux d'origine .lz, feuilles de tuiles .png)
Les adresses des routines sont celles de la ROM USA/Europe ; elles sont verifiees au lancement.
"""
import os, sys, csv, collections
from tk_decompress import decompress_limited

N = 1024
F = 34                     # longueur de match max ; position d'ecriture initiale = N - F = $3DE
R0 = N - F
LOADER = (0x2E53, 0x2E59, 0x2E5D)      # points d'entree du chargeur de listes
TABLE_LOOKUP = 0xDDD6                  # routine RAM : HL = table[A]
LOADER_SIGNATURE = bytes.fromhex("F040CB7F2804")   # LDH A,(LCDC) ; BIT 7,A ; JR Z


# ------------------------------------------------------------------ decodage / coherence (variante C)
def decode_tokens(src, s, end, maxout=6000):
    """Decode depuis s jusqu'au premier token invalide. Retourne (sortie, tokens) ;
    token = (indice_sortie, pos_source_apres, est_match, longueur, distance)."""
    written = [True] * R0 + [False] * F
    buf = bytearray([0x20]) * R0 + bytearray(F)
    r = R0; out = bytearray(); p = s; flags = 0; nb = 0; toks = []
    while len(out) < maxout:
        q = p
        if nb == 0:
            if q >= end: break
            fl = src[q]; q += 1; n2 = 8
        else:
            fl, n2 = flags, nb
        i = len(out)
        if fl & 1:
            if q >= end: break
            c = src[q]; q += 1
            buf[r] = c; written[r] = True; out.append(c); r = (r + 1) & 1023
            tok = (i, q, 0, 1, 0)
        else:
            if q + 1 >= end: break
            lo, hi = src[q], src[q + 1]; q += 2
            pos = lo | (((hi >> 5) & 3) << 8)
            L = (hi & 0x1F) + 3
            d = (r - pos) & 1023 or 1024
            bad = False; pp = pos
            for _ in range(L):
                if not written[pp]: bad = True; break
                c = buf[pp]; pp = (pp + 1) & 1023
                buf[r] = c; written[r] = True; out.append(c); r = (r + 1) & 1023
            if bad: break
            tok = (i, q, 1, L, d)
        toks.append(tok); flags, nb, p = fl >> 1, n2 - 1, q
    return out, toks


def consistent_prefix(out, toks, s):
    """Nombre de tokens consecutifs compatibles avec l'encodeur glouton d'Okumura (N=1024, F=34).
    Retourne (tokens_ok, octets_source, octets_sortie, nb_matchs). Les F derniers octets de sortie ne
    sont pas verifiables, donc la fin est sous-estimee de F octets au plus."""
    n = len(out)
    H = [-1] * F + [0x20] * R0 + list(out)             # H[1024 + i] = out[i]
    gram = {}
    for j in range(F, 1022):
        gram.setdefault((H[j], H[j + 1], H[j + 2]), []).append(j)
    nxt = 1022; good = 0; endp = s; endout = 0; nm = 0
    for (i, qa, kind, L, d) in toks:
        base = N + i
        if i + F > n: break
        while nxt < base:
            gram.setdefault((H[nxt], H[nxt + 1], H[nxt + 2]), []).append(nxt); nxt += 1
        best = 0
        for j in reversed(gram.get((H[base], H[base + 1], H[base + 2]), ())):
            if j >= base: continue
            if base - j > R0: break
            k = 3
            while k < F and H[j + k] == H[base + k]: k += 1
            if k > best: best = k
            if best == F: break
        if kind == 0:
            if best >= 3: break                          # un match existait : pas notre encodeur
        elif d > R0 or L > F or best != L:
            break
        good += 1; endp = qa; endout = i + L; nm += kind
    return good, endp - s, endout, nm


# ------------------------------------------------------------------ lecture des listes
class Rom:
    def __init__(self, path):
        self.d = open(path, "rb").read()
        self.nb = len(self.d) // 0x4000

    def off(self, bank, addr):
        return addr if addr < 0x4000 else bank * 0x4000 + addr - 0x4000

    def label(self, bank, addr):
        return "%02X:%04X" % (bank if addr >= 0x4000 else 0, addr)

    def find(self, pat):
        i = 0
        while True:
            i = self.d.find(pat, i)
            if i < 0: return
            yield i
            i += 1

    def parse_list(self, ctx, addr):
        """Liste de commandes : entrees [cmd][banque][src][dest][n] (cmd 1 ou 2), terminee par 00."""
        ents = []
        a = addr
        for _ in range(24):
            o = self.off(ctx, a)
            if o + 7 > len(self.d): return None
            cmd = self.d[o]
            if cmd == 0:
                return ents if ents else None
            if cmd not in (1, 2): return None
            b = self.d[o + 1]
            src = self.d[o + 2] | self.d[o + 3] << 8
            dst = self.d[o + 4] | self.d[o + 5] << 8
            cnt = self.d[o + 6]
            if b >= self.nb or cnt == 0: return None
            ents.append(dict(cmd=cmd, bank=b, src=src, dst=dst, cnt=cnt, at=a))
            a += 7
        return None

    def parse_table(self, ctx, addr):
        """Table de pointeurs vers des listes : s'arrete au premier pointeur qui ne mene pas a une liste
        valide, ou des que l'on atteint la premiere liste situee APRES la table."""
        ptrs = []; limit = 0x10000; a = addr
        while a < limit and len(ptrs) < 64:
            o = self.off(ctx, a)
            p = self.d[o] | self.d[o + 1] << 8
            if p < 0x100 or p >= 0x8000: break
            if self.parse_list(ctx if p >= 0x4000 else 0, p) is None: break
            ptrs.append(p)
            if p > addr: limit = min(limit, p)
            a += 2
        return ptrs


def guess_type(data):
    if len(data) in (576, 1024) and (data.count(0) + data.count(0x20)) / len(data) > 0.3:
        return "tilemap ?"
    return "tuiles"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    rom = Rom(args[0]); d = rom.d
    out = args[1] if len(args) > 1 else "cv_out"
    if d[LOADER[0]:LOADER[0] + 6] != LOADER_SIGNATURE:
        sys.exit("ROM non reconnue : le chargeur de listes de Castlevania Legends (USA/Europe) n'est pas en $%04X" % LOADER[0])
    os.makedirs(os.path.join(out, "data"), exist_ok=True)

    # 1. appelants du chargeur
    sites = []
    for tgt in LOADER:
        for op in (0xCD, 0xC3):
            sites += list(rom.find(bytes([op, tgt & 255, tgt >> 8])))
    roots = {}                                         # (ctx, adresse, nature) -> sites
    unresolved = []
    for s in sorted(sites):
        ctx = s // 0x4000 if s >= 0x4000 else 0
        if d[s - 3] == 0x21:                           # LD HL,liste ; CALL loader
            roots.setdefault((ctx, d[s - 2] | d[s - 1] << 8, "liste"), []).append(s)
        elif d[s - 3:s] == bytes([0xCD, TABLE_LOOKUP & 255, TABLE_LOOKUP >> 8]) and d[s - 6] == 0x21:
            roots.setdefault((ctx, d[s - 5] | d[s - 4] << 8, "table"), []).append(s)   # LD HL,table ; CALL $DDD6 ; CALL loader
        else:
            unresolved.append(s)
    print("appelants du chargeur : %d (%d racines resolues, %d non resolus)" % (len(sites), len(roots), len(unresolved)))

    # 2. listes
    lists = {}                                         # (bank_effective, adresse) -> (entrees, origines)
    for (ctx, hl, kind), ss in sorted(roots.items()):
        origin = ["%s@%s" % (kind, rom.label(ctx, hl))]
        if kind == "liste":
            ptrs = [hl]
        else:
            ptrs = rom.parse_table(ctx if hl >= 0x4000 else 0, hl)
        for p in ptrs:
            c = ctx if p >= 0x4000 else 0
            ents = rom.parse_list(c, p)
            if ents:
                lists.setdefault((c, p), (ents, []))[1].extend(origin)
    n1 = sum(1 for v in lists.values() for e in v[0] if e["cmd"] == 1)
    n2 = sum(1 for v in lists.values() for e in v[0] if e["cmd"] == 2)
    print("listes valides : %d | entrees compressees (cmd 1) : %d | copies brutes (cmd 2) : %d" % (len(lists), n1, n2))

    # 3. validation des flux compresses
    streams = collections.OrderedDict()
    for (c, p), (ents, origins) in sorted(lists.items()):
        for e in ents:
            if e["cmd"] == 1:
                streams.setdefault((e["bank"], e["src"]), []).append(dict(e, list=rom.label(c, p)))
    rows = []; rejected = []
    for (b, src), es in sorted(streams.items()):
        e = es[0]; n = e["cnt"] * 16
        so = rom.off(b, src); end = 0x4000 if src < 0x4000 else b * 0x4000 + 0x4000
        toks_out, toks = decode_tokens(d, so, end, n + 80)
        g, used, eo, nm = consistent_prefix(toks_out, toks, so)
        data, u, ok = decompress_limited(d[:end], n, so, None, "C")
        if not (ok and len(toks_out) >= n and eo >= n - F - 2):
            rejected.append((b, src)); continue
        name = "b%02X_%04X" % (b, src)
        open(os.path.join(out, "data", name + ".bin"), "wb").write(data)
        open(os.path.join(out, "data", name + ".lz"), "wb").write(d[so:so + u])
        kind = guess_type(data)
        if "--no-png" not in sys.argv and kind == "tuiles":
            from tk_extract import tiles_to_png
            tiles_to_png(os.path.join(out, "data", name + ".png"), data)
        rows.append(dict(banque="%02X" % b, adresse="%04X" % src, offset_fichier="%06X" % so, type=kind,
                         destination="tuile %03X" % e["dst"], tuiles=e["cnt"], octets_sortie=n, octets_compresses=u,
                         taux="%.1f%%" % (100.0 * u / n), tokens_coherents=g,
                         listes=" ; ".join(sorted({x["list"] for x in es}))))
    with open(os.path.join(out, "streams_castlevania_legends.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    lr = []
    for (c, p), (ents, origins) in sorted(lists.items(), key=lambda x: (x[0][0], x[0][1])):
        for i, e in enumerate(ents):
            lr.append(dict(liste=rom.label(c, p), entree=i, commande="1 (compresse)" if e["cmd"] == 1 else "2 (copie brute)",
                           banque_source="%02X" % e["bank"], source="%04X" % e["src"], destination="tuile %03X" % e["dst"],
                           tuiles=e["cnt"], octets=e["cnt"] * 16, appelee_depuis=" ; ".join(origins[:4])))
    with open(os.path.join(out, "lists_castlevania_legends.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, list(lr[0].keys())); w.writeheader(); w.writerows(lr)

    tn = sum(r["octets_sortie"] for r in rows); tu = sum(r["octets_compresses"] for r in rows)
    print("\n%d flux valides, %d octets compresses -> %d octets (%.1f%%) ; %d entree(s) rejetee(s)" %
          (len(rows), tu, tn, 100.0 * tu / tn, len(rejected)))
    print("par banque :", ", ".join("%s:%d" % kv for kv in sorted(collections.Counter(r["banque"] for r in rows).items())))
    print("sortie : %s/" % out)
    if "--verify" in sys.argv:
        import tk_sm83_check
        tk_sm83_check.check_cv(d, out)


if __name__ == "__main__":
    main()
