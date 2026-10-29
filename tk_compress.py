#!/usr/bin/env python3
"""
tk_compress.py - compresse un fichier au format LZSS de Konami GB Collection Vol.2 (Track'n Field et autres).

Le flux produit est lu par les decodeurs du jeu (verifie sur le code reel de la ROM, variantes A et B).
  Variante A (defaut) : fenetre ecrite a partir de 0x3EE, matchs de 3 a 18   (moteur $25xx)
  Variante B          : fenetre ecrite a partir de 0x3F0, matchs de 3 a 16   (moteur $3A5E / $3A65)

Modes :
  optimal  (defaut) : plus petit flux possible (parsing optimal par programmation dynamique)
  okumura           : encodeur de reference d'Okumura (glouton + arbre binaire). Donne la meme taille
                      que les flux d'origine du jeu (identiques a l'octet pres dans la majorite des cas)

Le programme relit et decompresse le resultat pour s'assurer qu'il redonne exactement l'entree.
--max-size refuse un flux plus gros que la place disponible (utile pour reinjecter en place dans la ROM).
Il n'y a pas de marqueur de fin dans ce format : notez la taille decompressee de l'entree,
c'est la longueur que le jeu doit recevoir.

Exemples :
  tk_compress.py tuiles.bin tuiles.lz
  tk_compress.py map.bin map.lz --variant B --mode okumura
  tk_compress.py tuiles.bin tuiles.lz --max-size 640
"""
import argparse, sys

N = 1024          # taille de la fenetre
F = 18            # valeur par defaut (variante A)
THRESHOLD = 2     # longueur min d'un match = 3
START = N - F
VARIANT_F = {"A": 18, "B": 16}


def _find_matches(data, F=F):
    """Pour chaque i : (longueur_max, distance) du meilleur match (longueur >= 3), sinon (0, 0).

    La position d'un octet dans la fenetre ne depend que de son rang dans la sortie
    ((0x3EE + i) mod 1024), donc les candidats ne dependent pas du parsing choisi.
    Historique virtuel H : 18 inconnus (jamais utilises), 1006 x $20, puis les donnees."""
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


def compress_optimal(data, F=F):
    """Compresse `data`. Parsing optimal (programmation dynamique, cout en bits :
    litteral = 9 bits, match = 17 bits). Le flux produit est lu par le decodeur du jeu."""
    data = bytes(data)
    n = len(data)
    if n == 0:
        return b""
    START = N - F
    best = _find_matches(data, F)
    INF = 1 << 60
    cost = [INF] * (n + 1)
    choice = [0] * n                           # 0 = litteral, sinon longueur du match
    cost[n] = 0
    for i in range(n - 1, -1, -1):
        c, ch = 9 + cost[i + 1], 0
        L, _ = best[i]
        for l in range(THRESHOLD + 1, L + 1):
            cc = 17 + cost[i + l]
            if cc < c:
                c, ch = cc, l
        cost[i], choice[i] = c, ch
    out = bytearray()
    i = 0
    flagpos = -1
    nbits = 0
    while i < n:
        if nbits == 0:
            flagpos = len(out); out.append(0); nbits = 8
        bit_index = 8 - nbits
        nbits -= 1
        l = choice[i]
        if l == 0:
            out[flagpos] |= 1 << bit_index
            out.append(data[i]); i += 1
        else:
            d = best[i][1]
            pos = (START + i - d) & (N - 1)
            out.append(pos & 0xFF)
            out.append(((pos >> 8) << 4) | (l - THRESHOLD - 1))
            i += l
    return bytes(out)


