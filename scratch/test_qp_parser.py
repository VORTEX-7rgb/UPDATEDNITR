import os
import sys

# Add app directory to sys.path to resolve imports correctly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.nitris.examination_parser import parse_question_papers_html

def run_tests():
    sample_path = r"c:\Users\mrara\OneDrive\Desktop\collegeclaw\questions_sample.html"
    if not os.path.exists(sample_path):
        print("FAIL: Sample file questions_sample.html not found!")
        sys.exit(1)

    print("Running Examination Parser Unit Tests...")
    
    with open(sample_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Run parser
    records = parse_question_papers_html(html)
    
    # Assert total records count
    # Total rows = 532, all rows represent valid subjects with numeric serials.
    print(f"Parsed {len(records)} subject records successfully.")
    assert len(records) == 532, f"Expected 532 records, got {len(records)}"
    print("[OK] Assertion Passed: Exact record count of 532 matches!")

    # Find specific subjects and assert their details
    bm1002 = next((r for r in records if r.subject_code == "BM1002"), None)
    assert bm1002 is not None, "BM1002 not found!"
    assert bm1002.subject_name == "Introduction to Bioengineering", f"Expected 'Introduction to Bioengineering', got '{bm1002.subject_name}'"
    assert bm1002.ltp == "3-0-0", f"Expected '3-0-0', got '{bm1002.ltp}'"
    assert bm1002.credit == "3", f"Expected '3', got '{bm1002.credit}'"
    
    expected_mid = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$gvSubjects$ctl02$btnprintmidsem"
    expected_end = "ctl00$ctl00$ctl00$ContentPlaceHolder2$ContentPlaceHolder1$mainContent$gvSubjects$ctl02$btnprintendsem"
    assert bm1002.mid_sem_target == expected_mid, f"Expected '{expected_mid}', got '{bm1002.mid_sem_target}'"
    assert bm1002.end_sem_target == expected_end, f"Expected '{expected_end}', got '{bm1002.end_sem_target}'"
    print("[OK] Assertion Passed: BM1002 details, LTP, credits, and postback targets are 100% correct!")

    # Find ME6134 (which has missing Mid Sem)
    me6134 = next((r for r in records if r.subject_code == "ME6134"), None)
    assert me6134 is not None, "ME6134 not found!"
    assert me6134.mid_sem_target is None, f"Expected Mid Sem target to be None, got '{me6134.mid_sem_target}'"
    assert me6134.end_sem_target is not None, "Expected End Sem target to be valid"
    print("[OK] Assertion Passed: ME6134 missing Mid-Sem target is correctly handled (set to None)!")

    # Find MN2103 (which has both missing)
    mn2103 = next((r for r in records if r.subject_code == "MN2103"), None)
    assert mn2103 is not None, "MN2103 not found!"
    assert mn2103.mid_sem_target is None, "Expected Mid Sem to be None"
    assert mn2103.end_sem_target is None, "Expected End Sem to be None"
    print("[OK] Assertion Passed: MN2103 missing both Mid and End Sem targets are correctly handled!")

    print("\nALL PARSER VERIFICATION TESTS PASSED SUCCESSFULLY! Flawless execution.")

if __name__ == "__main__":
    run_tests()
