Konami GB LZSS

Tools to compress and decompress the LZSS format used by Konami on Game Boy, found in two ROMs: the Konami GB Collection Vol. 2 compilation (header GB PACK 2) and Castlevania Legends (USA/Europe, SGB Enhanced).

This repository contains no ROMs. You must supply your own.

The format

This is Haruhiko Okumura's LZSS with a 1024-byte window. It comes in three variants, which differ only in the starting write position inside the window and in how the bits of a match token are laid out:

Variant	Game	Write start	Max match	hi byte: position high bits / length − 3	Decoder in the ROM*
A	Track & Field	0x3EE (N−18)	18	bits 4–7 / bits 0–3	$25CE (entry points $25C3 / $25CA)
B	Parodius	0x3F0 (N−16)	16	bits 4–7 / bits 0–3	$3A5E / $3A65
C	Castlevania Legends	0x3DE (N−34)	34	bits 5–6 / bits 0–4 (bit 7 = 0)	$2ECD (list loader $2E53 / $2E59 / $2E5D)

*addresses in bank 0 of the respective ROM (Konami GB Collection Vol. 2; Castlevania Legends USA/Europe).

Match positions are absolute within the window: a stream of one variant cannot be read by the decoder of another.

Common details

Window initialized to $20 over [0, N−F[; the last F bytes are left uninitialized.
One flag byte per 8 items, read from bit 0 to bit 7: 1 = literal byte, 0 = match.
2-byte match lo, hi:
A and B: position = lo | (hi >> 4) << 8, length = (hi & 0x0F) + 3
C: position = lo | ((hi >> 5) & 3) << 8, length = (hi & 0x1F) + 3
No end marker: the decompressed length is supplied by the game (for tiles, tile count × 16).

Same engine family. All three games load data with the same kind of command list: entries of 7 bytes [cmd][bank][source][destination tile][tile count] terminated by $00, where command 1 means "compressed" and command 2 means "raw copy". Only the match token layout changes from one game to another.

Tools
Script	Purpose	Variants
tk_decompress.py	Decompresses a stream, from a file or directly from the ROM (bank + address)	A B C
tk_compress.py	Compresses a file (optimal or okumura mode) and checks the round trip	A B C
tk_extract.py	Extracts tiles and tilemaps from the command lists of the Konami GB Collection ROM	A
tk_scan.py	Finds every stream in the Konami GB Collection ROM and produces a CSV catalog	A B
cv_scan.py	Inventory of every compressed block in Castlevania Legends (walks the command lists and pointer tables)	C
tk_sm83_check.py	Checks streams against the game's real code, run by a mini SM83 emulator	A B C
tk_lzss.py	Library used by tk_extract.py / tk_scan.py	A B
Quick start
bash
# Decompress from the ROM (give the length: the format has no end marker)
python3 tk_decompress.py --rom game.gb --bank 0x18 --addr 0x406F tiles.bin --length 0x3E0                   # A, Track & Field
python3 tk_decompress.py --rom game.gb --bank 0x0E --addr 0x4000 tiles.bin --length 0x800 --variant B       # B, Parodius
python3 tk_decompress.py --rom cv.gb   --bank 0x01 --addr 0x460A map.bin   --length 0x240 --variant C       # C, Castlevania Legends

# Recompress, checking that the stream fits in the original space
python3 tk_compress.py tiles.bin tiles.lz --variant A --max-size 640
python3 tk_compress.py tiles.bin tiles.lz --variant C --mode okumura

# Catalog every stream
python3 tk_scan.py konami_collection.gb output/        # variants A and B
python3 cv_scan.py castlevania_legends.gb output/      # variant C (add --verify to replay every stream in the emulator)

Notes:

Always pass --length (or --limit) when decompressing. In variant C every 10-bit position is valid, so without a length the decoder simply runs to the end of the bank.
Modes: optimal gives the smallest possible stream (exact byte-optimal parsing, counting flag bytes); okumura is a port of the reference encoder and reproduces the size of the original streams.
In-place patching: use --max-size with the space available. If a stream does not fit, it has to be relocated (and its pointer in the command list updated) or shrunk.
What has been found
ROM	Streams	Where
Konami GB Collection Vol. 2	70 (61 × A, 9 × B)	banks $0A–$0E, $16–$19, $2E
Castlevania Legends (USA/Europe)	35 × C	banks $01, $0A, $0C, $0D; 67 command lists, plus 167 raw-copy entries
Reliability
Every stream produced by tk_compress.py was decoded correctly by the games' original routines, run in the repository's SM83 emulator, for the three variants. For Castlevania Legends this covers all 35 streams, each checked as original, optimal and okumura. The C check simulates only one RAM routine ($DEC9, a memory fill); everything else is the game's own code. No full-emulator run of the games was performed.
The okumura mode reproduces the exact total size of the original streams (28 397 bytes for A, 8 073 for B, 33 139 for C) and is byte-identical to the original on 50/61 (A), 5/9 (B) and 10/35 (C) streams; the others differ only in which of two equal-length matches is chosen.
The optimal mode produces 99.0–99.3 % of the original size.
Decoder addresses are specific to the ROM versions above; cv_scan.py checks a signature before running.
Credits

Format derived from Haruhiko Okumura's LZSS.C (1989). Game Boy, Track & Field, Parodius, Castlevania and Konami are trademarks of their respective owners; this project is not affiliated with Konami or Nintendo.
