#!/usr/bin/env python3
"""
tk_sm83_check.py - verifie tk_lzss contre le VRAI code du jeu.

Un mini-emulateur Game Boy (jeu d'instructions SM83, sans timing, sans I/O) execute la routine
de decompression de la ROM ($25C3/$25CA -> $25CE... $267C, bank 0) sur des flux compresses, puis
compare ce que le jeu ecrit en memoire avec les donnees attendues.

Usage : tk_sm83_check.py ROM.gb dossier     dossier produit par tk_extract.py (variante A)
                                            ou par tk_scan.py (catalogue complet, variantes A et B)
"""
import os, sys, glob, random
from tk_lzss import compress, decompress

HALF_CARRY_IGNORED = True


class CPU:
    def __init__(s, mem):
        s.m = mem
        s.r = [0] * 8                      # B C D E H L (index 6 inutilise) A
        s.sp = s.pc = 0
        s.z = s.c = 0
        s.steps = 0

    # --- memoire
    def rd(s, a):
        a &= 0xFFFF
        return 0 if a == 0xFF41 else s.m[a]          # STAT : mode 0 (HBlank), VRAM accessible

    def wr(s, a, v):
        a &= 0xFFFF
        if a >= 0x8000:                               # ROM / registres MBC : ecritures ignorees
            s.m[a] = v & 0xFF

    def fetch(s):
        v = s.m[s.pc]; s.pc = (s.pc + 1) & 0xFFFF; return v

    def fetch16(s):
        lo = s.fetch(); return lo | (s.fetch() << 8)

    def push(s, v):
        s.sp = (s.sp - 1) & 0xFFFF; s.wr(s.sp, v >> 8)
        s.sp = (s.sp - 1) & 0xFFFF; s.wr(s.sp, v & 0xFF)

    def pop(s):
        lo = s.rd(s.sp); s.sp = (s.sp + 1) & 0xFFFF
        hi = s.rd(s.sp); s.sp = (s.sp + 1) & 0xFFFF
        return lo | (hi << 8)

    # --- registres
    def get(s, i): return s.rd(s.hl()) if i == 6 else s.r[i]
    def set(s, i, v):
        if i == 6: s.wr(s.hl(), v)
        else: s.r[i] = v & 0xFF
    def hl(s): return s.r[4] << 8 | s.r[5]
    def sethl(s, v): s.r[4], s.r[5] = (v >> 8) & 0xFF, v & 0xFF
    def rp(s, i):
        return (s.sp if i == 3 else s.r[2 * i] << 8 | s.r[2 * i + 1])
    def setrp(s, i, v):
        v &= 0xFFFF
        if i == 3: s.sp = v
        else: s.r[2 * i], s.r[2 * i + 1] = v >> 8, v & 0xFF
    def rp2(s, i):
        return (s.r[7] << 8 | s.z << 7 | s.c << 4) if i == 3 else s.rp(i)
    def setrp2(s, i, v):
        if i == 3: s.r[7], s.z, s.c = v >> 8, (v >> 7) & 1, (v >> 4) & 1
        else: s.setrp(i, v)

    def cc(s, i): return (not s.z, s.z, not s.c, s.c)[i]

    def alu(s, y, v):
        a = s.r[7]
        if y in (0, 1):
            res = a + v + (s.c if y == 1 else 0); s.c = res > 0xFF
        elif y in (2, 3, 7):
            res = a - v - (s.c if y == 3 else 0); s.c = res < 0
        elif y == 4: res = a & v; s.c = 0
        elif y == 5: res = a ^ v; s.c = 0
        else: res = a | v; s.c = 0
        s.z = (res & 0xFF) == 0
        if y != 7: s.r[7] = res & 0xFF

    def cb(s):
        op = s.fetch(); x, y, z = op >> 6, (op >> 3) & 7, op & 7
        v = s.get(z)
        if x == 0:
            if y == 0: s.c = v >> 7; v = ((v << 1) | s.c) & 255
            elif y == 1: s.c = v & 1; v = (v >> 1) | (s.c << 7)
            elif y == 2: nc = v >> 7; v = ((v << 1) | s.c) & 255; s.c = nc
            elif y == 3: nc = v & 1; v = (v >> 1) | (s.c << 7); s.c = nc
            elif y == 4: s.c = v >> 7; v = (v << 1) & 255
            elif y == 5: s.c = v & 1; v = (v >> 1) | (v & 0x80)
            elif y == 6: v = ((v << 4) | (v >> 4)) & 255; s.c = 0
            else: s.c = v & 1; v >>= 1
            s.z = v == 0; s.set(z, v)
        elif x == 1: s.z = not (v >> y) & 1
        elif x == 2: s.set(z, v & ~(1 << y))
        else: s.set(z, v | (1 << y))

    def step(s):
        s.steps += 1
        op = s.fetch(); x, y, z = op >> 6, (op >> 3) & 7, op & 7; p, q = y >> 1, y & 1
        if op == 0xCB: return s.cb()
        if x == 1:
            if op == 0x76: raise RuntimeError("HALT")
            return s.set(y, s.get(z))
        if x == 2: return s.alu(y, s.get(z))
        if x == 0:
            if z == 0:
                if y == 0: return
                if y == 3 or y >= 4:
                    e = s.fetch(); e -= 256 if e > 127 else 0
                    if y == 3 or s.cc(y - 4): s.pc = (s.pc + e) & 0xFFFF
                    return
                raise NotImplementedError("op %02X" % op)
            if z == 1:
                if q == 0: return s.setrp(p, s.fetch16())
                res = s.hl() + s.rp(p); s.c = res > 0xFFFF; return s.sethl(res)
            if z == 2:
                if p == 0: a = s.rp(0)
                elif p == 1: a = s.rp(1)
                else: a = s.hl()
                if q == 0: s.wr(a, s.r[7])
                else: s.r[7] = s.rd(a)
                if p == 2: s.sethl(a + 1)
                if p == 3:
                    a = s.hl()
                    if q == 0: s.wr(a, s.r[7])
                    else: s.r[7] = s.rd(a)
                    s.sethl(a - 1)
                return
            if z == 3: return s.setrp(p, s.rp(p) + (1 if q == 0 else -1))
            if z == 4: v = (s.get(y) + 1) & 255; s.z = v == 0; return s.set(y, v)
            if z == 5: v = (s.get(y) - 1) & 255; s.z = v == 0; return s.set(y, v)
            if z == 6: return s.set(y, s.fetch())
            if op == 0x37: s.c = 1; return
            raise NotImplementedError("op %02X" % op)
        # x == 3
        if z == 0:
            if y < 4:
                if s.cc(y): s.pc = s.pop()
                return
            if y == 4: return s.wr(0xFF00 + s.fetch(), s.r[7])                # LDH (a8),A
            if y == 6: s.r[7] = s.rd(0xFF00 + s.fetch()); return              # LDH A,(a8)
            raise NotImplementedError("op %02X" % op)
        if z == 1:
            if q == 0: return s.setrp2(p, s.pop())
            if p == 0: s.pc = s.pop(); return
            raise NotImplementedError("op %02X" % op)
        if z == 2:
            if y < 4:
                a = s.fetch16()
                if s.cc(y): s.pc = a
                return
            if y == 4: return s.wr(0xFF00 + s.r[1], s.r[7])                   # LD (C),A
            if y == 6: s.r[7] = s.rd(0xFF00 + s.r[1]); return                 # LD A,(C)
            if y == 5: return s.wr(s.fetch16(), s.r[7])
            if y == 7: s.r[7] = s.rd(s.fetch16()); return
            raise NotImplementedError("op %02X" % op)
        if z == 3:
            if y == 0: s.pc = s.fetch16(); return
            if y in (6, 7): return                    # DI / EI
            raise NotImplementedError("op %02X" % op)
        if z == 4:
            a = s.fetch16()
            if y < 4 and s.cc(y): s.push(s.pc); s.pc = a
            return
        if z == 5:
            if q == 0: return s.push(s.rp2(p))
            if p == 0:
                a = s.fetch16(); s.push(s.pc); s.pc = a; return
        if z == 6: return s.alu(y, s.fetch())
        raise NotImplementedError("op %02X" % op)


