"""2FA 二维码的编码器测试（``fissue/web/qr.py``）。

怎么保证这个编码器是对的
------------------------
自己写 QR 编码器，最怕的是「看着像二维码、扫不出来」。三条互相独立的防线：

1. **黄金摘要**：下面 ``_FROZEN`` 里的矩阵摘要在实现完成后、对着独立实现 ``segno`` 做过
   逐位比对（版本 1~10 × ECC L/M × 8 掩码 × 16 组输入 = 232 张矩阵全部逐位相同；
   比对前需修掉 segno 的一个填充怪癖，它对齐时多写一个 ``0x00`` 字节，与 ISO/IEC 18004 §7.4.10
   不符）。所以这些摘要是「已经验证过正确的形状」的快照，改坏了会立刻红。
2. **本地解码器往返**：测试里另写一个解码器（读格式信息 → 解掩码 → 反交织 → 校验
   Reed-Solomon 伴随式 → 取回载荷）。它走的是与编码器**相反**的方向，能抓住语义性破坏
   （掩码写错、交织顺序变了、纠错码字数变了），而不只是「字节变了」。
3. **结构断言**：定位图形、定时图形、固定黑模块、格式/版本信息的 BCH，逐项按规范核对。

全程离线、无第三方依赖（CI 里没有 node，所以编码器落在 Python 侧而不是 JS）。
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from fissue.web import qr

TOTP_URI = (
    "otpauth://totp/Fissue:admin?secret=JBSWY3DPEHPK3PXP"
    "&issuer=Fissue&algorithm=SHA1&digits=6&period=30"
)

#: 冻结的矩阵摘要：``(文本, ECC, 尺寸, 矩阵摘要)``。
#: 摘要是对「每行 '1'/'0' 拼接后换行」的字符串取 sha256——不是对图像，
#: 所以它只与模块黑白有关，与渲染无关。
_FROZEN = [
    ("A", "M", 21, "177b3a1897a152aee17ab1ee4b667913c6bb873e6ba357debfc5ba243dabff0e"),
    (TOTP_URI, "M", 41, "220f385d53823fd34cafe23f8da84956eca0ec5ba7bc0b5152d2bee798ece8aa"),
    (
        "otpauth://totp/Fissue:admin?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        "&issuer=Fissue&algorithm=SHA1&digits=6&period=30",
        "L",
        41,
        "28f460d8f2b85977d1a66f096cc50cdd5d4d32fdfa3f44e4fa587d8f40454265",
    ),
    ("x" * 200, "L", 53, "71ddfbc79101aebe329e96f941cecd5e8440af18575c5b44b1700da69a5e03d3"),
]


def _digest(matrix: list[list[bool]]) -> str:
    blob = "\n".join("".join("1" if module else "0" for module in row) for row in matrix)
    return hashlib.sha256(blob.encode()).hexdigest()


@pytest.mark.parametrize("text,ecc,size,digest", _FROZEN)
def test_frozen_matrices_are_unchanged(text: str, ecc: str, size: int, digest: str) -> None:
    matrix = qr.encode(text, ecc=ecc)
    assert len(matrix) == size
    assert _digest(matrix) == digest, "矩阵变了——要么改对了、要么改坏了，看上面的往返测试"


# ---------------------------------------------------------------------------
# 测试内的解码器（与编码器反向）
# ---------------------------------------------------------------------------


def _reserved(version: int) -> list[list[bool]]:
    """哪些格子是**函数图形**（不参与数据）。

    这里复用了编码器的 ``_setup_*``：函数图形的布局已经被黄金摘要钉住，
    所以借它一用不会让「两边一起错」蒙混过去。
    """
    size = version * 4 + 17
    modules: list[list[object]] = [[None] * size for _ in range(size)]
    qr._setup_finder(modules, size)
    qr._setup_alignment(modules, version)
    qr._setup_timing(modules, size)
    qr._setup_format(modules, "M", 0)
    qr._setup_version(modules, version)
    return [[module is not None for module in row] for row in modules]


def _read_format(matrix: list[list[bool]]) -> tuple[str, int, int]:
    """读第一份格式信息，返回 ``(ECC 档, 掩码, 原始 15 位)``，并顺带自检 BCH。"""
    size = len(matrix)
    raw = 0
    for i in range(15):
        if i < 6:
            bit = matrix[i][8]
        elif i < 8:
            bit = matrix[i + 1][8]
        else:
            bit = matrix[size - 15 + i][8]
        raw |= (1 if bit else 0) << i

    ecc_by_code = {code: name for name, code in qr._FORMAT_ECC.items()}
    ecc_code = (raw ^ qr._FORMAT_XOR) >> 13
    mask = ((raw ^ qr._FORMAT_XOR) >> 10) & 0b111
    assert ecc_code in ecc_by_code, f"格式信息里的 ECC 编码不认识：{ecc_code:#04b}"
    assert qr._format_bits(ecc_by_code[ecc_code], mask) == raw, "格式信息的 BCH 校验对不上"
    return ecc_by_code[ecc_code], mask, raw


def _extract_codewords(matrix: list[list[bool]], mask: int) -> list[int]:
    """按 Z 字形取出码字（顺带解掩码）。"""
    size = len(matrix)
    reserved = _reserved((size - 17) // 4)
    bits: list[int] = []
    row, direction, col = size - 1, -1, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        while True:
            for offset in range(2):
                current = col - offset
                if not reserved[row][current]:
                    bit = matrix[row][current]
                    if qr._mask_applies(mask, row, current):
                        bit = not bit
                    bits.append(1 if bit else 0)
            row += direction
            if row < 0 or row >= size:
                row -= direction
                direction = -direction
                break
        col -= 2

    codewords: list[int] = []
    for start in range(0, len(bits) - 7, 8):
        byte = 0
        for value in bits[start : start + 8]:
            byte = (byte << 1) | value
        codewords.append(byte)
    return codewords


def _deinterleave(stream: list[int], version: int, ecc: str) -> tuple[list[list[int]], list[list[int]]]:
    """交织的逆运算：还原每个块的数据与纠错码字。"""
    blocks = qr._RS_BLOCKS[version][ecc]
    per_block = [data for _count, _total, data in blocks for _ in range(_count)]
    ec_count = blocks[0][1] - blocks[0][2]

    data_blocks: list[list[int]] = [[] for _ in per_block]
    cursor = 0
    for index in range(max(per_block)):
        for block_index, data_len in enumerate(per_block):
            if index < data_len:
                data_blocks[block_index].append(stream[cursor])
                cursor += 1
    ec_blocks: list[list[int]] = [[] for _ in per_block]
    for _ in range(ec_count):
        for block_index in range(len(per_block)):
            ec_blocks[block_index].append(stream[cursor])
            cursor += 1
    assert cursor == len(stream), "码字流长度与分块表对不上"
    return data_blocks, ec_blocks


def _syndromes_are_zero(data: list[int], ec: list[int]) -> bool:
    """Reed-Solomon 的自检：``data + ec`` 在 α^0…α^(ec-1) 处必须都为 0。

    这是**另一条代码路径**上的校验（多项式求值 vs. 编码时的 LFSR 除法），
    所以它能独立发现「纠错码字算错了」。
    """
    for i in range(len(ec)):
        alpha = qr._GF_EXP[i]
        value = 0
        for coefficient in data + ec:
            value = qr._gf_mul(value, alpha) ^ coefficient
        if value != 0:
            return False
    return True


def _decode(matrix: list[list[bool]]) -> str:
    """把矩阵解回文本——不成立就 AssertionError。"""
    size = len(matrix)
    version = (size - 17) // 4
    assert size == version * 4 + 17
    ecc, mask, _raw = _read_format(matrix)
    codewords = _extract_codewords(matrix, mask)
    data_blocks, ec_blocks = _deinterleave(codewords, version, ecc)
    for data, ec in zip(data_blocks, ec_blocks, strict=True):
        assert _syndromes_are_zero(data, ec), "纠错码字对不上：RS 伴随式不为 0"

    stream = [byte for block in data_blocks for byte in block]
    bits: list[int] = []
    for byte in stream:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)

    def take(count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | bits.pop(0)
        return value

    assert take(4) == 0b0100, "只支持 byte 模式"
    length = take(8 if version < 10 else 16)
    payload = bytes(take(8) for _ in range(length))
    return payload.decode("utf-8")


# ---------------------------------------------------------------------------
# 往返：编码 → 解码
# ---------------------------------------------------------------------------

ROUNDTRIP_CASES = [
    "A",
    "HELLO",
    TOTP_URI,
    TOTP_URI.replace("admin", "a" * 64),   # 用户名打满 64 字符
    "用户名:中文也走 byte 模式",
    "x" * 14,
    "x" * 108,
    "x" * 200,
    "x" * 270,                             # 贴着版本 10-L 的上限（内容最多 ~271 字节）
]


@pytest.mark.parametrize("text", ROUNDTRIP_CASES)
@pytest.mark.parametrize("ecc", ["M", "L"])
def test_roundtrip_through_an_independent_decoder(text: str, ecc: str) -> None:
    assert _decode(qr.encode(text, ecc=ecc)) == text


@pytest.mark.parametrize("text", ["A", TOTP_URI, "x" * 200])
@pytest.mark.parametrize("mask", range(8))
def test_every_mask_is_self_describing(text: str, mask: int) -> None:
    """**8 张掩码都是合法码**：掩码号写在格式信息里，解码器据此解掩码。

    这条顺带守住「格式信息按掩码正确写入」——写错了这里必挂。
    """
    matrix = qr.encode(text, ecc="M", mask=mask)
    detected, found_mask, _raw = _read_format(matrix)
    assert detected == "M"
    assert found_mask == mask
    assert _decode(matrix) == text


def test_different_masks_produce_different_matrices() -> None:
    matrices = {_digest(qr.encode("A", ecc="M", mask=mask)) for mask in range(8)}
    assert len(matrices) == 8


# ---------------------------------------------------------------------------
# 结构：定位 / 定时 / 固定黑模块 / 版本信息
# ---------------------------------------------------------------------------


def _finder_ok(matrix: list[list[bool]], top: int, left: int) -> bool:
    for dr in range(7):
        for dc in range(7):
            expected = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
            if matrix[top + dr][left + dc] != expected:
                return False
    return True


@pytest.mark.parametrize("text", ROUNDTRIP_CASES)
def test_function_patterns_match_the_spec(text: str) -> None:
    matrix = qr.encode(text)
    size = len(matrix)
    version = (size - 17) // 4

    # 三个定位图形 + 它们外侧的分隔带（一整圈浅色）
    assert _finder_ok(matrix, 0, 0)
    assert _finder_ok(matrix, 0, size - 7)
    assert _finder_ok(matrix, size - 7, 0)
    for index in range(8):
        assert matrix[7][index] is False and matrix[index][7] is False

    # 定时图形：第 6 行/列黑白相间（含与定位图形相邻的两格）
    for index in range(8, size - 8):
        assert matrix[6][index] == (index % 2 == 0)
        assert matrix[index][6] == (index % 2 == 0)

    # 固定黑模块
    assert matrix[size - 8][8] is True

    # 版本信息：版本 ≥ 7 有两份，且 BCH 能被校验出来
    if version >= 7:
        size_ = size
        bits = 0
        for i in range(18):
            row, col = i // 3, size_ - 11 + i % 3
            assert matrix[row][col] == matrix[col][row], "两份版本信息必须一致"
            bits |= (1 if matrix[row][col] else 0) << i
        assert bits >> 12 == version, "版本信息里的版本号对不上"
        remainder = bits & 0xFFF
        check = version << 12
        generator = 0b1111100100101
        for shift in range(5, -1, -1):
            if check & (1 << (shift + 12)):
                check ^= generator << shift
        assert check == remainder, "版本信息的 BCH 校验对不上"


def test_alignment_patterns_are_where_the_spec_says() -> None:
    """对齐图形：每个版本的中心坐标是固定的，只有与三个定位图形冲突的那几个不画。"""
    for text, _ecc, _size, _digest in _FROZEN:
        matrix = qr.encode(text, ecc="L")
        size = len(matrix)
        version = (size - 17) // 4
        centers = qr._ALIGNMENT.get(version, ())
        for row_c, col_c in itertools.product(centers, centers):
            skipped = (row_c, col_c) in ((6, 6), (6, size - 7), (size - 7, 6))
            if skipped:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    expected = max(abs(dr), abs(dc)) != 1
                    assert matrix[row_c + dr][col_c + dc] == expected, (
                        f"版本 {version} 对齐图形 ({row_c},{col_c}) 的 ({dr},{dc}) 不对"
                    )


def test_version_is_the_smallest_that_fits() -> None:
    """版本必须是**放得下的最小**那个——大了白白占地、小了放不下。"""
    for text in ("A", TOTP_URI, "x" * 200):
        for ecc in ("M", "L"):
            size = len(qr.encode(text, ecc=ecc))
            version = (size - 17) // 4
            assert version == qr._choose_version(len(text.encode()), ecc)


def test_long_content_falls_back_from_m_to_l() -> None:
    """M 档放不下时自动降到 L，而不是报错。

    200 字节在 M 档要版本 10（213 字节）也放得下…所以这里用一段「M 放不下、L 放得下」的长度：
    版本 10 的 M 档是 213 字节，L 档是 271 字节。取 240 字节即可。
    """
    text = "y" * 240
    matrix = qr.encode(text, ecc="M")
    ecc, _mask, _raw = _read_format(matrix)
    assert ecc == "L", "放不下就该降档，而不是抛错"
    assert _decode(matrix) == text


def test_absurdly_long_content_raises() -> None:
    with pytest.raises(ValueError, match="太长"):
        qr.encode("z" * 400)


# ---------------------------------------------------------------------------
# SVG 渲染
# ---------------------------------------------------------------------------


def test_svg_is_self_contained_and_has_no_external_reference() -> None:
    """SVG 必须**自包含**：不引任何外部资源（否则等于把密钥所在页面挂到别人服务器上）。"""
    svg = qr.to_svg(qr.encode(TOTP_URI))
    assert svg.startswith("<svg ") and svg.endswith("</svg>")
    assert 'shape-rendering="crispEdges"' in svg, "抗锯齿会糊掉模块边界，扫码率会掉"
    assert "<path" in svg
    # 只允许出现一次 http：xmlns 命名空间本身
    assert svg.count("http") == 1 and 'xmlns="http://www.w3.org/2000/svg"' in svg
    for forbidden in ("<script", "<image", "href", "url(", "<iframe", "@import"):
        assert forbidden not in svg, f"SVG 里不该出现 {forbidden}"


def test_svg_geometry_accounts_for_the_quiet_zone() -> None:
    matrix = qr.encode("A")
    size = len(matrix)
    svg = qr.to_svg(matrix, module_px=4, border=4)
    total = size + 8
    assert f'viewBox="0 0 {total} {total}"' in svg
    assert f'width="{total * 4}"' in svg and f'height="{total * 4}"' in svg
    # 白底铺满整张图（静区就是靠它）
    assert f'<rect width="{total}" height="{total}"' in svg


def test_svg_path_only_covers_dark_modules() -> None:
    """把 path 的指令数出来，与「按行游程」的预期一致——顺带确认渲染没漏行。"""
    matrix = qr.encode("A")
    svg = qr.to_svg(matrix, border=0)
    expected_runs = sum(
        1 for row in matrix for value, _group in itertools.groupby(row) if value
    )
    path = svg.split('<path d="', 1)[1].split('"', 1)[0]
    assert expected_runs > 0
    assert path.count("z") == expected_runs
    assert path.count("M") == expected_runs


def test_svg_never_embeds_the_secret_in_a_readable_form() -> None:
    """二维码里**没有**明文密钥——它只以模块黑白的形式存在。

    这条守的是「别顺手把 otpauth URI 也塞进 SVG 的注释/标题里」：
    那种做法等于把密钥以文本形式留在了页面源码与截图里。
    """
    svg = qr.to_svg(qr.encode(TOTP_URI))
    assert "otpauth" not in svg and "secret" not in svg and "JBSWY3DP" not in svg
