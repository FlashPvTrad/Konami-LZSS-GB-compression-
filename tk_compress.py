#!/usr/bin/env python3
"""
tk_compress.py - compresse un fichier au format LZSS de Konami pour Game Boy (trois variantes : A, B, C).

Les flux produits sont lus par les decodeurs des jeux (verifie sur le code reel des ROM, voir tk_sm83_check.py).

  Variante  Jeu                       Depart   Match   hi = octet 2 : position haute / longueur-3
  A         Track & Field             $3EE     3..18   bits 4-7 / bits 0-3
  B         Parodius                  $3F0     3..16   bits 4-7 / bits 0-3
  C         Castlevania Legends       $3DE     3..34   bits 5-6 / bits 0-4      (bit 7 = 0)

Modes :
  optimal  (defaut) : plus petit flux possible. Parsing optimal EXACT en octets (donnees + octets de flags)
                      par programmation dynamique.
  okumura           : encodeur de reference d'Okumura (glouton + arbre binaire). Donne la meme taille que
                      les flux d'origine des jeux (identiques a l'octet pres dans une partie des cas).

Le programme relit et decompresse le resultat pour s'assurer qu'il redonne exactement l'entree.
--max-size refuse un flux plus gros que la place disponible (utile pour reinjecter en place dans la ROM).
Il n'y a pas de marqueur de fin dans ce format : notez la taille decompressee de l'entree,
c'est la longueur que le jeu doit recevoir.

Exemples :
  tk_compress.py tuiles.bin tuiles.lz
  tk_compress.py map.bin map.lz --variant B --mode okumura
  tk_compress.py tuiles.bin tuiles.lz --variant C --max-size 1449
"""
import argparse, sys

N = 1024          # taille de la fenetre
THRESHOLD = 2     # longueur min d'un match = 3
# variante : (F, decalage de la position haute, masque de la position haute, masque de la longueur)
# F = longueur de match max ; position d'ecriture initiale = N - F
VARIANTS = {"A": (18, 4, 0x0F, 0x0F), "B": (16, 4, 0x0F, 0x0F), "C": (34, 5, 0x03, 0x1F)}


def _token(variant, pos, length):
    """Les 2 octets d'un match : (lo, hi)."""
    _, ps, pm, _ = VARIANTS[variant]
    return pos & 0xFF, (((pos >> 8) & pm) << ps) | (length - THRESHOLD - 1)


def _find_matches(data, F):
    """Pour chaque i : (longueur_max, distance) du meilleur match (longueur >= 3), sinon (0, 0).

    La position d'un octet dans la fenetre ne depend que de son rang dans la sortie
    ((N - F + i) mod 1024), donc les candidats ne dependent pas du parsing choisi.
    Historique virtuel H : F inconnus (jamais utilises), N-F x $20, puis les donnees."""
    START = N - F
    n = len(data)
    H = [-1] * F + [0x20] * START + list(data)
    occ = {}                                   # valeur -> indices de H, croissants
    for j in range(N):
        if H[j] >= 0:
            occ.setdefault(H[j], []).append(j)
    best = [(0, 0)] * n
    for i in range(n):
        base = N + i                           # indice de data[i] dans H
        maxl = min(F, n - i)
        if maxl >= THRESHOLD + 1:
            bl, bd = 0, 0
            cands = occ.get(data[i], ())
            lo = base - N                      # distance max = 1024
            for idx in range(len(cands) - 1, -1, -1):   # du plus proche au plus loin
                j = cands[idx]
                if j < lo:
                    break
                k = 1
                while k < maxl and H[j + k] == data[i + k]:
                    k += 1
                if k > bl:
                    bl, bd = k, base - j
                    if k == maxl:
                        break
            if bl >= THRESHOLD + 1:
                best[i] = (bl, bd)
        occ.setdefault(data[i], []).append(base)
    return best


