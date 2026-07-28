"""A QR generator with ZERO imports. Run: python qrterminal_zero_imports.py
Version 1 QR only: short text (L=17 bytes, M=14 bytes, H=7 bytes).
Uses compact Unicode half blocks, like qrterminal's small terminal output."""

SIZE = 21
SPECS = {"L": (19, 7, 1), "M": (16, 10, 0), "H": (9, 17, 2)}


def multiply(x, y):
    answer = 0
    while y:
        if y & 1:
            answer ^= x
        x = (x << 1) ^ (0x11D if x & 128 else 0)
        y >>= 1
    return answer


def error_correction(data, amount):
    generator = [1]
    root = 1
    for unused in range(amount):
        new = [0] * (len(generator) + 1)
        for index, value in enumerate(generator):
            new[index] ^= value
            new[index + 1] ^= multiply(value, root)
        generator = new
        root = multiply(root, 2)
    result = [0] * amount
    for value in data:
        lead = value ^ result.pop(0)
        result.append(0)
        for index in range(amount):
            result[index] ^= multiply(generator[index + 1], lead)
    return result


def create_codewords(text, level):
    capacity, ec_size, unused = SPECS[level]
    values = list(text.encode("utf-8"))
    if len(values) > capacity - 2:
        raise ValueError("Text is too long: use at most " + str(capacity - 2) + " UTF-8 bytes for level " + level)
    bits = "0100" + format(len(values), "08b")
    for value in values:
        bits += format(value, "08b")
    bits = (bits + "0000")[:capacity * 8]
    bits += "0" * ((8 - len(bits) % 8) % 8)
    data = [int(bits[index:index + 8], 2) for index in range(0, len(bits), 8)]
    padding = [236, 17]
    while len(data) < capacity:
        data.append(padding[len(data) % 2])
    return data + error_correction(data, ec_size)


def format_information(value):
    bits = value << 10
    while bits.bit_length() >= 11:
        bits ^= 0x537 << (bits.bit_length() - 11)
    return ((value << 10) | bits) ^ 0x5412


def create_qr(text, level):
    grid = [[False] * SIZE for unused in range(SIZE)]
    fixed = [[False] * SIZE for unused in range(SIZE)]

    def set_fixed(x, y, value):
        if 0 <= x < SIZE and 0 <= y < SIZE:
            grid[y][x] = value
            fixed[y][x] = True

    def finder(center_x, center_y):
        for delta_y in range(-4, 5):
            for delta_x in range(-4, 5):
                distance = max(abs(delta_x), abs(delta_y))
                set_fixed(center_x + delta_x, center_y + delta_y, distance not in (2, 4))

    finder(3, 3); finder(17, 3); finder(3, 17)
    for index in range(8, 13):
        set_fixed(6, index, index % 2 == 0)
        set_fixed(index, 6, index % 2 == 0)
    set_fixed(8, 13, True)

    information = format_information(SPECS[level][2] << 3)  # QR mask pattern 0
    def bit(index): return bool((information >> index) & 1)
    for index in range(6): set_fixed(8, index, bit(index))
    set_fixed(8, 7, bit(6)); set_fixed(8, 8, bit(7)); set_fixed(7, 8, bit(8))
    for index in range(9, 15): set_fixed(14 - index, 8, bit(index))
    for index in range(8): set_fixed(20 - index, 8, bit(index))
    for index in range(8, 15): set_fixed(8, index - 6, bit(index))

    stream = "".join(format(value, "08b") for value in create_codewords(text, level))
    position, x, upwards = 0, 20, True
    while x > 0:
        if x == 6:
            x -= 1
        rows = range(20, -1, -1) if upwards else range(SIZE)
        for y in rows:
            for column in (x, x - 1):
                if not fixed[y][column]:
                    dark = position < len(stream) and stream[position] == "1"
                    position += 1
                    grid[y][column] = dark ^ ((column + y) % 2 == 0)
        x -= 2
        upwards = not upwards
    return grid


def show(grid, border):
    empty = [False] * (SIZE + border * 2)
    rows = [empty] * border + [[False] * border + row + [False] * border for row in grid] + [empty] * border
    if len(rows) % 2:
        rows.append(empty)
    # One character holds the top and bottom modules, making the QR nearly square.
    for top, bottom in zip(rows[::2], rows[1::2]):
        line = ""
        for upper, lower in zip(top, bottom):
            if upper and lower:
                line += "█"
            elif upper:
                line += "▀"
            elif lower:
                line += "▄"
            else:
                line += " "
        print(line)


def main():
    text = input("Text or URL: ")
    level = input("Error correction [L/M/H] (L): ").strip().upper() or "L"
    if level not in SPECS:
        print("Please use L, M, or H.")
        return
    try:
        show(create_qr(text, level), 2)
    except ValueError as error:
        print(error)


main()
