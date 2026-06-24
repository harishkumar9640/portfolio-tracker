"""
cas_dump.py  —  Extract the raw text from your CAS PDF and write it to a file.

Run:
    python3 cas_dump.py cas.pdf

Produces:
    cas_text.txt   ← the entire PDF as plain text (open in TextEdit or VS Code)

You read this file yourself to see what the CAS actually looks like.
Once you know the format, you (or I) can write a parser that matches it.
"""
import json
import sys
from pathlib import Path

from pypdf import PdfReader

PROJECT = Path(__file__).resolve().parent
SECRETS_FILE = PROJECT / "secrets.local.json"


def load_password() -> str:
    raw = SECRETS_FILE.read_text() if SECRETS_FILE.exists() else ""
    cleaned = "\n".join(
        line for line in raw.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )
    data = json.loads(cleaned) if cleaned.strip() else {}
    pw = (data.get("cas_pdf_password") or "").strip()
    if not pw or pw == "your_cas_password_here":
        raise RuntimeError("set cas_pdf_password in secrets.local.json")
    return pw


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 cas_dump.py <cas.pdf>")
        return
    pdf = Path(sys.argv[1]).expanduser()
    if not pdf.exists():
        print(f"FAIL: not found: {pdf}")
        return
    try:
        pw = load_password()
    except Exception as e:
        print(f"FAIL: {e}")
        return

    reader = PdfReader(str(pdf))
    if reader.is_encrypted:
        if not reader.decrypt(pw):
            print("FAIL: PDF password is incorrect")
            return

    chunks = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as e:
            t = f"[page {i+1}: extract error: {e}]"
        chunks.append(f"\n===== PAGE {i+1} =====\n{t}")

    text = "".join(chunks)
    out = pdf.parent / "cas_text.txt"
    out.write_text(text)
    n_lines = text.count("\n")
    print(f"Wrote: {out}")
    print(f"  pages:  {text.count('===== PAGE')}")
    print(f"  chars:  {len(text)}")
    print(f"  lines:  {n_lines}")
    print()
    print("--- First 30 lines (for a quick look) ---")
    for ln in text.splitlines()[:30]:
        print(ln)


if __name__ == "__main__":
    main()
