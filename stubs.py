"""
stubs.py — Placeholder generators for FOM, Yilmaz, and Emmegi
These write a clear "not yet configured" message file and return
an info string for display in the UI.
"""

import os


_MESSAGE = """\
CNC Preprocessor — Output Format Not Yet Configured
====================================================

Machine brand: {brand}
Job number:    {job_num}
Date:          {date}

This output format has not been implemented yet.
To enable it, please provide a sample output XML file from
your {brand} software. The developer can then add the full
transformation logic for this machine brand.

Contact: supply a sample {brand} XML file generated from a
known Softech V6 input so the field mappings can be verified.
"""


def generate_stub(brand: str, job_num: str, date: str,
                  output_folder: str) -> str:
    """
    Write a placeholder text file for unsupported machine brands.
    Returns the path of the file written.
    """
    content = _MESSAGE.format(brand=brand, job_num=job_num, date=date)
    filename = f"{job_num}_{brand}_NOT_CONFIGURED.txt"
    out_path = os.path.join(output_folder, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
