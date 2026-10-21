# CNC Preprocessor — Build & Usage Guide

## What this does

Reads a **Softech V6 XML** job file and converts it to **Camquix-format XML**
that CAM3D (Mecal) can import directly. No Camquix licence required.

---

## Requirements (on your Windows PC)

| Tool | Where to get it |
|------|----------------|
| Python 3.11+ (64-bit) | https://python.org/downloads/ — tick **"Add Python to PATH"** |

---

## Build the .exe (one time)

1. Copy the **`cnc_preprocessor`** folder to your Windows PC
2. Double-click **`build.bat`** inside that folder
3. Wait ~1–2 minutes
4. Your app is at `dist\CNC_Preprocessor\CNC_Preprocessor.exe`

To distribute, zip the entire `dist\CNC_Preprocessor\` folder.
Recipients just unzip and double-click — no Python needed.

---

## Running from source (no build needed)

```bat
cd cnc_preprocessor
pip install customtkinter
python main.py
```

Or install all dependencies at once:

```bat
pip install -r requirements.txt
python main.py
```

---

## Using the app

1. Click **Browse…** next to "Input File" and select your V6 XML file
2. The job summary appears automatically (job number, date, bar/piece counts)
3. Select the **Machine brand** (Mecal is fully supported)
4. Choose an **output folder**
5. Click **▶ Generate Output XML**
6. The output file is saved as `<JobNumber>.xml` in the chosen folder
7. Open this file directly in **CAM3D**

### Profile and DXF Library

- Profile Details, SIDE / WSide entries, and Machinings each have their own
  controls. XML metadata and detected operations are temporary drafts.
  A machine profile is first created only when **Save SIDE** or
  **Save Selected Machining** is clicked.
- Every machine has an isolated `machine_libraries/<machine>/profiles.json`
  and DXF suite tree. Import, delete, rename, backup, and export actions never
  share profile IDs or DXFs with another machine.
- Search profiles by profile number or description, then filter by suite and
  extrusion type.
- Select a profile and click **Import DXF** to copy a `.dxf` file into its
  machine-specific 300–800 suite/type folder. The preview expands nested
  blocks and renders lines, polylines, arcs, circles, ellipses, splines, faces,
  and mesh edges directly.
- DXF coordinates are treated as millimetres. The preview measures the actual
  model-space geometry and fits each profile independently, including profiles
  wider than 300 mm; it does not use a fixed drawing box.

---

## Configuring tool numbers and face mappings

Edit **`config.json`** (inside the `dist\CNC_Preprocessor\` folder after building,
or next to `main.py` when running from source).

### Tool numbers
```json
"tools": {
    "STD_HOLE":  3,
    "STD_DRILL": 6,
    "STD_SLOT":  3
}
```
Change the numbers to match your machine's tool setup.

### Drill vs Hole logic
```json
"drill_comments": ["H"]
```
Pieces with CComment value `"H"` (Head) produce **STD_DRILL** operations.
All other circular works produce **STD_HOLE**. Add more letters as needed.

### Depth calculations
```json
"depth_offset":      5,
"hole_par1_offset": -5,
"slot_par1_offset":  2
```
- `PositionZ = WDepth + depth_offset`
- `PAR1 (hole) = abs(WHeight) + hole_par1_offset`
- `PAR1 (slot) = -(WDepth + slot_par1_offset)`

---

## Cut angle mapping (V6 → Camquix)

| V6 field | Camquix field | Formula |
|----------|--------------|---------|
| CAngleLH | sxB | `180 - CAngleLH` |
| CAngleRH | dxB | `180 - CAngleRH` |
| CAngleLV | sxC | `90 + CAngleLV`  |
| CAngleRV | dxC | `90 + CAngleRV`  |

Example: 45° miter → `180 - 45 = 135` in Camquix ✓

---

## Supported / unsupported machine brands

| Brand | Status |
|-------|--------|
| Mecal (CAM3D) | Fully implemented |
| FOM | Not yet configured — provide a sample XML file |
| Yilmaz | Not yet configured — provide a sample XML file |
| Emmegi | Not yet configured — provide a sample XML file |

Selecting an unsupported brand writes a `*_NOT_CONFIGURED.txt` placeholder
file explaining what sample file is needed.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Python not found" during build | Re-install Python and tick "Add to PATH" |
| "No module named tkinter" | Use the official Python.org installer (Tkinter is included) |
| XML parse error | Confirm the input is a valid Softech V6 XML file (root element must be `<Job>`) |
| Wrong cut angles in output | Verify CAngle values in the V6 file and adjust the formula in `transformer.py` if needed |
| Wrong tool numbers | Edit `config.json` tool entries |