def compress_optimal(data, variant="A"):
    """Parsing optimal exact : cout = octets de donnees + 1 octet de flags par groupe de 8 elements.
    Etat de la programmation dynamique : (position, rang de l'element dans son groupe de 8)."""
    F = VARIANTS[variant][0]
    START = N - F
    data = bytes(data)
    n = len(data)
    if n == 0:
        return b""
    best = _find_matches(data, F)
    INF = 1 << 60
    f = [[INF] * 8 for _ in range(n + 1)]
    ch = [[0] * 8 for _ in range(n + 1)]
    for k in range(8):
        f[n][k] = 0
    for i in range(n - 1, -1, -1):
        L = best[i][0]
        for k in range(8):
            fl = 1 if k == 0 else 0
            nk = (k + 1) & 7
            c, c0 = 1 + fl + f[i + 1][nk], 0                  # litteral : 1 octet
            for l in range(THRESHOLD + 1, L + 1):             # match : 2 octets
                cc = 2 + fl + f[i + l][nk]
                if cc < c:
                    c, c0 = cc, l
            f[i][k], ch[i][k] = c, c0
    out = bytearray()
    i = k = 0
    fp = -1
    while i < n:
        if k == 0:
            fp = len(out); out.append(0)
        l = ch[i][k]
        if l == 0:
            out[fp] |= 1 << k
            out.append(data[i]); i += 1
        else:
            pos = (START + i - best[i][1]) & (N - 1)
            lo, hi = _token(variant, pos, l)
            out.append(lo); out.append(hi); i += l
        k = (k + 1) & 7
    return bytes(out)


def compress_okumura(data, variant="A"):
    """Port fidele de LZSS.C (H. Okumura, 1989) avec N=1024 et F selon la variante : arbre binaire
    de recherche, parsing glouton, distance max N-F. Seul le codage du token depend de la variante."""
    F = VARIANTS[variant][0]
    NIL = N
    text_buf = bytearray(N + F - 1)
    lson = [NIL] * (N + 1); rson = [NIL] * (N + 257); dad = [NIL] * (N + 1)
    st = {"mp": 0, "ml": 0}

    def insert(r):
        cmp = 1; key = r; p = N + 1 + text_buf[key]
        rson[r] = lson[r] = NIL; st["ml"] = 0
        while True:
            if cmp >= 0:
                if rson[p] != NIL: p = rson[p]
                else: rson[p] = r; dad[r] = p; return
            else:
                if lson[p] != NIL: p = lson[p]
                else: lson[p] = r; dad[r] = p; return
            i = 1
            while i < F:
                cmp = text_buf[key + i] - text_buf[p + i]
                if cmp != 0: break
                i += 1
            if i > st["ml"]:
                st["mp"] = p; st["ml"] = i
                if i >= F: break
        dad[r] = dad[p]; lson[r] = lson[p]; rson[r] = rson[p]
        dad[lson[p]] = r; dad[rson[p]] = r
        if rson[dad[p]] == p: rson[dad[p]] = r
        else: lson[dad[p]] = r
        dad[p] = NIL

    def delete(p):
        if dad[p] == NIL: return
        if rson[p] == NIL: q = lson[p]
        elif lson[p] == NIL: q = rson[p]
        else:
            q = lson[p]
            if rson[q] != NIL:
                while True:
                    q = rson[q]
                    if rson[q] == NIL: break
                rson[dad[q]] = lson[q]; dad[lson[q]] = dad[q]
                lson[q] = lson[p]; dad[lson[p]] = q
            rson[q] = rson[p]; dad[rson[p]] = q
        dad[q] = dad[p]
        if rson[dad[p]] == p: rson[dad[p]] = q
        else: lson[dad[p]] = q
        dad[p] = NIL

    data = bytes(data); n = len(data)
    if n == 0:
        return b""
    out = bytearray(); code = [0]; mask = 1
    s = 0; r = N - F
    for i in range(s, r): text_buf[i] = 0x20
    ip = 0; ln = 0
    while ln < F and ip < n:
        text_buf[r + ln] = data[ip]; ip += 1; ln += 1
    for i in range(1, F + 1): insert(r - i)
    insert(r)
    while True:
        if st["ml"] > ln: st["ml"] = ln
        if st["ml"] <= THRESHOLD:
            st["ml"] = 1; code[0] |= mask; code.append(text_buf[r])
        else:
            lo, hi = _token(variant, st["mp"], st["ml"])
            code.append(lo); code.append(hi)
        mask <<= 1
        if mask == 256:
            out += bytes(code); code = [0]; mask = 1
        last = st["ml"]; i = 0
        while i < last and ip < n:
            c = data[ip]; ip += 1
            delete(s); text_buf[s] = c
            if s < F - 1: text_buf[s + N] = c
            s = (s + 1) & (N - 1); r = (r + 1) & (N - 1)
            insert(r); i += 1
        while i < last:
            i += 1
            delete(s); s = (s + 1) & (N - 1); r = (r + 1) & (N - 1)
            ln -= 1
            if ln: insert(r)
        if ln <= 0: break
    if len(code) > 1: out += bytes(code)
    return bytes(out)


