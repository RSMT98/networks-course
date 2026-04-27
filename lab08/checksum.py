def _fold_sum(value: int) -> int:
    while value >> 16:
        value = (value & 0xFFFF) + (value >> 16)
    return value


def _sum_words(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"

    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
        total = _fold_sum(total)
    return total


def internet_checksum(data: bytes) -> int:
    total = _sum_words(data)
    return (~total) & 0xFFFF


def is_checksum_valid(data: bytes, checksum: int) -> bool:
    if not 0 <= checksum <= 0xFFFF:
        return False

    total = _sum_words(data) + checksum
    return _fold_sum(total) == 0xFFFF


def _run_test(checksum_data: bytes, checked_data: bytes, should_be_valid: bool) -> None:
    checksum = internet_checksum(checksum_data)
    is_valid = is_checksum_valid(checked_data, checksum)
    assert is_valid == should_be_valid


def main() -> None:
    plain_data = b"Hello, world!!"
    _run_test(plain_data, plain_data, True)

    broken_data = bytearray(plain_data)
    broken_data[1] ^= 0b00010000
    _run_test(plain_data, bytes(broken_data), False)

    odd_length_data = b"abcde"
    _run_test(odd_length_data, odd_length_data, True)

    broken_odd_length_data = bytearray(odd_length_data)
    broken_odd_length_data[-1] ^= 0b00000001
    _run_test(odd_length_data, bytes(broken_odd_length_data), False)

    print("All checksum tests passed!")


if __name__ == "__main__":
    main()
