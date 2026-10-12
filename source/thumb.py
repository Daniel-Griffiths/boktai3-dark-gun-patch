import struct

R0, R1, R2, R3, R4, R5, R6, R7, R8 = range(9)
IP = 12

NOP = 0x46C0


class ThumbAssembler:
    def __init__(self, base_address: int):
        self.base_address = base_address
        self.halfwords: list = []
        self.labels: dict = {}
        self.fixups: list = []
        self.literals: list = []

    def current_address(self) -> int:
        return self.base_address + 2 * len(self.halfwords)

    def label(self, name: str) -> None:
        self.labels[name] = self.current_address()

    def emit(self, halfword: int) -> None:
        self.halfwords.append(halfword & 0xFFFF)

    def placeholder(self) -> int:
        self.halfwords.append(None)
        return len(self.halfwords) - 1

    def movs(self, destination: int, value: int) -> None:
        assert 0 <= value <= 0xFF and destination < 8
        self.emit(0x2000 | destination << 8 | value)

    def cmp(self, register: int, value: int) -> None:
        assert 0 <= value <= 0xFF and register < 8
        self.emit(0x2800 | register << 8 | value)

    def cmp_registers(self, first: int, second: int) -> None:
        self.emit(0x4280 | second << 3 | first)

    def add_value(self, destination: int, value: int) -> None:
        self.emit(0x3000 | destination << 8 | value)

    def add_registers(self, destination: int, first: int, second: int) -> None:
        self.emit(0x1800 | second << 6 | first << 3 | destination)

    def copy_register(self, destination: int, source: int) -> None:
        self.emit(0x1C00 | source << 3 | destination)

    def shift_left(self, destination: int, source: int, amount: int) -> None:
        self.emit(amount << 6 | source << 3 | destination)

    def move_high(self, destination: int, source: int) -> None:
        self.emit(0x4600 | (destination >> 3) << 7 | (source >> 3) << 6 | (source & 7) << 3 | (destination & 7))

    def add_high(self, destination: int, source: int) -> None:
        self.emit(0x4400 | (destination >> 3) << 7 | (source >> 3) << 6 | (source & 7) << 3 | (destination & 7))

    def load_byte(self, destination: int, address: int, offset: int = 0) -> None:
        self.emit(0x7800 | offset << 6 | address << 3 | destination)

    def store_byte(self, value: int, address: int, offset: int = 0) -> None:
        self.emit(0x7000 | offset << 6 | address << 3 | value)

    def load_word(self, destination: int, address: int) -> None:
        self.emit(0x6800 | address << 3 | destination)

    def store_word(self, value: int, address: int, offset: int = 0) -> None:
        assert offset % 4 == 0 and 0 <= offset < 128
        self.emit(0x6000 | (offset >> 2) << 6 | address << 3 | value)

    def store_halfword(self, value: int, address: int, offset: int = 0) -> None:
        assert offset % 2 == 0 and 0 <= offset < 64
        self.emit(0x8000 | (offset >> 1) << 6 | address << 3 | value)

    def load_constant(self, destination: int, value: int) -> None:
        index = self.placeholder()
        self.literals.append((index, value))
        self.fixups.append(("literal", index, destination))

    def push(self, registers: list) -> None:
        self.emit(0xB400 | sum(1 << register for register in registers))

    def pop(self, registers: list) -> None:
        self.emit(0xBC00 | sum(1 << register for register in registers))

    def branch_exchange(self, register: int) -> None:
        self.emit(0x4700 | register << 3)

    def branch(self, label: str) -> None:
        self.fixups.append(("branch", self.placeholder(), label))

    def branch_if_equal(self, label: str) -> None:
        self.fixups.append(("conditional", self.placeholder(), (0x0, label)))

    def branch_if_not_equal(self, label: str) -> None:
        self.fixups.append(("conditional", self.placeholder(), (0x1, label)))

    def branch_with_link(self, label: str) -> None:
        index = self.placeholder()
        self.placeholder()
        self.fixups.append(("call", index, label))

    def jump_to(self, address: int, register: int = R0) -> None:
        self.load_constant(register, address | 1)
        self.branch_exchange(register)

    def call_far(self, address: int) -> None:
        self.push([R4])
        self.load_constant(R4, address | 1)
        self.move_high(IP, R4)
        self.pop([R4])
        self.branch_with_link("call_ip")

    def add_call_ip_veneer(self) -> None:
        self.label("call_ip")
        self.branch_exchange(IP)

    def assemble(self) -> bytes:
        if len(self.halfwords) % 2:
            self.emit(NOP)
        pool_address = self.current_address()
        pool: list = []
        literal_address: dict = {}
        for index, value in self.literals:
            if value not in pool:
                pool.append(value)
            literal_address[index] = pool_address + 4 * pool.index(value)
        code = list(self.halfwords)
        for kind, index, argument in self.fixups:
            here = self.base_address + 2 * index
            if kind == "literal":
                distance = literal_address[index] - ((here + 4) & ~3)
                assert 0 <= distance <= 1020 and distance % 4 == 0, distance
                code[index] = 0x4800 | argument << 8 | distance >> 2
            elif kind == "branch":
                distance = self.labels[argument] - (here + 4)
                assert -2048 <= distance < 2048
                code[index] = 0xE000 | (distance >> 1) & 0x7FF
            elif kind == "conditional":
                condition, label = argument
                distance = self.labels[label] - (here + 4)
                assert -256 <= distance < 256
                code[index] = 0xD000 | condition << 8 | (distance >> 1) & 0xFF
            elif kind == "call":
                distance = self.labels[argument] - (here + 4)
                assert -(1 << 22) <= distance < (1 << 22)
                code[index] = 0xF000 | (distance >> 12) & 0x7FF
                code[index + 1] = 0xF800 | (distance >> 1) & 0x7FF
        assert None not in code
        return b"".join(struct.pack("<H", halfword) for halfword in code) + b"".join(struct.pack("<I", value) for value in pool)
