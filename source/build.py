import argparse
import struct
import sys
from pathlib import Path

from PIL import Image

SOURCE_FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE_FOLDER))

import bps
from rom import (MAIN_SPRITE_DIRECTORY, OBJECT_PALETTE_DIRECTORY, ROM_BASE, Boktai3Rom, read_instruction)
from thumb import NOP, R0, R1, R2, R3, R4, R5, R6, R7, R8, ThumbAssembler

DEFAULT_OUTPUT_FOLDER = SOURCE_FOLDER.parent / "out"
OUTPUT_NAME = "boktai3_dark_gun"
PHANTOM_PICTURE = SOURCE_FOLDER / "art" / "gun_frame_phantom.png"

FREE_SPACE_START = 0x08E88200
FREE_SPACE_END = 0x09000000
TRAMPOLINE_SIZE = 256

PHANTOM_FRAME = 13
DARK_LENS = 8
FRAME_TWO = 2
SABATA = 6

DJANGOS_HOME_SCRIPT = 0x09B8
CHEST_SCRIPT = 0x045F
CHEST_HOLDS_LENS = 4
CHEST_HOLDS_FRAME = 5
CHEST_FACING = 2
CHEST_REMEMBERED_BY_FLAG = 0x01000102
PHANTOM_CHEST_FLAG = 240
DARK_LENS_CHEST_FLAG = 241
PHANTOM_CHEST_POSITION = (512, 768)
DARK_LENS_CHEST_POSITION = (768, 512)
EXISTING_CHEST_ARGUMENTS = [CHEST_HOLDS_LENS, 1, 512, 512, CHEST_FACING, 0, 0, CHEST_REMEMBERED_BY_FLAG, 205, 0]
SCRIPT_BLOCK = 0x8E
SCRIPT_CALL = 0x70
SCRIPT_CALL_WITH_LENGTH_BYTE = 0x7D

GUN_PAGE_SPRITES = 0x2BEC
GUN_PAGE_FIRST_PALETTE = 841
PALETTE_FILE_HEADER = 4
PHANTOM_POSE_FROM_SET = 9
GRIP_PALETTE_ROW = 13
PHANTOM_PALETTE_ROW = 15
PHANTOM_SCREEN_POSITION = (11, 76)
SOLAR_LENS_POSE = 0
SPRITE_16X16 = 0x04
SPRITE_32X32 = 0x08
TILE_SHEET_WIDTH = 16
TILE_BYTES = 32
LENS_STEM_SCREEN_Y = 80
LENS_STEM_COLOUR, DARK_LENS_STEM_COLOUR = 8, 2
DARK_LENS_COLOURS = [0x0000, 0x0000, 0x1840, 0x20C1, 0x2D24, 0x3987, 0x45CA, 0x01FA, 0x3C19,
                     0x3C16, 0x3C14, 0x402F, 0x446B, 0x48A6, 0x3C63, 0x3040]

PLAYER_KIND_HIGH_BYTE = 0x83
PLAYER_KIND = 0x418
PLAYER_NUMBER = 0x2C
GUN_FRAME = 0xBF4
HAS_GUN = 0xBF5
GUN_LENS = 0xC18
GUN_HANDLER = 0xC88
GUN_STATE = 0x457
CHARGE_LEVEL = 0xC0F
CHARGE_CENTRE = 0xA80
GUN_EFFECT_SPRITE = 0x828
GUN_EFFECT_CALLBACK = 0x864
MENU_LENS_SPRITE = 0x42A4
MENU_LENS_PALETTE_COPY = 0x4364

SABATA_GUN_HANDLER = 0x0814A9D5
COMMON_GUN_HANDLER = 0x0814E3D9
SET_SPRITE_POSE = 0x0821980C
COPY_MEMORY = 0x08248594
COPY_PALETTE_ARGUMENTS = 0x04000008
OBJECT_PALETTES_POINTER = 0x030042E4
GUN_FRAME_TABLE = 0x08E60F4C
GUN_EFFECT_READY = 0x0813BFE1
SUN_EFFECT_PALETTE, DARK_EFFECT_PALETTE = 0x4B, 0x51
LENS_PALETTES_FROM_KIND_6 = 0xC5
GUN_MUZZLE_POSITION = 0x08159284
FIRE_SHOT = 0x0814DFDC
SET_GUN_STATE = 0x0813B03C
SPAWN_CHARGE_PARTICLE = 0x0813C5B4
SABATA_HANDLER_STATE_TABLE = 0x0814AA08
SABATA_HANDLER_CHARGING_STATE = 2
SABATA_HANDLER_AFTER_SHOT_STATE = 5
UNAFFORDABLE_COST = 0x7FFF

