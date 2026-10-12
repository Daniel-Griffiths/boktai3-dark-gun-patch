import struct
from pathlib import Path

ROM_BASE = 0x08000000
FILE_SYSTEM_SIGNATURE = bytes.fromhex("30512592")
DIRECTORY_COUNT = 11

SCRIPT_DIRECTORY = 0x49BC
MAIN_SPRITE_DIRECTORY = 0x5F29
OBJECT_PALETTE_DIRECTORY = 0x4679

END_OF_SCRIPT_LIST = 0xFFFFFFFF


class Boktai3Rom:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        game_code = self.data[0xAC:0xB0].decode("ascii", "replace")
        assert game_code == "U33J", "not the Japanese Boktai 3 ROM (game code %s, expected U33J)" % game_code

        table = self.data.find(FILE_SYSTEM_SIGNATURE)
        assert table >= 0, "file system not found"
        self.directories = {}
        for index in range(DIRECTORY_COUNT):
            directory_id, _, address = struct.unpack_from("<HHI", self.data, table + 8 * index)
            self.directories[directory_id] = address
        self.file_ranges = {}

        self.script_directory = self.directories[SCRIPT_DIRECTORY]
        cursor = self.script_directory + 4
        while self.read_u32(cursor) != END_OF_SCRIPT_LIST:
            cursor += 4
        cursor += 4
        bytecode_header = cursor + self.read_u32(cursor)
        self.bytecode_address = bytecode_header + 4
        bytecode_size = self.read_u32(bytecode_header)
        self.bytecode = self.data[self.bytecode_address - ROM_BASE:self.bytecode_address - ROM_BASE + bytecode_size]

    def read_u16(self, address):
        return struct.unpack_from("<H", self.data, address - ROM_BASE)[0]

    def read_u32(self, address):
        return struct.unpack_from("<I", self.data, address - ROM_BASE)[0]

    def script_entry_address(self, script_id):
        return self.script_directory + 4 + 4 * (script_id - 1)

    def directory_entry_address(self, directory_id, file_id):
        directory = self.directories[directory_id]
        count, ids_offset, offsets_offset = (self.read_u32(directory + 4 * field) for field in range(3))
        for index in range(count):
            if self.read_u16(directory + ids_offset + 2 * index) == file_id:
                return directory, directory + offsets_offset + 4 * index
        raise KeyError("file %04X not in directory %04X" % (file_id, directory_id))

    def files(self, directory_id):
        if directory_id not in self.file_ranges:
            directory = self.directories[directory_id]
            count, ids_offset, offsets_offset = (self.read_u32(directory + 4 * field) for field in range(3))
            ids = [self.read_u16(directory + ids_offset + 2 * index) for index in range(count)]
            starts = [directory + self.read_u32(directory + offsets_offset + 4 * index) for index in range(count)]
            later_directories = sorted(address for address in self.directories.values() if address > directory)
            last_end = later_directories[0] if later_directories else ROM_BASE + len(self.data)
            ends = starts[1:] + [last_end]
            self.file_ranges[directory_id] = {file_id: (start, end) for file_id, start, end in zip(ids, starts, ends)}
        return self.file_ranges[directory_id]


class Instruction:
    def __init__(self, kind, value=None, start=0):
        self.kind = kind
        self.value = value
        self.children = []
        self.start = start
        self.end = start


def read_length(data, position):
    size_code = data[position] & 0xF
    if size_code == 0xD:
        return data[position + 1], position + 2
    if size_code == 0xE:
        return data[position + 1] | data[position + 2] << 8, position + 3
    if size_code == 0xF:
        return data[position + 1] | data[position + 2] << 8 | data[position + 3] << 16, position + 4
    return size_code, position + 1


def signed_16(value):
    return value - 0x10000 if value & 0x8000 else value


def read_children(data, position, end, instruction, skip_end_markers=True):
    while position < end:
        child, position = read_instruction(data, position)
        if not (skip_end_markers and child.kind == "end"):
            instruction.children.append(child)
    return end


def read_instruction(data, position):
    opcode = data[position]
    group = opcode & 0xF0
    instruction = Instruction("?", start=position)

    if group >= 0xC0:
        instruction.kind, instruction.value = "const", (opcode & 0x3F) - 1
        position += 1

    elif group == 0x00:
        position += 1
        if opcode == 0:
            instruction.kind = "end"
        elif opcode in (1, 8):
            instruction.kind, instruction.value = "const", signed_16(data[position] | data[position + 1] << 8)
            position += 2
        elif opcode in (2, 3, 4):
            instruction.kind, instruction.value = "const", data[position]
            position += 1
        elif opcode == 6:
            instruction.kind, instruction.value = "const", data[position] | data[position + 1] << 8
            position += 2
        elif opcode in (9, 10, 13):
            instruction.kind, instruction.value = "const", struct.unpack_from("<i", data, position)[0]
            position += 4
        elif opcode == 7:
            length = data[position]
            instruction.kind, instruction.value = "string", bytes(data[position + 1:position + 1 + length])
            position += 1 + length
        elif opcode == 14:
            instruction.kind, instruction.value = "strref", data[position] | data[position + 1] << 8
            position += 2
        else:
            raise ValueError("bad constant opcode %02X at %X" % (opcode, position))

    elif group in (0x10, 0x20):
        instruction.kind = "memory" if group == 0x10 else "indexed_memory"
        position += 4
        if group == 0x20:
            index, position = read_instruction(data, position)
            value, position = read_instruction(data, position)
            instruction.children = [index, value]

    elif group == 0x30:
        length, position = read_length(data, position)
        instruction.kind = "expression"
        position = read_children(data, position, position + length, instruction, skip_end_markers=False)

    elif group == 0x40:
        instruction.kind = "parameter"
        if opcode & 0xF == 0xF:
            instruction.value = 0xF + data[position + 1]
            position += 2
        else:
            instruction.value = opcode & 0xF
            position += 1

    elif group == 0x50:
        length, position = read_length(data, position)
        end = position + length
        instruction.kind, instruction.value = "label", chr(data[position])
        position = read_children(data, position + 1, end, instruction, skip_end_markers=False)

    elif group == 0x60:
        length, position = read_length(data, position)
        end = position + length
        instruction.kind, instruction.value = "control", data[position] | data[position + 1] << 8
        position += 2
        if data[position] & 0x80:
            position += 1
        position = read_children(data, position + 1, end, instruction)

    elif group == 0x70:
        length, position = read_length(data, position)
        end = position + length
        instruction.kind, instruction.value = "call", data[position] | data[position + 1] << 8
        position = read_children(data, position + 2, end, instruction)

    elif group == 0x80:
        length, position = read_length(data, position)
        instruction.kind = "block"
        position = read_children(data, position, position + length, instruction)

    elif group == 0x90:
        instruction.kind, instruction.value = "variable", opcode & 0xF
        position += 1

    elif group in (0xA0, 0xB0):
        instruction.kind, instruction.value = "operator", opcode & 0x1F
        position += 1

    instruction.end = position
    return instruction, position
