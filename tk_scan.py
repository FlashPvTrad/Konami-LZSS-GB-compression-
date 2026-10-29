#!/usr/bin/env python3
"""
tk_scan.py - cherche TOUTES les donnees LZSS (variantes A et B) dans la ROM Konami GB Collection Vol.2.

Deux sources de preuves, combinees :
 1. REFERENCES (certain) - on retrouve les points d'entree des decodeurs :
      variante A (F=18, depart 0x3EE) : entrees de 7 octets [01][banque][src][dest][n_tuiles]
                                         des listes lues par $23C4, et appels directs $25C3
      variante B (F=16, depart 0x3F0) : appels directs $3A5E / $3A65 precedes de
                                         LD HL,src / LD DE,dest / LD BC,longueur et du choix de banque
    chaque reference est validee en decodant reellement le flux.
 2. BALAYAGE AVEUGLE (probable) - chaque offset de la ROM est essaye comme debut de flux. Un flux
    produit par l'encodeur glouton d'Okumura ne contient jamais de litteral la ou un match de
    >= 3 octets existait, et ses matchs sont toujours maximaux : un flux reel reste "coherent"
    sur des centaines de tokens, du bruit decroche en quelques tokens (cf. --selftest).

Usage : tk_scan.py ROM.gb [dossier_sortie] [--min-tokens N] [--no-png]
Sortie : streams.csv, A/ et B/ (donnees decompressees .bin, flux .lz, feuilles de tuiles .png)
"""
import os, sys, csv, collections
from tk_lzss import decompress_limited, rom_offset
from tk_extract import Rom, tiles_to_png, find_all

N = 1024
VARIANTS = {"A": 18, "B": 16}              # F de chaque variante (depart d'ecriture = N - F)


# ------------------------------------------------------------------ decodage tolerant + coherence
def decode_tokens(src, s, end, F, maxout=6000):
    """Decode depuis s jusqu'au premier token invalide. Retourne (sortie, tokens).
    token = (indice_sortie, pos_source_apres, est_match, longueur, distance)."""
    r0 = N - F
    written = [True] * r0 + [False] * F
    buf = bytearray([0x20]) * r0 + bytearray(F)
    r = r0; out = bytearray(); p = s; flags = 0; nb = 0; toks = []
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
            pos = lo | ((hi >> 4) << 8)
            if pos >= N: break
            L = (hi & 15) + 3
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


def consistent_prefix(out, toks, s, F):
    """Nombre de tokens consecutifs compatibles avec l'encodeur glouton d'Okumura (N=1024, F).
    Retourne (tokens_ok, octets_source, octets_sortie, nb_matchs). Les F derniers octets de sortie ne sont
    pas verifiables (contexte insuffisant), donc la fin est sous-estimee de F octets au plus."""
    r0 = N - F
    n = len(out)
    H = [-1] * F + [0x20] * r0 + list(out)             # H[1024 + i] = out[i]
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
            if base - j > r0: break                      # distance max de l'encodeur : N - F
            k = 3
            while k < F and H[j + k] == H[base + k]: k += 1
            if k > best: best = k
            if best == F: break
        if kind == 0:
            if best >= 3: break                          # un match existait : pas notre encodeur
        elif d > r0 or L > F or best != L:
            break
        good += 1; endp = qa; endout = i + L; nm += kind
    return good, endp - s, endout, nm


# ------------------------------------------------------------------ utilitaires ROM
def cpu_addr(off):
    b = off // 0x4000
    return b, (0x4000 + off - b * 0x4000) if b else off


def label(b, a):
    return "%02X:%04X" % (b, a)


def window(rom, bank, addr):
    o = rom_offset(bank, addr) if bank else addr
    end = (bank * 0x4000 + 0x4000) if bank else 0x4000
    return o, end


