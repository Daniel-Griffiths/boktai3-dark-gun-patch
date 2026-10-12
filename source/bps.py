import zlib

SOURCE_READ, TARGET_READ, SOURCE_COPY, TARGET_COPY = 0, 1, 2, 3
MINIMUM_COPY_LENGTH = 16
UNCHANGED_RUN_TO_STOP = 8


def encode_number(value: int) -> bytes:
    encoded = bytearray()
    while True:
        low_bits = value & 0x7F
        value >>= 7
        if value == 0:
            encoded.append(0x80 | low_bits)
            return bytes(encoded)
        encoded.append(low_bits)
        value -= 1


def decode_number(data: bytes, position: int) -> tuple:
    value, shift = 0, 1
    while True:
        byte = data[position]
        position += 1
        value += (byte & 0x7F) * shift
        if byte & 0x80:
            return value, position
        shift <<= 7
        value += shift


def command(action: int, length: int) -> bytes:
    return encode_number(((length - 1) << 2) | action)


def signed_offset(offset: int) -> bytes:
    return encode_number((abs(offset) << 1) | (1 if offset < 0 else 0))


def checksum(data: bytes) -> bytes:
    return zlib.crc32(data).to_bytes(4, "little")


def encode(source: bytes, target: bytes, description: str = "", relocated_regions: list = ()) -> bytes:
    metadata = description.encode("utf-8")
    patch = bytearray(b"BPS1" + encode_number(len(source)) + encode_number(len(target)) + encode_number(len(metadata)) + metadata)

    source_positions: dict = {}
    for region_start, region_end in relocated_regions:
        for position in range(region_start, region_end - MINIMUM_COPY_LENGTH + 1):
            source_positions.setdefault(source[position:position + MINIMUM_COPY_LENGTH], position)

    def find_copy(target_position: int):
        source_position = source_positions.get(target[target_position:target_position + MINIMUM_COPY_LENGTH])
        if source_position is None:
            return None
        length = MINIMUM_COPY_LENGTH
        while (target_position + length < len(target) and source_position + length < len(source)
               and source[source_position + length] == target[target_position + length]):
            length += 1
        return source_position, length

    def unchanged_at(position: int, length: int = 1) -> bool:
        chunk = target[position:position + length]
        return position < len(source) and len(chunk) == length and source[position:position + length] == chunk

    source_cursor = 0
    position = 0
    while position < len(target):
        if unchanged_at(position):
            end = position
            while end < len(target) and unchanged_at(end):
                end += 1
            patch += command(SOURCE_READ, end - position)
            position = end
            continue

        copy = find_copy(position)
        if copy is not None:
            source_position, length = copy
            patch += command(SOURCE_COPY, length) + signed_offset(source_position - source_cursor)
            source_cursor = source_position + length
            position += length
            continue

        end = position
        while end < len(target):
            if unchanged_at(end, UNCHANGED_RUN_TO_STOP):
                break
            if end > position and find_copy(end) is not None:
                break
            end += 1
        patch += command(TARGET_READ, end - position) + target[position:end]
        position = end

    patch += checksum(source) + checksum(target)
    patch += checksum(bytes(patch))
    return bytes(patch)


def apply(source: bytes, patch: bytes) -> bytes:
    assert patch[:4] == b"BPS1"
    position = 4
    source_size, position = decode_number(patch, position)
    target_size, position = decode_number(patch, position)
    metadata_size, position = decode_number(patch, position)
    position += metadata_size
    assert source_size == len(source) and checksum(source) == patch[-12:-8], "wrong source ROM"

    target = bytearray()
    source_cursor = target_cursor = 0
    commands_end = len(patch) - 12
    while position < commands_end:
        value, position = decode_number(patch, position)
        action, length = value & 3, (value >> 2) + 1
        if action == SOURCE_READ:
            target += source[len(target):len(target) + length]
        elif action == TARGET_READ:
            target += patch[position:position + length]
            position += length
        else:
            encoded_offset, position = decode_number(patch, position)
            offset = (-1 if encoded_offset & 1 else 1) * (encoded_offset >> 1)
            if action == SOURCE_COPY:
                source_cursor += offset
                target += source[source_cursor:source_cursor + length]
                source_cursor += length
            else:
                target_cursor += offset
                for _ in range(length):
                    target.append(target[target_cursor])
                    target_cursor += 1

    assert len(target) == target_size and checksum(bytes(target)) == patch[-8:-4], "bad result"
    return bytes(target)
