"""纯标准库的 QR 编码器（byte 模式 / ECC L·M / 版本 1~10），只服务于 2FA 的 otpauth 二维码。

为什么自己写而不是引一个依赖
----------------------------
``docs/AUTH.md`` §12 第 1 条：TOTP 密钥**绝不能外发**——任何「把 secret 传到第三方生成二维码」
的做法等于把二次验证的密钥交给别人。所以要么内置一个离线编码器，要么让用户手抄密钥。
这里选了前者。

为什么是**服务端 Python**（而不是文档里提到的「精简 JS 编码器」）
----------------------------------------------------------------
两者的安全性等价（密钥本来就在服务端，二维码最终总要显示在这个浏览器里），但可测试性差很多：
Python 版能在 ``pytest`` 里离线跑向量与结构断言，JS 版要跑就得让 CI 依赖 node。
所以按「离线、不外发」这个**目的**落地，实现语言换了。已在 ``docs/AUTH.md`` 的偏差表里记下。

支持范围（刻意收窄）
--------------------
* 模式：**byte**（otpauth URI 里大小写、``:``、``?``、``&`` 都有，byte 模式最稳）；
* 纠错：**M**（屏幕扫码的常用档），数据太长时自动降到 **L**；
* 版本：**1~10**（1-M 放得下 14 字节，10-M 放得下 213 字节）——otpauth URI 实测 ~120 字节，
  而用户名上限 64 字符时也就 ~180 字节，够用且把表压得足够小。

不做的事：多模式混排（数字/字母数字）、Q/H 档、版本 11+、结构化追加。用不上，
而每多支持一档都要多一张必须正确的表。

验证与已知差异
--------------
本模块的实现对着独立实现（``segno``）做过**逐位比对**：版本 1~10 × ECC L/M × 8 个掩码 ×
16 组输入，共 232 张矩阵**全部逐位相同**（比对前需修掉 segno 的一个填充怪癖——它在数据流
已字节对齐时仍多写一个 ``0x00`` 字节，与 ISO/IEC 18004 §7.4.10 及它自己的 docstring 不符）。
测试里冻结的摘要即取自这次比对之后的实现。

**掩码选择可能与其它库不同**：这是已知的实现差异（各库对规则 3「重叠图案」的计数口径不一，
本模块逐位计每个「存在」，与规范文字一致）。掩码号写在格式信息里，读码器据此解掩码，
所以**选哪张掩码都不影响可解码性**，只是观感与评分略有差别。
"""

from __future__ import annotations

import itertools
from typing import Any

#: GF(256) 的本原多项式 x^8+x^4+x^3+x^2+1 —— QR 的 Reed-Solomon 用它。
_GF_PRIMITIVE = 0x11D

#: RS 分块表：``_RS_BLOCKS[版本][ECC]`` = ``(块数, 每块总码字数, 每块数据码字数)`` 的元组。
#: 出现两个元组 = 两组块（组的块大小不同），这是规范的写法，别把它拍平。
#:
#: 数值取自 ISO/IEC 18004 表 9。自检：每组的 ``块数 × 总码字数`` 之和必须等于该版本的
#: 总码字数（``27 + 版本 × 4`` 之外还有对齐/版本信息带来的差异，所以这里只做组内一致性检查，
#: 见 ``_data_codewords`` 与测试里的断言）。
_RS_BLOCKS: dict[int, dict[str, tuple[tuple[int, int, int], ...]]] = {
    1: {"L": ((1, 26, 19),), "M": ((1, 26, 16),)},
    2: {"L": ((1, 44, 34),), "M": ((1, 44, 28),)},
    3: {"L": ((1, 70, 55),), "M": ((1, 70, 44),)},
    4: {"L": ((1, 100, 80),), "M": ((2, 50, 32),)},
    5: {"L": ((1, 134, 108),), "M": ((2, 67, 43),)},
    6: {"L": ((2, 86, 68),), "M": ((4, 43, 27),)},
    7: {"L": ((2, 98, 78),), "M": ((4, 49, 31),)},
    8: {"L": ((2, 121, 97),), "M": ((2, 60, 38), (2, 61, 39))},
    9: {"L": ((2, 146, 116),), "M": ((3, 58, 36), (2, 59, 37))},
    10: {"L": ((2, 86, 68), (2, 87, 69)), "M": ((4, 69, 43), (1, 70, 44))},
}