SENTINEL = 0xFFE0


def run_game_decoder(rom, bank_data, lz, outlen, dest, entry):
    """Execute le decodeur du jeu. rom = bank 0 ; lz = flux place en $4000 ; entry = $25C3 (VRAM) ou $25CA (WRAM).
    Retourne (octets ecrits en dest, octets source consommes, nb d'instructions)."""
    mem = bytearray(0x10000)
    mem[0:0x4000] = rom[0:0x4000]
    mem[0x4000:0x4000 + len(lz)] = lz
    rnd = random.Random(len(lz) * 31 + outlen)
    for a in range(0xC000, 0xE000):                       # WRAM remplie de bruit : le decodeur ne doit
        mem[a] = rnd.randrange(256)                       # jamais dependre des 18 octets non initialises
    for a in range(0x8000, 0xA000):
        mem[a] = 0xEE
    cpu = CPU(mem)
    cpu.r[4], cpu.r[5] = 0x40, 0x00                       # HL = source
    cpu.r[2], cpu.r[3] = dest >> 8, dest & 0xFF           # DE = destination
    cpu.r[0], cpu.r[1] = outlen >> 8, outlen & 0xFF       # BC = longueur de sortie
    cpu.sp = 0xDFF0
    cpu.push(SENTINEL)
    cpu.pc = entry
    while cpu.pc != SENTINEL:
        cpu.step()
        if cpu.steps > 20_000_000:
            raise RuntimeError("trop d'instructions")
    srcptr = mem[0xD424] | mem[0xD425] << 8
    return bytes(mem[dest:dest + outlen]), srcptr - 0x4000, cpu.steps


