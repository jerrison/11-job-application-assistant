"""Validate a built resume PDF for the 2-page constraint."""

import sys
from pathlib import Path

import json_lenient
import pdfplumber
from enforce_resume_policy import minimum_bullet_deficits


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <resume.pdf>")
        sys.exit(2)

    pdf_path = sys.argv[1]
    passed = True

    # ── JSON content checks ──
    # Derive resume_content.json path from PDF path
    content_json = Path(pdf_path).parent.parent / "content" / "resume_content.json"
    if content_json.exists():
        data = json_lenient.loads(content_json.read_text(encoding="utf-8"))
        if not data.get("summary"):
            passed = False
            print(
                "FAIL: Resume summary is missing (null). Write a 2-3 sentence "
                "summary that communicates the candidate's fit for this specific "
                "role using JD keywords. The summary field must be a non-null string."
            )
        else:
            print("PASS: Resume summary is present.")

        tagline = data.get("tagline") or ""
        if "Principal Product Manager" in tagline:
            passed = False
            print(
                "FAIL: Tagline still says 'Principal Product Manager'. The first "
                "segment must match the TARGET ROLE TITLE from the JD (e.g. "
                "'Senior Product Manager', 'Staff Product Manager', 'Founding PM')."
            )
        else:
            print("PASS: Tagline role title is tailored.")

        deficits = minimum_bullet_deficits(data)
        if deficits:
            passed = False
            for position_id, current, minimum in deficits:
                print(
                    f"FAIL: Resume policy requires at least {minimum} bullet(s) for {position_id}; "
                    f"found {current}."
                )
        else:
            print("PASS: Resume retains the required minimum bullets for each position.")

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as e:
        print(f"FAIL: Could not open PDF: {e}")
        sys.exit(1)

    with pdf:
        num_pages = len(pdf.pages)
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Page count check
    if num_pages == 2:
        print("PASS: Resume is exactly 2 pages.")
    elif num_pages == 1:
        passed = False
        print(
            "FAIL: Resume is only 1 page. Add more bullets from the master resume "
            "pool first; only add or expand the summary if still needed, and keep "
            "it as tight as possible without dropping important signal. Then "
            "re-run the page-break optimizer."
        )
    else:
        passed = False
        print(
            f"FAIL: Resume is {num_pages} pages. Cut low-value bullets, shorten "
            f"verbose bullets, then re-run the page-break optimizer. "
            f"Only remove the summary as a last resort."
        )

    # Sanity check for candidate name
    if "JERRISON LI" in full_text.upper():
        print("PASS: Candidate name 'JERRISON LI' found.")
    else:
        passed = False
        print("FAIL: Candidate name 'JERRISON LI' not found in PDF text.")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