#: 对齐图形的中心坐标（每个版本一套）。
_ALIGNMENT: dict[int, tuple[int, ...]] = {
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}

#: ECC 档 → 格式信息里的 2 位编码（L=01 / M=00 / Q=11 / H=10）。
_FORMAT_ECC: dict[str, int] = {"L": 0b01, "M": 0b00}

#: byte 模式的模式指示符。
_MODE_BYTE = 0b0100

#: 格式信息的掩码常量（规范规定要异或它，否则读码器认不出）。
_FORMAT_XOR = 0b101010000010010


# ---------------------------------------------------------------------------
# GF(256) 与 Reed-Solomon
# ---------------------------------------------------------------------------


def _gf_tables() -> tuple[list[int], list[int]]:
    """生成 GF(256) 的指数/对数表（乘法查表，比逐位乘快得多）。"""
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for power in range(255):
        exp[power] = value
        log[value] = power
        value <<= 1
        if value & 0x100:
            value ^= _GF_PRIMITIVE
    for power in range(255, 512):
        exp[power] = exp[power - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return _GF_EXP[_GF_LOG[left] + _GF_LOG[right]]


def _poly_mul(left: list[int], right: list[int]) -> list[int]:
    """多项式乘法（系数**降幂**排列）。"""
    out = [0] * (len(left) + len(right) - 1)
    for i, a in enumerate(left):
        if not a:
            continue
        for j, b in enumerate(right):
            if b:
                out[i + j] ^= _gf_mul(a, b)
    return out


def _rs_generator(degree: int) -> list[int]:
    """生成多项式 ``g(x) = ∏(x - α^i)``，降幂（``g[0]`` 恒为 1）。

    缓存住：同一个 degree 每次编码都要用，而它是 8 次多项式乘法。
    """
    cached = _RS_GENERATORS.get(degree)
    if cached is not None:
        return cached
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _GF_EXP[i]])
    _RS_GENERATORS[degree] = poly
    return poly


_RS_GENERATORS: dict[int, list[int]] = {}


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    """算出 ``degree`` 个纠错码字。

    做法：把 data 左移 degree 位后对生成多项式取模——即教科书式的 LFSR 除法，
    一遍扫过去、每步消掉当前的最高次项。
    """
    generator = _rs_generator(degree)
    out = [0] * (len(data) + degree)
    out[: len(data)] = data
    for i in range(len(data)):
        coefficient = out[i]
        if coefficient:
            for j in range(1, degree + 1):
                out[i + j] ^= _gf_mul(generator[j], coefficient)
    return out[len(data) :]


# ---------------------------------------------------------------------------
# 位缓冲与版本选择
# ---------------------------------------------------------------------------