def run_game_decoder_B(rom, lz, outlen, dest):
    """Variante B : entree $3A65 (ecriture directe) avec HL=source, DE=destination, BC=longueur."""
    mem = bytearray(0x10000)
    mem[0:0x4000] = rom[0:0x4000]
    mem[0x4000:0x4000 + len(lz)] = lz
    rnd = random.Random(len(lz) * 17 + outlen)
    for a in range(0xC000, 0xE000):
        mem[a] = rnd.randrange(256)
    cpu = CPU(mem)
    cpu.r[4], cpu.r[5] = 0x40, 0x00
    cpu.r[2], cpu.r[3] = dest >> 8, dest & 0xFF
    cpu.r[0], cpu.r[1] = outlen >> 8, outlen & 0xFF
    cpu.sp = 0xDFF0
    cpu.push(SENTINEL)
    cpu.pc = 0x3A65
    while cpu.pc != SENTINEL:
        cpu.step()
        if cpu.steps > 30_000_000:
            raise RuntimeError("trop d'instructions")
    srcptr = mem[0xCBAA] | mem[0xCBAB] << 8              # pointeur source du decodeur B
    return bytes(mem[dest:dest + outlen]), srcptr - 0x4000, cpu.steps


def check_scan(rom, d):
    """Verifie tous les flux de streams.csv (dossier produit par tk_scan.py) avec les vrais decodeurs."""
    import csv
    ok = bad = 0
    for r in csv.DictReader(open(os.path.join(d, "streams.csv"))):
        name = "b%s_%s" % (r["banque"], r["adresse"])
        base = os.path.join(d, r["variante"], name)
        lz = open(base + ".lz", "rb").read()
        data = open(base + ".bin", "rb").read()
        if r["variante"] == "A":
            got, used, _ = run_game_decoder(rom, None, lz, len(data), 0x8800, 0x25CA)
        else:
            got, used, _ = run_game_decoder_B(rom, lz, len(data), 0x8800)
        good = got == data
        ok += good; bad += (not good)
        if not good:
            print("ECHEC %s %s:%s (%s) consomme %d/%d" % (r["variante"], r["banque"], r["adresse"], r["source"], used, len(lz)))
    print("decodeurs du jeu sur le catalogue : %d flux corrects, %d en echec" % (ok, bad))


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    rom = open(sys.argv[1], "rb").read()
    d = sys.argv[2]
    if os.path.exists(os.path.join(d, "streams.csv")):
        return check_scan(rom, d)
    files = sorted(glob.glob(os.path.join(d, "lz", "b*.lz")))
    ok = bad = 0
    instr = 0
    for f in files:
        name = os.path.basename(f)[:-3]
        lz = open(f, "rb").read()
        data = open(os.path.join(d, "tiles", name + ".bin"), "rb").read()
        tests = [("jeu (flux d'origine)", lz)]
        for mode in ("optimal", "okumura"):
            tests.append(("tk_lzss %s" % mode, compress(data, mode)))
        for label, stream in tests:
            for entry, dest in ((0x25C3, 0x8800), (0x25CA, 0xC800)):
                got, used, n = run_game_decoder(rom, None, stream, len(data), dest, entry)
                instr += n
                good = (got == data and used == len(stream))
                ok += good; bad += (not good)
                if not good:
                    print("ECHEC", name, label, "entry %04X" % entry, "consomme %d/%d" % (used, len(stream)))
    rnd = random.Random(7)
    for k in range(20):                                   # donnees aleatoires structurees
        n = rnd.choice([1, 2, 17, 100, 1023, 1024, 1025, 3000])
        data = bytes(rnd.choice(b"\x00\xff\x55\xaa\x20") if rnd.random() < .8 else rnd.randrange(256) for _ in range(n))
        for mode in ("optimal", "okumura"):
            z = compress(data, mode)
            got, used, _ = run_game_decoder(rom, None, z, n, 0x9000, 0x25C3)
            good = got == data and used == len(z)
            ok += good; bad += (not good)
            if not good: print("ECHEC aleatoire", k, n, mode)
    print("%d executions du decodeur du jeu (%d millions d'instructions SM83) : %d correctes, %d en echec" %
          (ok + bad, instr // 1_000_000, ok, bad))


if __name__ == "__main__":
    main()