def validate(rom, variant, bank, addr, n):
    """Decode reellement `n` octets depuis bank:addr. Retourne (ok, octets_compresses, coherents, sortie_verifiee).
    ok = decodage complet ET coherence de l'encodeur sur n-F-2 octets au moins."""
    F = VARIANTS[variant]
    o, end = window(rom, bank, addr)
    out, toks = decode_tokens(rom, o, end, F, n + 40)
    g, used, eo, nm = consistent_prefix(out, toks, o, F)
    if len(out) < n:
        return False, used, g, eo
    ok = eo >= n - F - 2
    _, used_full, _ = decompress_limited(rom[o:end], n, 0, None, F)
    return ok, used_full, g, eo


# ------------------------------------------------------------------ 1. references
def refs_variant_A(rom, R):
    """Entrees de listes [01][banque][src][dest][n] (valides par decodage) + appels $25C3."""
    nb = len(rom.d) // 0x4000
    d = rom.d
    found = {}
    for o in range(0x4000, len(d) - 8):
        if d[o] != 1: continue
        b = d[o + 1]
        if not 0 < b < nb: continue
        src = d[o + 2] | d[o + 3] << 8
        if not 0x4000 <= src < 0x8000: continue
        cnt = d[o + 6]; dst = d[o + 4] | d[o + 5] << 8
        if cnt == 0 or dst > 0x0800: continue
        def entry_ok(p):
            if d[p] not in (1, 2) or not 0 < d[p + 1] < nb: return False
            sa = d[p + 2] | d[p + 3] << 8
            return 0x4000 <= sa < 0x8000 and d[p + 6] > 0 and (d[p + 4] | d[p + 5] << 8) <= 0x0800
        if not (d[o + 7] == 0 or entry_ok(o + 7) or entry_ok(o - 7)):    # contexte de liste
            continue
        ok, used, g, eo = validate(d, "A", b, src, cnt * 16)
        why = "liste @%s" % label(*cpu_addr(o))
        if ok:
            found.setdefault((b, src), []).append(dict(kind="tuiles", dest="tuile %03X" % dst, n=cnt * 16,
                                                       used=used, g=g, why=why))
        elif g >= 24 and 32 <= eo < cnt * 16:                  # la liste declare plus que le flux ne contient
            o2, end2 = window(d, b, src)
            n2 = (eo + 15) // 16 * 16
            data2, used2, ok2 = decompress_limited(d[o2:end2], n2, 0, None, 18)
            if ok2:
                found.setdefault((b, src), []).append(dict(
                    kind="tuiles", dest="tuile %03X" % dst, n=n2, used=used2, g=g,
                    why=why + " (declare %d tuiles, flux = %d)" % (cnt, n2 // 16)))
    for site in find_all(d, b"\xCD\xC3\x25"):             # CALL $25C3
        if site < 9: continue
        ops = (d[site - 9], d[site - 6], d[site - 3])
        if sorted(ops) != [0x01, 0x11, 0x21]: continue
        val = {d[q]: d[q + 1] | d[q + 2] << 8 for q in (site - 9, site - 6, site - 3)}
        hl, de, bc = val[0x21], val[0x11], val[0x01]
        if not (0x4000 <= hl < 0x8000 and 0 < bc <= 0x800): continue
        b = R.bank_before(site - 9)
        if b is None: b = site // 0x4000 if site >= 0x4000 else 0x15
        ok, used, g, eo = validate(d, "A", b, hl, bc)
        if ok:
            kind = "tilemap" if 0x9800 <= de < 0xA000 else "tuiles"
            found.setdefault((b, hl), []).append(dict(kind=kind, dest="VRAM %04X" % de, n=bc, used=used, g=g,
                                                      why="appel $25C3 @%s" % label(*cpu_addr(site))))
    return found


def refs_variant_B(rom):
    """Appels $3A5E / $3A65 : LD HL / LD DE / LD BC (9 octets contigus) + LD A,n ; LD ($2180),A avant."""
    d = rom.d
    found = {}
    nb = len(d) // 0x4000
    for target in (0x3A5E, 0x3A65):
        for site in find_all(d, bytes([0xCD, target & 255, target >> 8])):
            if site < 9: continue
            ops = (d[site - 9], d[site - 6], d[site - 3])
            if sorted(ops) != [0x01, 0x11, 0x21]: continue
            val = {d[q]: d[q + 1] | d[q + 2] << 8 for q in (site - 9, site - 6, site - 3)}
            hl, de, bc = val[0x21], val[0x11], val[0x01]
            if not (0 < bc <= 0x2000): continue
            bank = None
            for p in range(site - 10, max(0, site - 70), -1):   # dernier "LD A,n ; LD ($2180),A"
                if d[p] == 0x3E and d[p + 2:p + 5] == b"\xEA\x80\x21":
                    bank = d[p + 1]; break
            if hl < 0x4000: bank = 0 if hl < 0x4000 and bank is None else bank
            if bank is None or not (hl >= 0x4000 or bank == 0) or hl >= 0x8000: continue
            if hl >= 0x4000 and not 0 < bank < nb: continue
            ok, used, g, eo = validate(d, "B", bank if hl >= 0x4000 else 0, hl, bc)
            if ok:
                kind = ("tilemap" if 0x9800 <= de < 0xA000 else "tuiles" if 0x8000 <= de < 0x9800 else "tampon")
                found.setdefault((bank if hl >= 0x4000 else 0, hl), []).append(
                    dict(kind=kind, dest="%s %04X" % ("VRAM" if 0x8000 <= de < 0xA000 else "RAM", de),
                         n=bc, used=used, g=g, why="appel $%04X @%s" % (target, label(*cpu_addr(site)))))
    return found


# ------------------------------------------------------------------ 2. balayage aveugle
def blind_scan(rom, variant, min_tokens):
    d = rom.d
    F = VARIANTS[variant]
    res = []
    for bank in range(len(d) // 0x4000):
        base = bank * 0x4000; end = base + 0x4000
        for s in range(base, end - 4):
            if len(set(d[s:s + 8])) < 4: continue            # remplissage ($00, $FF, ...)
            out, toks = decode_tokens(d, s, end, F)
            if len(toks) < min_tokens: continue
            g, used, eo, nm = consistent_prefix(out, toks, s, F)
            if g >= min_tokens and nm >= 8 and nm >= 0.04 * g:      # un vrai flux contient des matchs
                res.append(dict(variant=variant, off=s, bank=bank, g=g, used=used, eo=eo, nm=nm))
    return res


def guess_type(data):
    if len(data) < 64: return "?"
    distinct = len(set(data))
    return "tilemap ?" if distinct <= max(24, len(data) // 12) else "tuiles ?"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args: sys.exit(__doc__)
    min_tokens = 40
    if "--min-tokens" in sys.argv:
        min_tokens = int(sys.argv[sys.argv.index("--min-tokens") + 1])
        args = [a for a in args if a != str(min_tokens)]
    out = args[1] if len(args) > 1 else "scan"
    rom = Rom(args[0]); d = rom.d
    for v in VARIANTS: os.makedirs(os.path.join(out, v), exist_ok=True)

    # ---- 1. references
    refs = {"A": refs_variant_A(rom, rom), "B": refs_variant_B(rom)}
    streams = []        # dict(variant, bank, addr, off, n, used, source, ...)
    for v, found in refs.items():
        for (b, a), lst in sorted(found.items()):
            e = lst[0]
            o, _ = window(d, b, a)
            streams.append(dict(variant=v, bank=b, addr=a, off=o, n=e["n"], used=e["used"], g=e["g"],
                                source="reference", conf="certain", kind=e["kind"], dest=e["dest"],
                                note="%d reference(s) : %s" % (len(lst), " ; ".join(x["why"] for x in lst[:3]))))
    print("references : %d flux (A: %d, B: %d)" % (len(streams), len(refs["A"]), len(refs["B"])))

    # ---- 2. balayage aveugle
    cands = []
    for v in VARIANTS:
        c = blind_scan(rom, v, min_tokens)
        print("balayage variante %s : %d departs coherents (>= %d tokens)" % (v, len(c), min_tokens))
        cands += c
    extents = sorted((s["off"], s["off"] + max(1, s["used"])) for s in streams)

    def inside(x):
        return any(a <= x < b for a, b in extents)

    for c in sorted(cands, key=lambda c: -c["g"]):
        if inside(c["off"]): continue                         # depart au milieu d'un flux deja retenu
        limit = next((a for a, b in extents if a > c["off"]), None)
        F = VARIANTS[c["variant"]]
        o_end = c["bank"] * 0x4000 + 0x4000
        used, eo, note = c["used"], c["eo"], ""
        if limit is not None and limit < c["off"] + used + 2:   # le flux bute sur une structure connue
            data, u, _ = decompress_limited(d[c["off"]:o_end], 6000, 0, limit - c["off"], F)
            used, eo, note = u, len(data), "borne par un flux/reference voisin"
        if used < 24: continue
        n16 = (eo + 15) // 16 * 16                         # tuiles/tilemaps : multiples de 16 octets
        lim = (limit - c["off"]) if limit is not None else None
        d16, u16, ok16 = decompress_limited(d[c["off"]:o_end], n16, 0, lim, F)
        if ok16 and u16 <= used + 4:
            used, eo = u16, n16
            note = (note + " ; " if note else "") + "taille arrondie a 16 octets"
        b, a = cpu_addr(c["off"])
        conf = "haute" if c["g"] >= 120 else "bonne" if c["g"] >= 60 else "moyenne"
        s = dict(variant=c["variant"], bank=b, addr=a, off=c["off"], n=eo, used=used, g=c["g"],
                 source="balayage", conf=conf, kind="", dest="", note=note or "taille = partie verifiee (+<=%d o)" % F)
        streams.append(s)
        extents = sorted(extents + [(s["off"], s["off"] + used)])
    streams.sort(key=lambda s: (s["variant"], s["off"]))

    # ---- extraction + csv
    rows = []
    for s in streams:
        F = VARIANTS[s["variant"]]
        o, end = s["off"], (s["off"] // 0x4000) * 0x4000 + 0x4000
        data, used, ok = decompress_limited(d[o:end], s["n"], 0, None, F)
        name = "b%02X_%04X" % (s["bank"], s["addr"])
        open(os.path.join(out, s["variant"], name + ".bin"), "wb").write(data)
        open(os.path.join(out, s["variant"], name + ".lz"), "wb").write(d[o:o + used])
        kind = s["kind"] or guess_type(data)
        if "--no-png" not in sys.argv and kind.startswith("tuiles") and len(data) >= 32:
            tiles_to_png(os.path.join(out, s["variant"], name + ".png"), data)
        refcount = sum(1 for _ in find_all(d, bytes([s["addr"] & 255, s["addr"] >> 8]))) if s["source"] != "reference" else ""
        rows.append(dict(variante=s["variant"], banque="%02X" % s["bank"], adresse="%04X" % s["addr"],
                         offset_fichier="%06X" % o, source=s["source"], confiance=s["conf"],
                         type=kind, destination=s["dest"], octets_sortie=len(data), octets_compresses=used,
                         tokens_coherents=s["g"], occurrences_adresse=refcount, note=s["note"]))
    with open(os.path.join(out, "streams.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    # ---- resume
    print()
    cnt = collections.Counter((r["variante"], r["source"], r["confiance"]) for r in rows)
    for k, v in sorted(cnt.items()): print("  variante %s | %-10s | %-8s : %d flux" % (k[0], k[1], k[2], v))
    tot_o = sum(r["octets_sortie"] for r in rows); tot_z = sum(r["octets_compresses"] for r in rows)
    print("\n%d flux au total, %d octets compresses -> %d octets decompresses  (%s/streams.csv)" %
          (len(rows), tot_z, tot_o, out))
    byb = collections.Counter((r["variante"], r["banque"]) for r in rows)
    print("par banque :", ", ".join("%s%s:%d" % (k[0], k[1], v) for k, v in sorted(byb.items())))


if __name__ == "__main__":
    main()