LOAD_R1_FROM_HANDLER_LITERAL = 0x490C
BRANCH_EXCHANGE_R1 = 0x4708


class PatchedRom:
    def __init__(self, data: bytearray):
        self.data = data
        self.next_free_address = FREE_SPACE_START
        self.relocated_regions: list = []

    def allocate(self, size: int, alignment: int = 4) -> int:
        address = (self.next_free_address + alignment - 1) & ~(alignment - 1)
        self.next_free_address = address + size
        assert self.next_free_address <= FREE_SPACE_END, "out of free space"
        assert not any(self.data[address - ROM_BASE:address - ROM_BASE + size]), "free space not empty at %08X" % address
        return address

    def write(self, address: int, data: bytes) -> None:
        self.data[address - ROM_BASE:address - ROM_BASE + len(data)] = data

    def read_u16(self, address: int) -> int:
        return struct.unpack_from("<H", self.data, address - ROM_BASE)[0]

    def read_u32(self, address: int) -> int:
        return struct.unpack_from("<I", self.data, address - ROM_BASE)[0]

    def write_u32(self, address: int, value: int) -> None:
        struct.pack_into("<I", self.data, address - ROM_BASE, value & 0xFFFFFFFF)


def encode_script_constant(value: int) -> bytes:
    if 0 <= value <= 62:
        return bytes([0xC1 + value])
    if 0 <= value <= 0xFF:
        return bytes([2, value])
    if -0x8000 <= value <= 0x7FFF:
        return bytes([1]) + struct.pack("<h", value)
    return bytes([9]) + struct.pack("<i", value)


def encode_script_call(script_id: int, arguments: list) -> bytes:
    body = struct.pack("<H", script_id) + b"".join(encode_script_constant(argument) for argument in arguments) + b"\x00"
    if len(body) < 0xD:
        return bytes([SCRIPT_CALL | len(body)]) + body
    assert len(body) <= 0xFF
    return bytes([SCRIPT_CALL_WITH_LENGTH_BYTE, len(body)]) + body


def chest(contents: int, item: int, position: tuple, flag: int) -> bytes:
    x, z = position
    return encode_script_call(CHEST_SCRIPT, [contents, item, x, z, CHEST_FACING, 0, 0, CHEST_REMEMBERED_BY_FLAG, flag, 0])


def add_chests_to_djangos_home(rom: PatchedRom, original: Boktai3Rom) -> None:
    entry_address = original.script_entry_address(DJANGOS_HOME_SCRIPT)
    entry = rom.read_u32(entry_address)
    script_start = entry & 0xFFFFFF
    script, script_end = read_instruction(original.bytecode, script_start)
    assert original.bytecode[script_start] == SCRIPT_BLOCK
    existing_chest = next(child for child in script.children if child.kind == "call" and child.value == CHEST_SCRIPT)
    assert [argument.value for argument in existing_chest.children] == EXISTING_CHEST_ARGUMENTS

    new_chests = (chest(CHEST_HOLDS_FRAME, PHANTOM_FRAME, PHANTOM_CHEST_POSITION, PHANTOM_CHEST_FLAG)
                  + chest(CHEST_HOLDS_LENS, DARK_LENS, DARK_LENS_CHEST_POSITION, DARK_LENS_CHEST_FLAG))
    body = bytes(original.bytecode[script_start + 3:script_end])
    insert_at = existing_chest.end - (script_start + 3)
    body = body[:insert_at] + new_chests + body[insert_at:]
    new_script = bytes([SCRIPT_BLOCK]) + struct.pack("<H", len(body)) + body

    address = rom.allocate(len(new_script), alignment=1)
    rom.write(address, new_script)
    new_offset = address - original.bytecode_address
    assert 0 < new_offset < 0x1000000
    rom.write_u32(entry_address, (entry & 0xFF000000) | new_offset)
    rom.relocated_regions.append((original.bytecode_address + script_start - ROM_BASE,
                                  original.bytecode_address + script_end - ROM_BASE))


