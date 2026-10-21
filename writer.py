"""
writer.py — XML output writer
Validates required fields and writes the Camquix XML to disk.
"""

import os


def write_output(xml_content: str, output_folder: str, job_num: str) -> str:
    """
    Write xml_content to <output_folder>/<job_num>.xml.
    Returns the full path of the written file.
    Raises OSError or ValueError on failure.
    """
    if not job_num:
        raise ValueError("Job number is empty — cannot determine output filename.")

    if not os.path.isdir(output_folder):
        raise ValueError(f"Output folder does not exist: {output_folder}")

    filename = f"{job_num}.xml"
    out_path = os.path.join(output_folder, filename)

    with open(out_path, "w", encoding="iso-8859-1", errors="replace") as f:
        f.write(xml_content)

    return out_path