def compress(data, mode="optimal", variant="A"):
    if mode == "optimal":
        return compress_optimal(data, variant)
    if mode == "okumura":
        return compress_okumura(data, variant)
    raise ValueError("mode inconnu : %r" % mode)


def decompress_limited(src, outlen, variant="A"):
    """Decodeur, uniquement pour verifier le resultat. Retourne (donnees, octets_consommes, complet)."""
    F, ps, pm, lm = VARIANTS[variant]
    START = N - F
    buf = bytearray([0x20]) * START + bytearray(F)
    r = START; out = bytearray(); p = 0; flags = 0; nbits = 0
    while len(out) < outlen:
        q = p
        if nbits == 0:
            if q >= len(src): break
            fl = src[q]; q += 1; nb = 8
        else:
            fl, nb = flags, nbits
        if fl & 1:
            if q >= len(src): break
            c = src[q]; q += 1
            out.append(c); buf[r] = c; r = (r + 1) & (N - 1)
        else:
            if q + 1 >= len(src): break
            lo, hi = src[q], src[q + 1]; q += 2
            pos = lo | (((hi >> ps) & pm) << 8)
            if pos >= N: break
            for _ in range((hi & lm) + THRESHOLD + 1):
                if len(out) >= outlen: break
                c = buf[pos]; pos = (pos + 1) & (N - 1)
                out.append(c); buf[r] = c; r = (r + 1) & (N - 1)
        flags, nbits, p = fl >> 1, nb - 1, q
    return bytes(out), p, len(out) >= outlen


def main():
    ap = argparse.ArgumentParser(description="Compresse un fichier en LZSS Konami (variantes A, B et C).")
    ap.add_argument("entree", help="fichier a compresser")
    ap.add_argument("sortie", help="fichier compresse")
    ap.add_argument("--variant", "-v", choices=sorted(VARIANTS), default="A",
                    help="variante du codec : A Track & Field (defaut), B Parodius, C Castlevania Legends")
    ap.add_argument("--mode", "-m", choices=("optimal", "okumura"), default="optimal")
    ap.add_argument("--max-size", type=lambda x: int(x, 0), help="echoue si le flux depasse cette taille (octets)")
    ap.add_argument("--no-verify", action="store_true", help="ne pas relire/decompresser le resultat")
    a = ap.parse_args()

    data = open(a.entree, "rb").read()
    z = compress(data, a.mode, a.variant)
    if data and not a.no_verify:
        back, used, ok = decompress_limited(z, len(data), a.variant)
        if not ok or back != data or used != len(z):
            sys.exit("erreur interne : l'aller-retour ne redonne pas l'entree")
    if a.max_size is not None and len(z) > a.max_size:
        sys.exit("trop gros : %d octets compresses > %d autorises%s" %
                 (len(z), a.max_size, " (essayez --mode optimal)" if a.mode == "okumura" else ""))
    open(a.sortie, "wb").write(z)
    pct = 100.0 * len(z) / len(data) if data else 0
    print("variante %s, mode %s : %d -> %d octets (%.1f%%)  [longueur decompressee a fournir au jeu : %d = 0x%X]" %
          (a.variant, a.mode, len(data), len(z), pct, len(data), len(data)))


if __name__ == "__main__":
    main()
