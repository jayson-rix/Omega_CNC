"""
OMEGA SAWLINK FOM library stripper
=================================

Strips a Softech V6 XML job down to a minimal FOM FMC 370 / 340 XML that
references the FOM machine's *on-machine* library by name.

Differences vs. the full ``fom_transformer.transform_fom`` pipeline:

* No tool resolution / no geometry — the FOM CNC already holds the full
  recipe for each machining op inside its own library; we only emit the
  ``WCODE`` reference and the X-position offset.
* Only bars that contain at least one cut with a non-empty ``<Works>``
  block survive — bars that are pure stock cuts are skipped entirely.
* Each surviving ``Cut`` emits one ``<MACHINING WCODE="…" OFFSET="…">``
  per V6 ``Work`` entry, with WCODE drawn from ``WComment`` and OFFSET
  drawn from ``WX1``.

The shape of the output (JOB → VER → BODY → BAR → CUT → MACHININGS →
MACHINING) matches the reference file
``attached_assets/FOM_20260522_133054_…xml`` byte-for-byte for the same
input.

This module ports the user's standalone ``fom_xml.py`` stripper directly
into the OMEGA CNC-zee pipeline so it shares the same ``Job`` /
``Bar`` / ``Cut`` / ``Work`` dataclasses produced by ``parser.parse_v6``.
"""

from __future__ import annotations

try:
    from .parser import Job  # package import
except ImportError:  # script/PyInstaller flat-layout import
    from parser import Job  # type: ignore[no-redef]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _convert_angle(angle: float) -> int:
    """V6 → FOM angle convention. Matches the reference stripper exactly
    (truncation via ``int(float(x))``, *not* rounding):

    * 45° (mitre)  → 135 (FOM's outside-angle convention)
    * 0°  (unset)  → 90  (square / default)
    * everything else passes through, integer-truncated
    """
    a = int(float(angle))
    if a == 45:
        return 135
    if a == 0:
        return 90
    return a


def _fmt_offset(x: float) -> str:
    """Format an offset distance to match the reference stripper output:
    whole numbers as plain integers (``2150``), fractional numbers as
    their natural ``str()`` form (``832.5``). The V6 parser already
    decoded WX1 from text via ``float``, so ``str()`` round-trips the
    common cases exactly."""
    f = float(x)
    if f.is_integer():
        return str(int(f))
    return repr(f) if "e" in str(f) else str(f)


def _machining_block(wcomment: str, wx1: float) -> str:
    """Render one ``<MACHINING>`` reference block. Layout / indentation
    matches the reference FOM stripper byte-for-byte."""
    return (
        f'          <MACHINING WCODE="{wcomment}" \n'
        f'            VAR1="0" VAR2="0" VAR3="0" VAR4="0"\n'
        f'            VERPERC="0" ANGLE="0" OFFSET="{_fmt_offset(wx1)}" '
        f'OFFSETY="0" OFFSETZ="0"\n'
        f'            FACE="0" P1X="0" P1Y="0" P1Z="0" P2X="0" P2Y="0" '
        f'P2Z="0" \n'
        f'            EMPTYGROVE="0" ORDINEESECSRG="99999" '
        f'ORDINEESECFIN="99999" \n'
        f'            CODUTENSILE="0" CILINLAV="3" OFFSETFIN="0" '
        f'CODEUTEFIN="-1"\n'
        f'            CILLAVFIN="0">\n'
        f'            <PIANI>\n'
        f'              <PIANO START="0" THICK="0"/>\n'
        f'            </PIANI>\n'
        f'          </MACHINING>'
    )


# ─── Main entry point ─────────────────────────────────────────────────────────

