"""Build the committed ``vba_macro.xlsm`` fixture.

Why this exists
---------------
ExcelPilot's VBA handling is only meaningful to test against a package that
actually contains ``xl/vbaProject.bin``. Without one, the previous test injected
a 520-byte blob with a correct OLE magic number and asserted only that
``has_vba()`` returned True — which proves the *detector* works, not that the
*preservation* works.

What this fixture is
--------------------
A structurally genuine macro-enabled package containing a structurally valid
OLE2/CFB compound file as ``xl/vbaProject.bin``:

* a real ``[Content_Types].xml`` entry of type ``application/vnd.ms-office.vbaProject``
* a real ``xl/_rels/workbook.xml.rels`` relationship to it
* a CFB container with a valid 512-byte header, a self-consistent FAT, a
  directory with a proper root storage entry, and one named stream

**What it is not:** a real VBA project. It has no ``dir`` stream, no
``PROJECT`` stream, and no MS-OVBA-compressed modules, so Excel would not run
macros from it. It is a *structural* stand-in.

That is a deliberate, bounded scope. It exercises everything ExcelPilot actually
does with a macro workbook — extension handling, ``keep_vba``, byte-level
preservation, package validity, and the ``vba_read_only`` policy denial — none
of which depends on the macro source being meaningful.

It is not a substitute for testing against a real macro project. That is done
against the user's actual workbooks by
``tests/test_workbook.py::TestRealMacroWorkbook``, which is skipped when the
collection is not present, and its measured result is recorded in
``docs/limitations.md``.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path

import openpyxl

#: OOXML well-known part names.
CONTENT_TYPES = "[Content_Types].xml"
WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
VBA_PART = "xl/vbaProject.bin"

SECTOR = 512
MINI_SECTOR = 64
MINI_CUTOFF = 4096

#: A stream below 4096 bytes lives in the mini stream, which needs a MiniFAT and
#: a root-owned mini stream. Padding the single stream past the cutoff keeps the
#: container to FAT-only addressing, which is far easier to verify by hand and
#: leaves no room for a subtly wrong MiniFAT.
STREAM_NAME = "VbaProbe"
STREAM_FILLER = 4200

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF


def _cfb_stream_payload() -> bytes:
    """The bytes of the single named stream inside the container."""
    return (STREAM_NAME.encode("utf-16-le") + b"\x00\x00" + b"vba-preservation-probe").ljust(
        STREAM_FILLER, b"\x00"
    )


def build_vba_project_bin() -> bytes:
    """Return a minimal but structurally valid OLE2/CFB compound file.

    Layout (all 512-byte sectors, sector 0 is the FAT):

    ==========  ==================================================
    Sector      Contents
    ==========  ==================================================
    0           FAT (128 entries)
    1           Directory (4 entries x 128 bytes)
    2           The single stream's data
    ==========  ==================================================

    The FAT is self-consistent: sector 0 is ``FATSECT``, sector 1 and 2 form a
    chain, and every other entry is ``FREESECT``. A reader that walks the FAT
    therefore finds exactly the directory and exactly the stream.
    """
    payload = _cfb_stream_payload()
    assert len(payload) >= MINI_CUTOFF, "stream must exceed the mini-stream cutoff"

    # -- FAT: 128 entries, enough for a 64 KB container.
    fat = [FREESECT] * (SECTOR // 4)
    fat[0] = FATSECT  # the FAT sector itself
    fat[1] = 2  # directory -> stream data
    fat[2] = ENDOFCHAIN  # stream data -> end

    # -- Directory: Root Entry, then the one stream.
    def entry(name: str, obj_type: int, child: int, start: int, size: int) -> bytes:
        raw = bytearray(128)
        encoded = name.encode("utf-16-le") + b"\x00\x00"
        raw[0 : len(encoded)] = encoded
        struct.pack_into("<H", raw, 64, len(encoded))
        raw[66] = obj_type
        struct.pack_into("<I", raw, 68, NOSTREAM)  # colour: black
        struct.pack_into("<I", raw, 72, NOSTREAM)  # left sibling
        struct.pack_into("<I", raw, 76, NOSTREAM)  # right sibling
        struct.pack_into("<I", raw, 80, child)  # child
        struct.pack_into("<I", raw, 116, start)  # starting sector
        struct.pack_into("<Q", raw, 120, size)  # stream size
        return bytes(raw)

    root = entry("Root Entry", 5, NOSTREAM, ENDOFCHAIN, 0)
    stream = entry(STREAM_NAME, 2, NOSTREAM, 2, len(payload))
    empty = b"\x00" * 128
    directory = root + stream + empty + empty

    # -- Header.
    header = bytearray(SECTOR)
    header[0:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 0x0003)  # major version 3
    struct.pack_into("<H", header, 28, 0xFFFE)  # little endian
    struct.pack_into("<H", header, 30, 9)  # sector shift  -> 512
    struct.pack_into("<H", header, 32, 6)  # mini shift    -> 64
    struct.pack_into("<I", header, 44, 1)  # number of FAT sectors
    struct.pack_into("<I", header, 48, 1)  # first directory sector
    struct.pack_into("<I", header, 56, MINI_CUTOFF)
    struct.pack_into("<I", header, 60, ENDOFCHAIN)  # first MiniFAT sector
    struct.pack_into("<I", header, 64, 0)  # MiniFAT sector count
    struct.pack_into("<I", header, 68, ENDOFCHAIN)  # first DIFAT sector
    struct.pack_into("<I", header, 72, 0)  # DIFAT sector count
    difat = [0] + [FREESECT] * 108
    struct.pack_into("<109I", header, 76, *difat)

    fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
    data_sectors = b"".join(
        payload[i : i + SECTOR].ljust(SECTOR, b"\x00") for i in range(0, len(payload), SECTOR)
    )

    container = bytes(header) + fat_bytes + directory + data_sectors

    # The header declares exactly 3 sectors (FAT, directory, data), so the file
    # length must agree. Asserting it here means a layout change cannot silently
    # produce a truncated container.
    declared = 512 + SECTOR * (1 + 1 + len(data_sectors) // SECTOR)
    assert len(container) == declared, f"{len(container)} != {declared}"
    return container


def build_workbook_bytes(sheet_rows: list[list[object]]) -> bytes:
    """A minimal xlsx payload produced by openpyxl, returned as bytes."""
    buffer = io.BytesIO()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Inventory"
    for row in sheet_rows:
        sheet.append(row)
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


#: The plain ``.xlsx`` content type for the main workbook part, replaced so the
#: package is genuinely macro-enabled rather than merely carrying a stray bin.
#:
#: openpyxl emits a self-closing tag with a space before ``/>``, and the exact
#: spacing is a formatting detail that could change. Matching the element with a
#: regex keyed on ``PartName`` and ``ContentType`` avoids depending on it — an
#: earlier exact-string version silently failed to match, which would have left
#: the package claiming to be a plain ``.xlsx``.
_WORKBOOK_TYPE_PATTERN = re.compile(
    r"<Override\b[^>]*PartName=\"/xl/workbook\.xml\"[^>]*>", re.IGNORECASE
)
_MACRO_WORKBOOK_TYPE = (
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'ms-excel.sheet.macroEnabled.main+xml" />'
)
_VBA_DEFAULT_TYPE = '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
_VBA_RELATIONSHIP = (
    '<Relationship Id="rIdVbaProject" Type="http://schemas.microsoft.com/office/'
    '2006/relationships/vbaProject" Target="vbaProject.bin"/>'
)

#: Placeholder rows only — no real inventory, names, or paths — so the generated
#: fixture is safe to produce anywhere. The last column holds real formulas so
#: the fixture also exercises formula-bearing macro workbooks.
PLACEHOLDER_ROWS: list[list[object]] = [
    ["Item", "Category", "Count", "Extended"],
    ["Widget A", "Hardware", 12, "=C2*2"],
    ["Gadget B", "Hardware", 7, "=C3*2"],
    ["Doohickey C", "Consumable", 30, "=C4*2"],
]


def build_macro_workbook(destination: Path) -> Path:
    """Write a ``.xlsm`` whose package genuinely declares a VBA project.

    Three things make it a macro-enabled package rather than a ``.xlsx`` with a
    stray file appended, and all three are asserted by
    ``tests/test_workbook.py::TestMacroEnabledFixture``:

    1. ``xl/vbaProject.bin`` exists and is a structurally valid CFB container
    2. ``[Content_Types].xml`` declares the ``bin`` default and marks
       ``xl/workbook.xml`` as ``macroEnabled``
    3. ``xl/_rels/workbook.xml.rels`` references the part

    No binary is committed. The workbook is generated from this source at test
    time, per the repository convention for generated fixtures.
    """
    base = build_workbook_bytes(PLACEHOLDER_ROWS)
    vba = build_vba_project_bin()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
        with zipfile.ZipFile(io.BytesIO(base)) as source:
            for item in source.infolist():
                data = source.read(item.filename)

                if item.filename == CONTENT_TYPES:
                    text = data.decode("utf-8")
                    text, replaced = _WORKBOOK_TYPE_PATTERN.subn(_MACRO_WORKBOOK_TYPE, text)
                    if replaced != 1:
                        raise RuntimeError(
                            "could not rewrite the workbook content type; the "
                            f"package would claim to be a plain .xlsx ({replaced} "
                            "matches)"
                        )
                    if "vbaProject" not in text:
                        text = text.replace("</Types>", f"{_VBA_DEFAULT_TYPE}</Types>")
                    data = text.encode("utf-8")

                elif item.filename == WORKBOOK_RELS:
                    text = data.decode("utf-8")
                    if "vbaProject" not in text:
                        text = text.replace(
                            "</Relationships>", f"{_VBA_RELATIONSHIP}</Relationships>"
                        )
                    data = text.encode("utf-8")

                out.writestr(item, data)

        out.writestr(VBA_PART, vba)

    return destination


__all__ = [
    "PLACEHOLDER_ROWS",
    "STREAM_NAME",
    "VBA_PART",
    "build_macro_workbook",
    "build_vba_project_bin",
]
