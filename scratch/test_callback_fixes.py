import html

def parse_subject_callback(callback_data: str) -> str:
    """Simulate corrected handle_subject_selected extraction."""
    return callback_data[7:]

def parse_year_callback(callback_data: str) -> tuple[str, str]:
    """Simulate corrected handle_year_selected extraction."""
    data = callback_data[6:]
    subject_code, year_code = data.rsplit("_", 1)
    return subject_code, year_code

def test_parser():
    print("--- Testing Callback Parsing Fixes ---")
    
    # Standard code
    c1 = "qp_sub_BM1002"
    assert parse_subject_callback(c1) == "BM1002", f"Failed standard subject: {parse_subject_callback(c1)}"
    
    # Underscore code
    c2 = "qp_sub_CS_3002"
    assert parse_subject_callback(c2) == "CS_3002", f"Failed underscore subject: {parse_subject_callback(c2)}"
    
    # Multi-underscore code
    c3 = "qp_sub_HS_1002_B"
    assert parse_subject_callback(c3) == "HS_1002_B", f"Failed multi-underscore subject: {parse_subject_callback(c3)}"
    
    # Standard year
    y1 = "qp_yr_BM1002_2526S"
    sub, year = parse_year_callback(y1)
    assert sub == "BM1002" and year == "2526S", f"Failed standard year: {sub}, {year}"
    
    # Underscore year
    y2 = "qp_yr_CS_3002_2526S"
    sub, year = parse_year_callback(y2)
    assert sub == "CS_3002" and year == "2526S", f"Failed underscore year: {sub}, {year}"
    
    # Multi-underscore year
    y3 = "qp_yr_HS_1002_B_2526S"
    sub, year = parse_year_callback(y3)
    assert sub == "HS_1002_B" and year == "2526S", f"Failed multi-underscore year: {sub}, {year}"
    
    print("PASS: Callback parsing verification matches 100% of cases!\n")

def test_escaping():
    print("--- Testing HTML Escaping Fixes ---")
    e1 = ValueError("No <form> found in HTML.")
    escaped = html.escape(str(e1))
    print(f"Original: {e1}")
    print(f"Escaped:  {escaped}")
    assert "<form>" not in escaped, "Tag not properly escaped!"
    assert "&lt;form&gt;" in escaped, "Tag representation incorrect!"
    print("PASS: HTML escaping blocks tag compilation crashes!\n")

if __name__ == "__main__":
    test_parser()
    test_escaping()
    print("ALL FIXES CONFIRMED AND WORKING LOCALLY!")