def to_gba_colour(rgb: tuple) -> int:
    red, green, blue = (channel >> 3 for channel in rgb[:3])
    return red | green << 5 | blue << 10


def load_phantom_picture() -> tuple:
    image = Image.open(PHANTOM_PICTURE).convert("RGBA")
    width, height = image.size
    pixel_counts: dict = {}
    for y in range(height):
        for x in range(width):
            pixel = image.getpixel((x, y))
            if pixel[3]:
                pixel_counts[pixel[:3]] = pixel_counts.get(pixel[:3], 0) + 1

    replaced_by: dict = {}
    remaining = dict(pixel_counts)
    while len(remaining) > 15:
        rarest = min(remaining, key=lambda colour: (remaining[colour], colour))
        nearest = min((colour for colour in remaining if colour != rarest),
                      key=lambda colour: sum((a - b) ** 2 for a, b in zip(colour, rarest)))
        replaced_by[rarest] = nearest
        remaining[nearest] += remaining.pop(rarest)
    colours = sorted(remaining, key=lambda colour: -remaining[colour])

    def final_colour(colour):
        while colour in replaced_by:
            colour = replaced_by[colour]
        return colour

    indices = [[0] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            pixel = image.getpixel((x, y))
            if pixel[3]:
                indices[y][x] = 1 + colours.index(final_colour(pixel[:3]))
    palette = [0] + [to_gba_colour(colour) for colour in colours] + [0] * (15 - len(colours))
    return palette, indices, width, height


def encode_tile(indices: list, left: int, top: int) -> bytes:
    tile = bytearray()
    for y in range(8):
        for x in range(0, 8, 2):
            tile.append(indices[top + y][left + x] | indices[top + y][left + x + 1] << 4)
    return bytes(tile)


def write_to_tile_sheet(tiles: bytearray, first_tile: int, indices: list, width: int, height: int) -> None:
    for row in range(height // 8):
        for column in range(width // 8):
            tile = first_tile + TILE_SHEET_WIDTH * row + column
            if len(tiles) < TILE_BYTES * (tile + 1):
                tiles += bytes(TILE_BYTES * (tile + 1) - len(tiles))
            tiles[TILE_BYTES * tile:TILE_BYTES * (tile + 1)] = encode_tile(indices, 8 * column, 8 * row)


def read_from_tile_sheet(tiles: bytes, first_tile: int, width: int, height: int) -> list:
    indices = [[0] * width for _ in range(height)]
    for row in range(height // 8):
        for column in range(width // 8):
            tile = first_tile + TILE_SHEET_WIDTH * row + column
            for y in range(8):
                for x in range(0, 8, 2):
                    pair = tiles[TILE_BYTES * tile + 4 * y + x // 2]
                    indices[8 * row + y][8 * column + x] = pair & 15
                    indices[8 * row + y][8 * column + x + 1] = pair >> 4
    return indices


def add_gun_page_pictures(rom: PatchedRom, original: Boktai3Rom) -> int:
    file_start, file_end = original.files(MAIN_SPRITE_DIRECTORY)[GUN_PAGE_SPRITES]
    rom.relocated_regions.append((file_start - ROM_BASE, file_end - ROM_BASE))
    sprite_file = bytes(original.data[file_start - ROM_BASE:file_end - ROM_BASE])
    first_palette, pose_count, animation_count, sub_sprite_count, command_count, tile_count = struct.unpack_from("<6H", sprite_file, 0)
    poses_offset, _, sub_sprites_offset, _, tiles_offset = struct.unpack_from("<5I", sprite_file, 12)
    assert (first_palette, pose_count, animation_count, command_count) == (GUN_PAGE_FIRST_PALETTE, 14, 0, 0)

    poses = [list(struct.unpack_from("<HHhhhhhhI", sprite_file, poses_offset + 20 * index)) for index in range(pose_count)]
    sub_sprites = [list(struct.unpack_from("<BBhhH", sprite_file, sub_sprites_offset + 8 * index)) for index in range(sub_sprite_count)]
    tiles = bytearray(sprite_file[tiles_offset:tiles_offset + TILE_BYTES * tile_count])

    def sub_sprites_of(pose_index):
        first, count = poses[pose_index][8] // 8, poses[pose_index][1]
        return sub_sprites[first:first + count]

    first_new_row = (tile_count + TILE_SHEET_WIDTH - 1) // TILE_SHEET_WIDTH
    palette, indices, width, height = load_phantom_picture()
    phantom_sub_sprites = [sprite for sprite in sub_sprites_of(PHANTOM_POSE_FROM_SET) if sprite[4] >> 12 == GRIP_PALETTE_ROW]
    column = 0
    for block_top in range(0, height, 16):
        for block_left in range(0, width, 16):
            block = [[indices[y][x] if y < height and x < width else 0 for x in range(block_left, block_left + 16)]
                     for y in range(block_top, block_top + 16)]
            if not any(any(row) for row in block):
                continue
            first_tile = TILE_SHEET_WIDTH * first_new_row + column
            column += 2
            assert column <= TILE_SHEET_WIDTH
            write_to_tile_sheet(tiles, first_tile, block, 16, 16)
            phantom_sub_sprites.append([0, SPRITE_16X16, PHANTOM_SCREEN_POSITION[0] + block_left,
                                        PHANTOM_SCREEN_POSITION[1] + block_top, PHANTOM_PALETTE_ROW << 12 | first_tile])
    model = poses[PHANTOM_POSE_FROM_SET]
    poses.append([poses[13][0] + 1, len(phantom_sub_sprites), 0, 0, model[4], model[5], model[6], model[7], 8 * len(sub_sprites)])
    sub_sprites += phantom_sub_sprites

    dark_lens_sub_sprites = []
    for flip, shape, x, y, tile_and_row in sub_sprites_of(SOLAR_LENS_POSE):
        if shape == SPRITE_32X32:
            lens = read_from_tile_sheet(bytes(tiles), tile_and_row & 0xFFF, 32, 32)
            for row in range(32):
                if y + row >= LENS_STEM_SCREEN_Y:
                    lens[row] = [DARK_LENS_STEM_COLOUR if colour == LENS_STEM_COLOUR else colour for colour in lens[row]]
            first_tile = TILE_SHEET_WIDTH * (first_new_row + 2)
            write_to_tile_sheet(tiles, first_tile, lens, 32, 32)
            dark_lens_sub_sprites.append([flip, shape, x, y, (tile_and_row & 0xF000) | first_tile])
        else:
            dark_lens_sub_sprites.append([flip, shape, x, y, tile_and_row])
    model = poses[SOLAR_LENS_POSE]
    poses.append([model[0] + 1, len(dark_lens_sub_sprites), model[2], model[3], model[4], model[5], model[6], model[7], 8 * len(sub_sprites)])
    sub_sprites += dark_lens_sub_sprites
    tiles += bytes((-len(tiles)) % (TILE_BYTES * TILE_SHEET_WIDTH))

    new_poses_offset = 32
    new_sub_sprites_offset = new_poses_offset + 20 * len(poses)
    new_tiles_offset = new_sub_sprites_offset + 8 * len(sub_sprites)
    new_file = bytearray(struct.pack("<6H5I", first_palette, len(poses), 0, len(sub_sprites), 0, len(tiles) // TILE_BYTES,
                                     new_poses_offset, new_sub_sprites_offset, new_sub_sprites_offset, new_tiles_offset, new_tiles_offset))
    for pose in poses:
        new_file += struct.pack("<HHhhhhhhI", *pose)
    for sprite in sub_sprites:
        new_file += struct.pack("<BBhhH", *sprite)
    new_file += tiles
    address = rom.allocate(len(new_file))
    rom.write(address, bytes(new_file))
    directory, entry_address = original.directory_entry_address(MAIN_SPRITE_DIRECTORY, GUN_PAGE_SPRITES)
    rom.write_u32(entry_address, address - directory)

    (palette_file_start, palette_file_end), = original.files(OBJECT_PALETTE_DIRECTORY).values()
    palettes = palette_file_start + PALETTE_FILE_HEADER
    assert palette_file_end - palettes >= 32 * (GUN_PAGE_FIRST_PALETTE + PHANTOM_PALETTE_ROW + 1)
    rom.write(palettes + 32 * (GUN_PAGE_FIRST_PALETTE + PHANTOM_PALETTE_ROW), b"".join(struct.pack("<H", colour) for colour in palette))

    dark_lens_palette = rom.allocate(32)
    rom.write(dark_lens_palette, b"".join(struct.pack("<H", colour) for colour in DARK_LENS_COLOURS))
    return dark_lens_palette


def require_original_code(rom: PatchedRom, address: int, halfwords: list) -> None:
    found = [rom.read_u16(address + 2 * index) for index in range(len(halfwords))]
    assert found == halfwords, "%08X: %s, expected %s" % (address, ["%04X" % value for value in found], ["%04X" % value for value in halfwords])


def jump_from_game_code(rom: PatchedRom, site: int, target: int, register: int, replaced_length: int) -> None:
    literal = (site + 4 + 3) & ~3
    if literal < site + 4:
        literal += 4
    distance = literal - ((site + 4) & ~3)
    code = struct.pack("<HH", 0x4800 | register << 8 | distance >> 2, 0x4700 | register << 3)
    code += struct.pack("<H", NOP) * ((literal - site - 4) // 2) + struct.pack("<I", target | 1)
    assert len(code) <= replaced_length
    rom.write(site, code + struct.pack("<H", NOP) * ((replaced_length - len(code)) // 2))


def new_routine(rom: PatchedRom) -> ThumbAssembler:
    return ThumbAssembler(rom.allocate(TRAMPOLINE_SIZE))


def place_routine(rom: PatchedRom, routine: ThumbAssembler) -> int:
    code = routine.assemble()
    assert len(code) <= TRAMPOLINE_SIZE
    rom.write(routine.base_address, code)
    return routine.base_address


def load_player_kind(routine: ThumbAssembler, destination: int, scratch: int, player: int) -> None:
    routine.movs(scratch, PLAYER_KIND_HIGH_BYTE)
    routine.shift_left(scratch, scratch, 3)
    routine.add_registers(destination, player, scratch)
    routine.load_byte(destination, destination)


def field_address(routine: ThumbAssembler, destination: int, scratch: int, player: int, field: int) -> None:
    routine.load_constant(scratch, field)
    routine.add_registers(destination, player, scratch)


def load_player_field(routine: ThumbAssembler, destination: int, scratch: int, player: int, field: int) -> None:
    field_address(routine, destination, scratch, player, field)
    routine.load_byte(destination, destination)


def give_phantom_sabatas_gun_handler(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0813ECD8, [0x480B, 0x1821, 0x480B])
    assert rom.read_u32(0x0813ED0C) == COMMON_GUN_HANDLER
    routine = new_routine(rom)
    routine.load_constant(R1, GUN_HANDLER)
    routine.add_registers(R1, R4, R1)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_equal("sabatas_handler")
    routine.load_constant(R0, COMMON_GUN_HANDLER)
    routine.branch("store_handler")
    routine.label("sabatas_handler")
    routine.load_constant(R0, SABATA_GUN_HANDLER)
    routine.label("store_handler")
    routine.jump_to(0x0813ECDE, R2)
    address = place_routine(rom, routine)
    rom.write(0x0813ECD8, struct.pack("<HHH", LOAD_R1_FROM_HANDLER_LITERAL, BRANCH_EXCHANGE_R1, NOP))
    rom.write_u32(0x0813ED0C, address | 1)


def check_dark_pair_when_firing(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0814DFE6, [0x2183, 0x00C9, 0x1868, 0x7800, 0x2806, 0xD107])
    routine = new_routine(rom)
    load_player_kind(routine, R0, R1, R5)
    routine.cmp(R0, SABATA)
    routine.branch_if_not_equal("django")
    field_address(routine, R1, R2, R5, GUN_LENS)
    routine.movs(R0, DARK_LENS)
    routine.store_byte(R0, R1)
    field_address(routine, R1, R2, R5, GUN_FRAME)
    routine.movs(R0, FRAME_TWO)
    routine.store_byte(R0, R1)
    routine.branch("fire")
    routine.label("django")
    load_player_field(routine, R0, R2, R5, HAS_GUN)
    routine.cmp(R0, 0)
    routine.branch_if_equal("fire")
    load_player_field(routine, R0, R2, R5, GUN_FRAME)
    field_address(routine, R1, R2, R5, GUN_LENS)
    routine.load_byte(R1, R1)
    routine.movs(R2, 0)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_not_equal("frame_checked")
    routine.add_value(R2, 1)
    routine.label("frame_checked")
    routine.cmp(R1, DARK_LENS)
    routine.branch_if_not_equal("lens_checked")
    routine.add_value(R2, 1)
    routine.label("lens_checked")
    routine.cmp(R2, 1)
    routine.branch_if_not_equal("fire")
    routine.jump_to(0x0814E0AE)
    routine.label("fire")
    routine.jump_to(0x0814E002)
    jump_from_game_code(rom, 0x0814DFE6, place_routine(rom, routine), R0, 0x0814E002 - 0x0814DFE6)


def use_sabatas_shot_spawner(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0814E340, [0x2183, 0x00C9, 0x1868, 0x7800, 0x2806, 0xD109])
    routine = new_routine(rom)
    load_player_kind(routine, R0, R1, R5)
    routine.cmp(R0, SABATA)
    routine.branch_if_equal("sabatas_spawner")
    load_player_field(routine, R0, R2, R5, GUN_FRAME)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_equal("sabatas_spawner")
    routine.jump_to(0x0814E360)
    routine.label("sabatas_spawner")
    routine.jump_to(0x0814E34C)
    jump_from_game_code(rom, 0x0814E340, place_routine(rom, routine), R0, 0x0814E34C - 0x0814E340)


def draw_dark_lens_pose(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0817259C, [0x490E, 0x1878, 0x1C31, 0x2201, 0x2300])
    routine = new_routine(rom)
    routine.load_constant(R1, MENU_LENS_SPRITE)
    routine.add_registers(R0, R7, R1)
    routine.copy_register(R1, R6)
    routine.movs(R2, 1)
    routine.move_high(R3, R8)
    routine.cmp(R3, DARK_LENS)
    routine.branch_if_not_equal("set_pose")
    routine.movs(R2, 15)
    routine.label("set_pose")
    routine.movs(R3, 0)
    routine.call_far(SET_SPRITE_POSE)
    routine.jump_to(0x081725AA)
    routine.add_call_ip_veneer()
    jump_from_game_code(rom, 0x0817259C, place_routine(rom, routine), R2, 0x081725AA - 0x0817259C)


def copy_dark_lens_colours(rom: PatchedRom, dark_lens_palette: int) -> None:
    require_original_code(rom, 0x081725EC, [0x480D, 0x21C5, 0x0049, 0x4441, 0x0149])
    routine = new_routine(rom)
    routine.move_high(R3, R8)
    routine.cmp(R3, DARK_LENS)
    routine.branch_if_not_equal("game_palette")
    routine.load_constant(R0, dark_lens_palette)
    routine.branch("copy")
    routine.label("game_palette")
    routine.load_constant(R0, OBJECT_PALETTES_POINTER)
    routine.load_word(R0, R0)
    routine.movs(R1, LENS_PALETTES_FROM_KIND_6)
    routine.shift_left(R1, R1, 1)
    routine.add_high(R1, R8)
    routine.shift_left(R1, R1, 5)
    routine.add_registers(R0, R0, R1)
    routine.label("copy")
    routine.load_constant(R3, MENU_LENS_PALETTE_COPY)
    routine.add_registers(R1, R7, R3)
    routine.load_constant(R2, COPY_PALETTE_ARGUMENTS)
    routine.call_far(COPY_MEMORY)
    routine.jump_to(0x08172604)
    routine.add_call_ip_veneer()
    jump_from_game_code(rom, 0x081725EC, place_routine(rom, routine), R0, 0x08172604 - 0x081725EC)


def use_sabatas_shot_setup(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0814DE70, [0x2183, 0x00C9, 0x1868, 0x7800, 0x2806, 0xD109])
    routine = new_routine(rom)
    load_player_kind(routine, R0, R1, R5)
    routine.cmp(R0, SABATA)
    routine.branch_if_equal("sabatas_setup")
    routine.load_byte(R0, R6)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_equal("sabatas_setup")
    routine.jump_to(0x0814DE90)
    routine.label("sabatas_setup")
    routine.jump_to(0x0814DE7C)
    jump_from_game_code(rom, 0x0814DE70, place_routine(rom, routine), R0, 0x0814DE7C - 0x0814DE70)


def use_frame_twos_stun(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0814E298, [0x4A0A, 0x18A8, 0x7800, 0x2802, 0xD116])
    routine = new_routine(rom)
    load_player_field(routine, R0, R2, R5, GUN_FRAME)
    routine.cmp(R0, FRAME_TWO)
    routine.branch_if_equal("short_stun")
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_equal("short_stun")
    routine.jump_to(0x0814E2D0)
    routine.label("short_stun")
    routine.jump_to(0x0814E2A2)
    jump_from_game_code(rom, 0x0814E298, place_routine(rom, routine), R0, 0x0814E2A2 - 0x0814E298)


def dark_effect_palette_when_created(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0813C28E, [0x2183, 0x00C9, 0x1870, 0x7800, 0x2806, 0xD10A])
    routine = new_routine(rom)
    load_player_kind(routine, R0, R1, R6)
    routine.cmp(R0, SABATA)
    routine.branch_if_equal("dark")
    load_player_field(routine, R0, R1, R6, GUN_FRAME)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_equal("dark")
    routine.jump_to(0x0813C2B0)
    routine.label("dark")
    routine.jump_to(0x0813C29A)
    jump_from_game_code(rom, 0x0813C28E, place_routine(rom, routine), R0, 0x0813C29A - 0x0813C28E)


def dark_effect_palette_when_equipped(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0813ECE6, [0x490A, 0x1860, 0x7800, 0x2803, 0xD104])
    routine = new_routine(rom)
    field_address(routine, R1, R1, R4, GUN_EFFECT_CALLBACK)
    routine.load_word(R1, R1)
    routine.load_constant(R2, GUN_EFFECT_READY)
    routine.cmp_registers(R1, R2)
    routine.branch_if_not_equal("continue")
    routine.movs(R3, SUN_EFFECT_PALETTE)
    load_player_kind(routine, R0, R1, R4)
    routine.cmp(R0, SABATA)
    routine.branch_if_equal("dark")
    load_player_field(routine, R0, R1, R4, GUN_FRAME)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_not_equal("set_palette")
    routine.label("dark")
    routine.movs(R3, DARK_EFFECT_PALETTE)
    routine.label("set_palette")
    field_address(routine, R1, R1, R4, GUN_EFFECT_SPRITE)
    routine.store_halfword(R3, R1, 6)
    routine.load_constant(R2, OBJECT_PALETTES_POINTER)
    routine.load_word(R2, R2)
    routine.shift_left(R0, R3, 5)
    routine.add_registers(R2, R2, R0)
    routine.store_word(R2, R1, 0xC)
    routine.label("continue")
    load_player_field(routine, R0, R1, R4, GUN_STATE)
    routine.cmp(R0, 3)
    routine.branch_if_not_equal("done")
    routine.jump_to(0x0813ECF0)
    routine.label("done")
    routine.jump_to(0x0813ECFA)
    jump_from_game_code(rom, 0x0813ECE6, place_routine(rom, routine), R0, 0x0813ECF0 - 0x0813ECE6)


def charge_with_particles_only_as_a_pair(rom: PatchedRom) -> None:
    state_entry = SABATA_HANDLER_STATE_TABLE + 4 * SABATA_HANDLER_CHARGING_STATE
    original_state = 0x0814AAEE
    assert rom.read_u32(state_entry) == original_state
    routine = new_routine(rom)
    load_player_kind(routine, R0, R1, R5)
    routine.cmp(R0, SABATA)
    routine.branch_if_equal("charge")
    load_player_field(routine, R0, R1, R5, GUN_FRAME)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_not_equal("charge")
    load_player_field(routine, R0, R1, R5, GUN_LENS)
    routine.cmp(R0, DARK_LENS)
    routine.branch_if_equal("particles")
    routine.copy_register(R0, R5)
    routine.call_far(FIRE_SHOT)
    routine.copy_register(R0, R5)
    routine.movs(R1, SABATA_HANDLER_AFTER_SHOT_STATE)
    routine.call_far(SET_GUN_STATE)
    routine.jump_to(0x0814AFB2)
    routine.label("particles")
    routine.movs(R1, PLAYER_NUMBER)
    routine.add_registers(R0, R5, R1)
    routine.load_byte(R0, R0)
    field_address(routine, R1, R1, R5, CHARGE_CENTRE)
    routine.call_far(GUN_MUZZLE_POSITION)
    routine.copy_register(R0, R5)
    field_address(routine, R1, R1, R5, CHARGE_CENTRE)
    routine.call_far(SPAWN_CHARGE_PARTICLE)
    routine.label("charge")
    routine.jump_to(original_state)
    routine.add_call_ip_veneer()
    rom.write_u32(state_entry, place_routine(rom, routine))


def block_charge_for_a_mismatched_pair(rom: PatchedRom) -> None:
    require_original_code(rom, 0x0814E4D8, [0x1C02, 0x480C, 0x1829, 0x2000])
    routine = new_routine(rom)
    routine.copy_register(R2, R0)
    load_player_kind(routine, R0, R1, R5)
    routine.cmp(R0, SABATA)
    routine.branch_if_equal("continue")
    load_player_field(routine, R0, R1, R5, HAS_GUN)
    routine.cmp(R0, 0)
    routine.branch_if_equal("continue")
    routine.movs(R3, 0)
    load_player_field(routine, R0, R1, R5, GUN_FRAME)
    routine.cmp(R0, PHANTOM_FRAME)
    routine.branch_if_not_equal("frame_checked")
    routine.add_value(R3, 1)
    routine.label("frame_checked")
    load_player_field(routine, R0, R1, R5, GUN_LENS)
    routine.cmp(R0, DARK_LENS)
    routine.branch_if_not_equal("lens_checked")
    routine.add_value(R3, 1)
    routine.label("lens_checked")
    routine.cmp(R3, 1)
    routine.branch_if_not_equal("continue")
    routine.load_constant(R2, UNAFFORDABLE_COST)
    routine.label("continue")
    routine.load_constant(R0, CHARGE_LEVEL)
    routine.add_registers(R1, R5, R0)
    routine.movs(R0, 0)
    routine.jump_to(0x0814E4E0, R3)
    jump_from_game_code(rom, 0x0814E4D8, place_routine(rom, routine), R0, 0x0814E4E0 - 0x0814E4D8)


def match_frame_twos_energy_cost(rom: PatchedRom) -> None:
    phantom_entry = GUN_FRAME_TABLE + 4 * (PHANTOM_FRAME - 1)
    frame_two_entry = GUN_FRAME_TABLE + 4 * (FRAME_TWO - 1)
    assert bytes(rom.data[phantom_entry - ROM_BASE:phantom_entry - ROM_BASE + 4]) == bytes([13, 10, 3, 1])
    rom.write(phantom_entry + 2, bytes(rom.data[frame_two_entry - ROM_BASE + 2:frame_two_entry - ROM_BASE + 3]))


def patch_game_code(rom: PatchedRom, dark_lens_palette: int) -> None:
    give_phantom_sabatas_gun_handler(rom)
    check_dark_pair_when_firing(rom)
    use_sabatas_shot_spawner(rom)
    draw_dark_lens_pose(rom)
    copy_dark_lens_colours(rom, dark_lens_palette)
    use_sabatas_shot_setup(rom)
    use_frame_twos_stun(rom)
    dark_effect_palette_when_created(rom)
    dark_effect_palette_when_equipped(rom)
    charge_with_particles_only_as_a_pair(rom)
    block_charge_for_a_mismatched_pair(rom)
    match_frame_twos_energy_cost(rom)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Boktai 3 dark gun patch from a clean Japanese ROM.")
    parser.add_argument("--rom", required=True, type=Path)
    parser.add_argument("--out", default=DEFAULT_OUTPUT_FOLDER, type=Path)
    arguments = parser.parse_args()
    arguments.out.mkdir(parents=True, exist_ok=True)

    original = Boktai3Rom(arguments.rom)
    clean = bytes(original.data)
    rom = PatchedRom(bytearray(clean))
    add_chests_to_djangos_home(rom, original)
    dark_lens_palette = add_gun_page_pictures(rom, original)
    patch_game_code(rom, dark_lens_palette)
    patched = bytes(rom.data)

    patch = bps.encode(clean, patched, "Boktai 3 dark gun (Phantom + dark lens)", rom.relocated_regions)
    assert bps.apply(clean, patch) == patched
    (arguments.out / (OUTPUT_NAME + ".gba")).write_bytes(patched)
    (arguments.out / (OUTPUT_NAME + ".bps")).write_bytes(patch)
    print("wrote %s (%d bytes) and the patched ROM to %s" % (OUTPUT_NAME + ".bps", len(patch), arguments.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
