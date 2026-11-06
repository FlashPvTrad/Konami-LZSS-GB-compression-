#!/usr/bin/env python3
"""
tk_decompress.py - decompresse un flux LZSS de Konami pour Game Boy (trois variantes : A, B, C).

Format commun : LZSS d'Okumura, fenetre circulaire de 1024 octets initialisee a $20, un octet de
flags pour 8 elements lu du bit 0 au bit 7 (1 = octet litteral, 0 = match sur 2 octets lo, hi).
Les variantes ne different que par la position d'ecriture initiale et la repartition des bits du match :

  Variante  Jeu                       Depart   Match   hi = octet 2 : position haute / longueur-3
  A         Track & Field             $3EE     3..18   bits 4-7 / bits 0-3
  B         Parodius                  $3F0     3..16   bits 4-7 / bits 0-3
  C         Castlevania Legends       $3DE     3..34   bits 5-6 / bits 0-4      (bit 7 = 0)

Il n'y a pas de marqueur de fin : il faut donner la longueur decompressee (--length). Sans elle, le
programme decode jusqu'a la premiere anomalie ou jusqu'a la fin de la banque (--limit borne la lecture).
Attention : en variante C, toute position de 10 bits est valide, donc il n'y a en pratique aucune anomalie
a detecter et le decodage va jusqu'a la fin de la banque ; donnez toujours --length (ou --limit).

Exemples :
  # depuis un fichier contenant un flux brut
  tk_decompress.py flux.lz sortie.bin --length 0x120
  # directement depuis la ROM (banque, adresse CPU en $4000-$7FFF)
  tk_decompress.py --rom jeu.gb --bank 0x18 --addr 0x43F1 map.bin --length 0x120
  tk_decompress.py --rom jeu.gb --bank 0x0E --addr 0x4000 tuiles.bin --length 0x800 --variant B
  tk_decompress.py --rom cv.gb  --bank 0x01 --addr 0x460A tilemap.bin --length 0x240 --variant C
  # depuis un offset de fichier de la ROM
  tk_decompress.py jeu.gb sortie.bin --offset 0x62DF1 --length 0x120
"""
import argparse, sys

N = 1024          # taille de la fenetre
THRESHOLD = 2     # longueur min d'un match = 3
# variante : (F, decalage de la position haute, masque de la position haute, masque de la longueur)
# F = longueur de match max ; position d'ecriture initiale = N - F
VARIANTS = {"A": (18, 4, 0x0F, 0x0F), "B": (16, 4, 0x0F, 0x0F), "C": (34, 5, 0x03, 0x1F)}


def decompress_limited(src, outlen, start=0, limit=None, variant="A"):
    """Decodeur tolerant. Produit au plus `outlen` octets en ne lisant pas plus de `limit`
    octets de source (None = tout). S'arrete proprement si le flux est epuise ou si un
    token pointe hors de la fenetre de 1 Ko (le vrai decodeur lirait alors la WRAM).
    Retourne (donnees, octets_source_consommes, complet)."""
    F, ps, pm, lm = VARIANTS[variant]
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
        if fl & 1:
            if q >= end: break
            c = src[q]; q += 1
            out.append(c); buf[r] = c; r = (r + 1) & (N - 1)
        else:
            if q + 1 >= end: break
            lo, hi = src[q], src[q + 1]; q += 2
            pos = lo | (((hi >> ps) & pm) << 8)
            if pos >= N: break
            for _ in range((hi & lm) + THRESHOLD + 1):
                if len(out) >= outlen: break
                c = buf[pos]; pos = (pos + 1) & (N - 1)
                out.append(c); buf[r] = c; r = (r + 1) & (N - 1)
        flags, nbits, p = fl >> 1, nb - 1, q
    return bytes(out), p - start, len(out) >= outlen


def decompress(src, outlen, start=0, variant="A"):
    """Decompresse `outlen` octets depuis src[start:].
    Retourne (donnees, octets_source_consommes). Leve ValueError si le flux est invalide."""
    data, used, ok = decompress_limited(src, outlen, start, None, variant)
    if not ok:
        raise ValueError("flux invalide ou tronque (%d/%d octets produits)" % (len(data), outlen))
    return data, used


def rom_offset(bank, addr):
    """Banque ROM + adresse CPU ($4000-$7FFF) -> offset dans le fichier."""
    return bank * 0x4000 + (addr - 0x4000) if addr >= 0x4000 else addr


def main():
    ap = argparse.ArgumentParser(description="Decompresse un flux LZSS Konami (variantes A, B et C).")
    ap.add_argument("entree", nargs="?", help="fichier contenant le flux (inutile avec --rom)")
    ap.add_argument("sortie", help="fichier de sortie")
    ap.add_argument("--rom", help="ROM source : le flux est lu a --bank/--addr")
    ap.add_argument("--bank", type=lambda x: int(x, 0), default=0, help="banque ROM (avec --rom)")
    ap.add_argument("--addr", type=lambda x: int(x, 0), help="adresse CPU du flux (avec --rom)")
    ap.add_argument("--offset", type=lambda x: int(x, 0), default=0, help="offset de depart dans le fichier d'entree")
    ap.add_argument("--length", type=lambda x: int(x, 0), help="nombre d'octets a produire")
    ap.add_argument("--variant", "-v", choices=sorted(VARIANTS), default="A",
                    help="variante du codec : A Track & Field (defaut), B Parodius, C Castlevania Legends")
    ap.add_argument("--limit", type=lambda x: int(x, 0), help="nombre maximal d'octets compresses lus")
    a = ap.parse_args()

    if a.rom:
        if a.addr is None:
            ap.error("--rom demande --addr")
        data = open(a.rom, "rb").read()
        start = rom_offset(a.bank, a.addr)
        end = (a.bank * 0x4000 + 0x4000) if (a.bank and a.addr >= 0x4000) else 0x4000   # un flux ne franchit pas sa banque
        src = data[:end]
        origin = "%02X:%04X" % (a.bank, a.addr)
    else:
        if not a.entree:
            ap.error("donner un fichier d'entree ou --rom")
        src = open(a.entree, "rb").read()
        start = a.offset
        origin = "%s+0x%X" % (a.entree, a.offset)

    outlen = a.length if a.length is not None else 1 << 24
    out, used, ok = decompress_limited(src, outlen, start, a.limit, a.variant)
    if a.length is not None and not ok:
        sys.exit("erreur : flux invalide ou tronque apres %d octets (sur %d demandes)" % (len(out), a.length))
    open(a.sortie, "wb").write(out)
    note = "" if a.length is not None else "  [longueur non fournie : decode jusqu'a la premiere anomalie ou la fin de la banque]"
    print("variante %s : %s -> %d octets decompresses (%d octets compresses lus, fin a l'offset de fichier 0x%X)%s" %
          (a.variant, origin, len(out), used, start + used, note))


if __name__ == "__main__":
    main()