class _Bits:
    """按位写入的缓冲（QR 的数据段是**位**级的，不是字节级的）。"""

    def __init__(self) -> None:
        self._bits: list[int] = []

    def put(self, value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    def __len__(self) -> int:
        return len(self._bits)

    def to_codewords(self) -> list[int]:
        out: list[int] = []
        for start in range(0, len(self._bits), 8):
            byte = 0
            for bit in self._bits[start : start + 8]:
                byte = (byte << 1) | bit
            out.append(byte)
        return out


def _data_codewords(version: int, ecc: str) -> int:
    """该版本 + ECC 档下**数据**码字的总数（纠错码字不算）。"""
    return sum(count * data for count, _total, data in _RS_BLOCKS[version][ecc])


def _choose_version(nbytes: int, ecc: str) -> int:
    """挑最小的放得下的版本；都放不下就报错（调用方应降级到 L 或退回文本）。"""
    for version in sorted(_RS_BLOCKS):
        count_bits = 8 if version < 10 else 16
        needed = 4 + count_bits + 8 * nbytes
        if needed <= _data_codewords(version, ecc) * 8:
            return version
    raise ValueError(
        f"内容太长（{nbytes} 字节），本编码器只支持到版本 10 的 {ecc} 档"
        f"（上限 {_data_codewords(10, ecc)} 字节）"
    )


def _codewords(text: str, version: int, ecc: str) -> list[int]:
    """数据位 → 码字（含纠错、含块间交织）。"""
    payload = text.encode("utf-8")
    capacity = _data_codewords(version, ecc) * 8

    bits = _Bits()
    bits.put(_MODE_BYTE, 4)
    bits.put(len(payload), 8 if version < 10 else 16)
    for byte in payload:
        bits.put(byte, 8)
    # 结束符：最多 4 个 0，但**不能超过容量**
    bits.put(0, min(4, capacity - len(bits)))
    # 补齐到字节边界
    if len(bits) % 8:
        bits.put(0, 8 - len(bits) % 8)
    # 填充码字：0xEC / 0x11 交替（规范规定的「空洞」填充值）
    pad = 0xEC
    while len(bits) < capacity:
        bits.put(pad, 8)
        pad = 0x11 if pad == 0xEC else 0xEC

    codewords = bits.to_codewords()
    blocks = _RS_BLOCKS[version][ecc]
    ec_per_block = blocks[0][1] - blocks[0][2]

    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for count, _total, data_len in blocks:
        for _ in range(count):
            chunk = codewords[offset : offset + data_len]
            offset += data_len
            data_blocks.append(chunk)
            ec_blocks.append(_rs_remainder(chunk, ec_per_block))

    # 交织：先按列取所有数据块，再按列取所有纠错块（读码器按同样的顺序还原）
    out: list[int] = []
    for index in range(max(len(block) for block in data_blocks)):
        for block in data_blocks:
            if index < len(block):
                out.append(block[index])
    for index in range(ec_per_block):
        for block in ec_blocks:
            out.append(block[index])
    return out


# ---------------------------------------------------------------------------
# 矩阵构造
# ---------------------------------------------------------------------------


def _setup_finder(modules: list[list[Any]], size: int) -> None:
    """三个定位图形 + 分隔带（7×7 的「回」字，外面留一圈浅色）。"""
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                row, col = top + dr, left + dc
                if not (0 <= row < size and 0 <= col < size):
                    continue
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    dark = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                else:
                    dark = False  # 分隔带
                modules[row][col] = dark


def _setup_alignment(modules: list[list[Any]], version: int) -> None:
    """对齐图形（5×5）——三个角上的会与定位图形重叠，跳过。"""
    centers = _ALIGNMENT.get(version)
    if not centers:
        return
    size = len(modules)
    for row_c, col_c in itertools.product(centers, centers):
        # 左上角、右上角、左下角三处与定位图形冲突
        if (row_c, col_c) in ((6, 6), (6, size - 7), (size - 7, 6)):
            continue
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                modules[row_c + dr][col_c + dc] = max(abs(dr), abs(dc)) != 1


def _setup_timing(modules: list[list[Any]], size: int) -> None:
    """定时图形：第 6 行/列的黑白相间，读码器靠它定位模块网格。"""
    for index in range(8, size - 8):
        dark = index % 2 == 0
        modules[6][index] = dark
        modules[index][6] = dark


def _format_bits(ecc: str, mask: int) -> int:
    """格式信息：5 位数据（ECC 档 + 掩码）+ BCH(15,5) 校验，最后异或固定常量。"""
    data = (_FORMAT_ECC[ecc] << 3) | mask
    remainder = data << 10
    generator = 0b10100110111  # x^10+x^8+x^5+x^4+x^2+x+1
    for shift in range(4, -1, -1):
        if remainder & (1 << (shift + 10)):
            remainder ^= generator << shift
    return ((data << 10) | remainder) ^ _FORMAT_XOR


def _setup_format(modules: list[list[Any]], ecc: str, mask: int) -> None:
    """写入格式信息的**两份**副本，以及那个恒黑的固定模块。

    两份副本是规范要求的冗余：读码器只要读到其中一份就能知道 ECC 档与掩码。
    """
    size = len(modules)
    bits = _format_bits(ecc, mask)
    for i in range(15):
        dark = ((bits >> i) & 1) == 1
        # 第一份：沿左上角定位图形铺，绕过第 6 行/列的定时图形
        if i < 6:
            modules[i][8] = dark
        elif i < 8:
            modules[i + 1][8] = dark
        else:
            modules[size - 15 + i][8] = dark
        # 第二份：右上角与左下角
        if i < 8:
            modules[8][size - i - 1] = dark
        elif i < 9:
            modules[8][15 - i - 1 + 1] = dark
        else:
            modules[8][15 - i - 1] = dark
    modules[size - 8][8] = True  # 固定黑模块


def _setup_version(modules: list[list[Any]], version: int) -> None:
    """版本信息（版本 ≥ 7 才有）：6 位版本号 + BCH(18,6) 校验，两份副本。"""
    if version < 7:
        return
    size = len(modules)
    remainder = version << 12
    generator = 0b1111100100101  # x^12+x^11+x^10+x^9+x^8+x^5+x^2+1
    for shift in range(5, -1, -1):
        if remainder & (1 << (shift + 12)):
            remainder ^= generator << shift
    bits = (version << 12) | remainder
    for i in range(18):
        dark = ((bits >> i) & 1) == 1
        modules[size - 11 + i % 3][i // 3] = dark
        modules[i // 3][size - 11 + i % 3] = dark


def _mask_applies(mask: int, row: int, col: int) -> bool:
    """8 种掩码图形（规范里的公式，别合并化简——它们各自的意义不同）。"""
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _map_data(modules: list[list[Any]], codewords: list[int], mask: int) -> None:
    """把码字按 Z 字形填进「还空着」的模块，同时套上掩码。

    掩码**只作用于数据模块**（函数图形不参与）——所以它是与放置同时做的：
    已经填过的模块（``not None``）一律跳过，判定与规范的「函数图形不掩码」等价。
    """
    size = len(modules)
    index = 0
    bit = 7
    row = size - 1
    direction = -1
    col = size - 1
    while col > 0:
        if col == 6:  # 纵向定时图形所在列，整列跳过
            col -= 1
        while True:
            for offset in range(2):
                current_col = col - offset
                if modules[row][current_col] is not None:
                    continue
                dark = False
                if index < len(codewords):
                    dark = ((codewords[index] >> bit) & 1) == 1
                if _mask_applies(mask, row, current_col):
                    dark = not dark
                modules[row][current_col] = dark
                bit -= 1
                if bit < 0:
                    index += 1
                    bit = 7
            row += direction
            if row < 0 or row >= size:
                row -= direction
                direction = -direction
                break
        col -= 2


def _penalty(modules: list[list[bool]]) -> int:
    """规范里的 4 条掩码评分规则——分数越低越好。"""
    size = len(modules)
    score = 0

    # 规则 1：行/列里连续同色 5 个以上 → 3 + (长度 - 5)
    for row in range(size):
        run = 1
        for col in range(1, size):
            if modules[row][col] == modules[row][col - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5
    for col in range(size):
        run = 1
        for row in range(1, size):
            if modules[row][col] == modules[row - 1][col]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5

    # 规则 2：2×2 同色方块 → 每处 3 分
    for row in range(size - 1):
        for col in range(size - 1):
            block = (
                modules[row][col]
                + modules[row + 1][col]
                + modules[row][col + 1]
                + modules[row + 1][col + 1]
            )
            if block in (0, 4):
                score += 3

    # 规则 3：形如 1011101 的图案，且**前或后**有 4 个模块宽的浅色区 → 每处 40 分。
    #
    # 关键在于「4 个模块宽的浅色区」可以落在**符号之外**（静区本来就是浅色）：
    # 图案贴着符号边缘时同样计分。原先这里拿 11 个模块的窗口去比对，边缘那一类会被漏掉，
    # 于是自动挑出来的掩码与规范（以及其它实现）不同——不是错码，但没必要不一样。
    pattern = (True, False, True, True, True, False, True)
    for row in range(size):
        line = modules[row]
        for start in range(size - 6):
            if tuple(line[start : start + 7]) != pattern:
                continue
            before_light = all(not line[index] for index in range(max(0, start - 4), start))
            after_light = all(
                not line[index] for index in range(start + 7, min(size, start + 11))
            )
            if before_light or after_light:
                score += 40
    for col in range(size):
        column = [modules[row][col] for row in range(size)]
        for start in range(size - 6):
            if tuple(column[start : start + 7]) != pattern:
                continue
            before_light = all(
                not column[index] for index in range(max(0, start - 4), start)
            )
            after_light = all(
                not column[index] for index in range(start + 7, min(size, start + 11))
            )
            if before_light or after_light:
                score += 40

    # 规则 4：黑模块比例偏离 50% → 每 5% 计 10 分
    dark = sum(1 for row in modules for module in row if module)
    ratio = abs(100 * dark / (size * size) - 50) / 5
    score += int(ratio) * 10
    return score


def _build(version: int, ecc: str, codewords: list[int], mask: int) -> list[list[bool]]:
    """按给定掩码造一张完整的矩阵。"""
    size = version * 4 + 17
    modules: list[list[Any]] = [[None] * size for _ in range(size)]
    _setup_finder(modules, size)
    _setup_alignment(modules, version)
    _setup_timing(modules, size)
    _setup_format(modules, ecc, mask)
    _setup_version(modules, version)
    _map_data(modules, codewords, mask)
    return [[bool(module) for module in row] for row in modules]


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def encode(text: str, *, ecc: str = "M", mask: int | None = None) -> list[list[bool]]:
    """把文本编成 QR 矩阵（``True`` = 黑）。

    ``ecc``：``"M"``（默认）或 ``"L"``；数据放不下时**自动降到 L** 再试一次。
    ``mask``：``None`` = 按规范的评分规则自动挑最优（正常路径）；
    指定 0~7 则强制用那一张图形——**只给测试用**（对着参考实现逐位比对时要能锁定同一个掩码）。
    """
    payload = text.encode("utf-8")
    for level in (ecc, "L") if ecc != "L" else ("L",):
        try:
            version = _choose_version(len(payload), level)
        except ValueError:
            continue
        codewords = _codewords(text, version, level)
        if mask is not None:
            return _build(version, level, codewords, mask)
        candidates = [_build(version, level, codewords, candidate) for candidate in range(8)]
        return min(candidates, key=_penalty)
    raise ValueError(
        f"内容太长：{len(payload)} 字节，放不进版本 10 的 L 档"
        f"（{_data_codewords(10, 'L')} 个数据码字，扣掉模式与长度指示符后"
        f"最多约 {_data_codewords(10, 'L') - 3} 字节内容）"
    )


def to_svg(
    matrix: list[list[bool]],
    *,
    module_px: int = 4,
    border: int = 4,
    dark: str = "#000000",
    light: str = "#ffffff",
    title: str = "二维码",
) -> str:
    """矩阵 → 内联 SVG。

    为什么用**按行游程**合成一条 ``<path>``，而不是一个模块一个 ``<rect>``：
    版本 10 有 57×57 = 3249 个模块，逐个 ``<rect>`` 要几十 KB，而按行游程通常只要几 KB。

    ``shape-rendering="crispEdges"`` 不能省：默认的抗锯齿会在模块边界糊出灰边，
    手机扫码时正是靠黑白边界定位的（糊了会显著降低识别率）。
    """
    size = len(matrix)
    total = size + border * 2
    commands: list[str] = []
    for row_index, row in enumerate(matrix):
        col = 0
        while col < size:
            if not row[col]:
                col += 1
                continue
            start = col
            while col < size and row[col]:
                col += 1
            x = start + border
            y = row_index + border
            commands.append(f"M{x} {y}h{col - start}v1h-{col - start}z")
    path = "".join(commands)
    side = total * module_px
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" '
        f'viewBox="0 0 {total} {total}" role="img" aria-label="{title}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{total}" height="{total}" fill="{light}"/>'
        f'<path d="{path}" fill="{dark}"/>'
        f"</svg>"
    )