def compress_okumura(data, F=F):
    """Port fidele de LZSS.C (H. Okumura, 1989) avec N=1024 : arbre binaire de recherche,
    parsing glouton, distance max N-F = 1006. Les flux obtenus ont exactement la taille
    de ceux du jeu (identiques a l'octet pres dans 17 cas sur 25 ; sinon seul le choix entre
    deux matchs de meme longueur differe)."""
    NIL=N
    text_buf=bytearray(N+F-1)
    lson=[NIL]*(N+1); rson=[NIL]*(N+257); dad=[NIL]*(N+1)
    st={'mp':0,'ml':0}
    def insert(r):
        cmp=1; key=r; p=N+1+text_buf[key]
        rson[r]=lson[r]=NIL; st['ml']=0
        while True:
            if cmp>=0:
                if rson[p]!=NIL: p=rson[p]
                else: rson[p]=r; dad[r]=p; return
            else:
                if lson[p]!=NIL: p=lson[p]
                else: lson[p]=r; dad[r]=p; return
            i=1
            while i<F:
                cmp=text_buf[key+i]-text_buf[p+i]
                if cmp!=0: break
                i+=1
            if i>st['ml']:
                st['mp']=p; st['ml']=i
                if i>=F: break
        dad[r]=dad[p]; lson[r]=lson[p]; rson[r]=rson[p]
        dad[lson[p]]=r; dad[rson[p]]=r
        if rson[dad[p]]==p: rson[dad[p]]=r
        else: lson[dad[p]]=r
        dad[p]=NIL
    def delete(p):
        if dad[p]==NIL: return
        if rson[p]==NIL: q=lson[p]
        elif lson[p]==NIL: q=rson[p]
        else:
            q=lson[p]
            if rson[q]!=NIL:
                while True:
                    q=rson[q]
                    if rson[q]==NIL: break
                rson[dad[q]]=lson[q]; dad[lson[q]]=dad[q]
                lson[q]=lson[p]; dad[lson[p]]=q
            rson[q]=rson[p]; dad[rson[p]]=q
        dad[q]=dad[p]
        if rson[dad[p]]==p: rson[dad[p]]=q
        else: lson[dad[p]]=q
        dad[p]=NIL
    data=bytes(data); n=len(data)
    if n==0: return b""
    out=bytearray(); code=[0]; mask=1
    s=0; r=N-F
    for i in range(s,r): text_buf[i]=0x20
    ip=0; ln=0
    while ln<F and ip<n:
        text_buf[r+ln]=data[ip]; ip+=1; ln+=1
    for i in range(1,F+1): insert(r-i)
    insert(r)
    while True:
        if st['ml']>ln: st['ml']=ln
        if st['ml']<=THRESHOLD:
            st['ml']=1; code[0]|=mask; code.append(text_buf[r])
        else:
            mp=st['mp']
            code.append(mp&255); code.append(((mp>>4)&0xF0)|(st['ml']-(THRESHOLD+1)))
        mask<<=1
        if mask==256:
            out+=bytes(code); code=[0]; mask=1
        last=st['ml']
        i=0
        while i<last and ip<n:
            c=data[ip]; ip+=1
            delete(s); text_buf[s]=c
            if s<F-1: text_buf[s+N]=c
            s=(s+1)&(N-1); r=(r+1)&(N-1)
            insert(r); i+=1
        while i<last:
            i+=1
            delete(s); s=(s+1)&(N-1); r=(r+1)&(N-1)
            ln-=1
            if ln: insert(r)
        if ln<=0: break
    if len(code)>1: out+=bytes(code)
    return bytes(out)


def compress(data, mode="optimal", F=18):
    if mode == "optimal":
        return compress_optimal(data, F)
    if mode == "okumura":
        return compress_okumura(data, F)
    raise ValueError("mode inconnu : %r" % mode)


# decodeur, uniquement pour verifier le resultat
def decompress_limited(src, outlen, start=0, limit=None, F=F):
    """Decodeur tolerant. Produit au plus `outlen` octets en ne lisant pas plus de `limit`
    octets de source (None = tout). S'arrete proprement si le flux est epuise ou si un
    token pointe hors de la fenetre de 1 Ko (le vrai decodeur lirait alors la WRAM).
    Retourne (donnees, octets_source_consommes, complet)."""
    START = N - F
    end = len(src) if limit is None else min(len(src), start + limit)
    buf = bytearray([0x20]) * START + bytearray(F)
    r = START
    out = bytearray()
    p = start
    flags = 0
    nbits = 0
    while len(out) < outlen:
        q = p
        if nbits == 0:
            if q >= end: break
            fl = src[q]; q += 1; nb = 8
        else:
            fl, nb = flags, nbits
        bit = fl & 1
        if bit:
            if q >= end: break
            c = src[q]; q += 1
            out.append(c); buf[r] = c; r = (r + 1) & (N - 1)
        else:
            if q + 1 >= end: break
            lo, hi = src[q], src[q + 1]; q += 2
            pos = lo | ((hi >> 4) << 8)
            if pos >= N: break
            for _ in range((hi & 0x0F) + THRESHOLD + 1):
                if len(out) >= outlen: break
                c = buf[pos]; pos = (pos + 1) & (N - 1)
                out.append(c); buf[r] = c; r = (r + 1) & (N - 1)
        flags, nbits, p = fl >> 1, nb - 1, q
    return bytes(out), p - start, len(out) >= outlen


def main():
    ap = argparse.ArgumentParser(description="Compresse un fichier en LZSS Konami (variantes A et B).")
    ap.add_argument("entree", help="fichier a compresser")
    ap.add_argument("sortie", help="fichier compresse")
    ap.add_argument("--variant", "-v", choices=("A", "B"), default="A", help="variante du codec (defaut A)")
    ap.add_argument("--mode", "-m", choices=("optimal", "okumura"), default="optimal")
    ap.add_argument("--max-size", type=lambda x: int(x, 0), help="echoue si le flux depasse cette taille (octets)")
    ap.add_argument("--no-verify", action="store_true", help="ne pas relire/decompresser le resultat")
    a = ap.parse_args()
    Fv = VARIANT_F[a.variant]

    data = open(a.entree, "rb").read()
    z = compress(data, a.mode, Fv)
    if data and not a.no_verify:
        back, used, ok = decompress_limited(z, len(data), 0, None, Fv)
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
