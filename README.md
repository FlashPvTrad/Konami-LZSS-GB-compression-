# Konami GB LZSS

Tools to **compress and decompress** the LZSS format used by Konami on Game Boy,
found in the *Konami GB Collection Vol. 2* compilation (header `GB PACK 2`).

> This repository contains no ROMs. You must supply your own.

## The format

This is Haruhiko Okumura's LZSS with a 1024-byte window. It comes in two variants,
which differ only in the starting write position inside the window:

| Variant | Game | Write start | Max match | Decoder in the ROM* |
|:-:|---|:-:|:-:|---|
| **A** | **Track & Field** | `0x3EE` (N−18) | 18 bytes | `$25CE` (entry points `$25C3` / `$25CA`) |
| **B** | **Parodius** | `0x3F0` (N−16) | 16 bytes | `$3A5E` / `$3A65` |

\*addresses in bank 0 of the Konami GB Collection Vol. 2 compilation ROM.

Match positions are **absolute** within the window: a variant A stream therefore cannot be read
by the B decoder, and vice versa.

**Common details**
- Window initialized to `$20` over `[0, N−F[`; the last F bytes are left uninitialized.
- One flag byte per 8 items, read from bit 0 to bit 7: `1` = literal byte, `0` = match.
- 2-byte match `lo`, `hi`: position = `lo | (hi >> 4) << 8`, length = `(hi & 0x0F) + 3`.
- **No end marker**: the decompressed length is supplied by the game (for tiles, tile count × 16).

## Tools

| Script | Purpose |
|---|---|
| `tk_decompress.py` | Decompresses a stream, from a file or directly from the ROM (bank + address) |
| `tk_compress.py` | Compresses a file (`optimal` or `okumura` mode) and checks the round trip |
| `tk_extract.py` | Extracts variant A tiles and tilemaps from the command lists |
| `tk_scan.py` | Finds every stream (A and B) in a ROM and produces a CSV catalog |
| `tk_sm83_check.py` | Checks streams against the **game's real code**, run by a mini SM83 emulator |

## Quick start

```bash
# Decompress from the ROM (variant A, Track & Field)
python3 tk_decompress.py --rom game.gb --bank 0x18 --addr 0x406F tiles.bin --length 0x3E0

# Variant B (Parodius)
python3 tk_decompress.py --rom game.gb --bank 0x0E --addr 0x4000 tiles.bin --length 0x800 --variant B

# Recompress, checking that the stream fits in the original space
python3 tk_compress.py tiles.bin tiles.lz --variant A --max-size 640

# Catalog every stream in a ROM
python3 tk_scan.py game.gb output/
```

## Reliability

Streams produced by `tk_compress.py` were successfully decoded by the game's original routines,
run in the repository's SM83 emulator (variants A and B). The `okumura` mode reproduces
the exact size of the original streams; the `optimal` mode gives the smallest possible stream.

## Credits

Format derived from Haruhiko Okumura's LZSS.C (1989). Game Boy, Track & Field, Parodius and Konami
are trademarks of their respective owners; this project is not affiliated with Konami or Nintendo.