def transform_fom_library(job: Job) -> tuple[str, list[str], dict]:
    """Convert a parsed V6 ``Job`` into a FOM library-reference XML.

    Returns ``(xml_text, warnings, stats)`` where ``stats`` carries the
    counts the UI surfaces (``bars_in``, ``bars_out``, ``cuts_in``,
    ``cuts_out``, ``machinings_out``).
    """
    warnings: list[str] = []
    stats = {
        "bars_in": 0, "bars_out": 0,
        "cuts_in": 0, "cuts_out": 0,
        "machinings_out": 0,
    }

    lines: list[str] = [
        '<?xml version="1.0" encoding="iso-8859-1"?>',
        '<JOB>',
        '  <VER>',
        '    <MJ>1</MJ>',
        '    <MN>0</MN>',
        '  </VER>',
        '  <HEAD></HEAD>',
        '  <BODY>',
    ]

    for bar in job.bars:
        stats["bars_in"] += 1
        stats["cuts_in"] += len(bar.cuts)

        if not bar.bidentno:
            warnings.append(
                f"Skipping bar (no BIdentNo) — {len(bar.cuts)} cut(s) "
                f"discarded."
            )
            continue

        cuts_with_machining = [c for c in bar.cuts if c.works]
        if not cuts_with_machining:
            # Pure stock cuts — no machining → bar omitted from FOM file.
            continue

        stats["bars_out"] += 1

        lines.append('    <BAR>')
        lines.append('      <BRAN>OMEGA</BRAN>')
        lines.append('      <SYST>OMEGA</SYST>')
        lines.append(f'      <CODE>{bar.bidentno}</CODE>')
        lines.append(f'      <DICL>{bar.bsurface}</DICL>')
        lines.append(f'      <DOCL>{bar.bsurface}</DOCL>')
        lines.append(f'      <LEN>{int(bar.blength)}</LEN>')

        h = bar.bheight
        # LENR is bar-length minus the first cut's IL — emitted once at the
        # top of the bar (matches the reference stripper exactly).
        first_cut = cuts_with_machining[0]
        lenr = bar.blength - first_cut.clength
        lines.append(f'      <LENR>{int(lenr)}</LENR>')
        lines.append(f'      <H>{int(h)}</H>')
        lines.append('      <MLT>1</MLT>')

        for cut in cuts_with_machining:
            stats["cuts_out"] += 1

            angl = _convert_angle(cut.canglelh)
            angr = _convert_angle(cut.canglerh)
            ab1  = _convert_angle(cut.canglelv)
            ab2  = _convert_angle(cut.canglerv)

            il = cut.clength
            # FOM "outside length" — double-mitred cuts subtract 2×H.
            ol = il - 2 * h if angl == 135 and angr == 135 else il

            lines.append('      <CUT>')
            lines.append(f'        <ANGL>{angl}</ANGL>')
            lines.append(f'        <ANGR>{angr}</ANGR>')
            lines.append(f'        <AB1>{ab1}</AB1>')
            lines.append(f'        <AB2>{ab2}</AB2>')
            lines.append(f'        <OL>{int(ol)}</OL>')
            lines.append(f'        <IL>{int(il)}</IL>')
            lines.append(f'        <H>{int(h)}</H>')
            lines.append('        <TRML></TRML>')
            lines.append('        <TAL></TAL>')
            lines.append('        <TRMR></TRMR>')
            lines.append('        <TAR></TAR>')
            lines.append(f'        <BCOD>{cut.ccomno}</BCOD>')
            lines.append('        <MACHININGS>')

            for work in cut.works:
                # Pass WComment through raw (no strip) — FOM library
                # lookup is exact-string and any whitespace the operator
                # baked into the V6 name must survive intact.
                wcomment = work.wcomment or ""
                if not wcomment.strip():
                    warnings.append(
                        f"Cut {cut.ccomno}: work has no WComment — "
                        f"emitting empty WCODE (FOM library lookup will "
                        f"fail)."
                    )
                lines.append(_machining_block(wcomment, work.wx1))
                stats["machinings_out"] += 1

            lines.append('        </MACHININGS>')
            lines.append('      </CUT>')

        lines.append('    </BAR>')

    lines.append('  </BODY>')
    lines.append('</JOB>')

    # No trailing newline — matches the reference stripper output byte
    # for byte (the FOM CNC tolerates either way).
    return ("\n".join(lines), warnings, stats)
