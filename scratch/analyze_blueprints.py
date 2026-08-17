import os
from bs4 import BeautifulSoup

def analyze_all_messages():
    path = r"C:\Users\mrara\OneDrive\Desktop\collegeclaw\debug_html\view-source_https___eapplication.nitrkl.ac.in_nitris_Student_Home_AllMessages.aspx.html"
    if not os.path.exists(path):
        print("AllMessages file not found!")
        return
        
    print("Analyzing AllMessages raw file...")
    
    # Try different encodings to be safe
    for encoding in ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'latin-1']:
        try:
            with open(path, 'r', encoding=encoding) as f:
                content = f.read()
            if "AllMessages.aspx" in content:
                print(f"-> Successfully loaded AllMessages using encoding: {encoding}")
                break
        except Exception:
            continue
    else:
        print("Failed to decode AllMessages file!")
        return

    # Parse Chrome's view-source markup
    # Chrome's view-source page places the actual code inside a <table> element.
    # Each row is a line, and line contents are in <td class="line-content"> or similar.
    # We can reconstruct the original HTML by joining the text of the line contents,
    # or by simply unescaping the page since Chrome might have escaped it.
    soup = BeautifulSoup(content, 'html.parser')
    
    # Let's reconstruct the original HTML from view-source tables
    lines = []
    for td in soup.find_all('td', class_='line-content'):
        lines.append(td.get_text())
    
    original_html = "\n".join(lines)
    if not original_html.strip():
        # Fallback: maybe it's already regular HTML
        original_html = content

    print(f"Reconstructed HTML length: {len(original_html)} bytes")
    
    # Now parse the reconstructed HTML
    clean_soup = BeautifulSoup(original_html, 'html.parser')
    
    # Search for GridView
    tables = clean_soup.find_all('table')
    print(f"Found {len(tables)} tables in the page.")
    for t in tables:
        t_id = t.get('id', '')
        t_class = t.get('class', '')
        if t_id or t_class:
            print(f"Table ID: '{t_id}', Class: {t_class}")
            
    # Look for links containing Message.aspx
    links = clean_soup.find_all('a')
    message_links = []
    for l in links:
        href = l.get('href', '')
        if 'Message.aspx' in href:
            message_links.append(href)
            
    print(f"Found {len(message_links)} links to Message.aspx. Examples:")
    for ml in message_links[:5]:
        print("  -", ml)

def main():
    report_lines = []
    
    # 1. Analyze AllMessages
    path_all = r"C:\Users\mrara\OneDrive\Desktop\collegeclaw\debug_html\view-source_https___eapplication.nitrkl.ac.in_nitris_Student_Home_AllMessages.aspx.html"
    if not os.path.exists(path_all):
        report_lines.append("AllMessages file not found!")
    else:
        report_lines.append("=== ALL MESSAGES FILE ANALYSIS ===")
        content = ""
        for encoding in ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'latin-1']:
            try:
                with open(path_all, 'r', encoding=encoding) as f:
                    content = f.read()
                if "AllMessages.aspx" in content:
                    report_lines.append(f"Loaded successfully using encoding: {encoding}")
                    break
            except Exception:
                continue
        
        soup = BeautifulSoup(content, 'html.parser')
        lines = [td.get_text() for td in soup.find_all('td', class_='line-content')]
        original_html = "\n".join(lines) if lines else content
        report_lines.append(f"Reconstructed HTML length: {len(original_html)} bytes")
        
        clean_soup = BeautifulSoup(original_html, 'html.parser')
        
        # GridView
        tables = clean_soup.find_all('table')
        report_lines.append(f"Found {len(tables)} tables in the page:")
        for t in tables:
            t_id = t.get('id', '')
            t_class = t.get('class', '')
            report_lines.append(f"  Table ID: '{t_id}', Class: {t_class}")
            
            # If it's a GridView, inspect headers
            rows = t.find_all('tr')
            if rows:
                headers = [th.get_text(strip=True) for th in rows[0].find_all(['th', 'td'])]
                report_lines.append(f"    First row cells/headers: {headers}")
                
        # Link buttons/Links
        links = clean_soup.find_all('a')
        message_links = []
        for l in links:
            href = l.get('href', '')
            text = l.get_text(strip=True)
            if 'Message.aspx' in href:
                message_links.append((text, href))
                
        report_lines.append(f"Found {len(message_links)} links to Message.aspx:")
        for text, href in message_links:
            report_lines.append(f"  - Text: '{text}', Href: '{href}'")

    # 2. Analyze Message Detail
    path_detail = r"C:\Users\mrara\OneDrive\Desktop\collegeclaw\debug_html\view-source_https___eapplication.nitrkl.ac.in_nitris_Student_Home_Message.aspx_i=Mjc2Mjk2NA3d-703j2p9j4TY%3d.html"
    if not os.path.exists(path_detail):
        report_lines.append("\nMessage detail file not found!")
    else:
        report_lines.append("\n=== MESSAGE DETAIL FILE ANALYSIS ===")
        content = ""
        for encoding in ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'latin-1']:
            try:
                with open(path_detail, 'r', encoding=encoding) as f:
                    content = f.read()
                if "Message.aspx" in content or "Mjc2" in content:
                    report_lines.append(f"Loaded successfully using encoding: {encoding}")
                    break
            except Exception:
                continue
                
        soup = BeautifulSoup(content, 'html.parser')
        lines = [td.get_text() for td in soup.find_all('td', class_='line-content')]
        original_html = "\n".join(lines) if lines else content
        report_lines.append(f"Reconstructed HTML length: {len(original_html)} bytes")
        
        clean_soup = BeautifulSoup(original_html, 'html.parser')
        
        # Details elements
        spans = clean_soup.find_all('span')
        report_lines.append(f"Found {len(spans)} span elements. Inspecting potential labels:")
        for s in spans:
            s_id = s.get('id', '')
            if s_id and ('lbl' in s_id or 'ContentPlaceHolder' in s_id):
                text_content = s.get_text(strip=True)[:100]
                report_lines.append(f"  Span ID: '{s_id}', Text: '{text_content}'")
                
        # Inspect form fields / Viewstate
        inputs = clean_soup.find_all('input')
        report_lines.append(f"Found {len(inputs)} input elements. Hidden fields:")
        for inp in inputs:
            inp_id = inp.get('id', '')
            inp_name = inp.get('name', '')
            inp_type = inp.get('type', '')
            if inp_type == 'hidden':
                report_lines.append(f"  Hidden Input ID: '{inp_id}', Name: '{inp_name}'")
                
        # Attachment links
        links = clean_soup.find_all('a')
        report_lines.append(f"Found {len(links)} anchors. Inspecting potential attachments:")
        for l in links:
            href = l.get('href', '')
            text = l.get_text(strip=True)
            if href and ('docs' in href or 'Attachment' in text or 'pdf' in href):
                report_lines.append(f"  Anchor Text: '{text}', Href: '{href}'")

    # Write report
    report_path = r"C:\Users\mrara\OneDrive\Desktop\collegeclaw\scratch\analysis_report.txt"
    with open(report_path, 'w', encoding='utf-8') as outf:
        outf.write("\n".join(report_lines))
    print(f"Analysis written successfully to: {report_path}")

if __name__ == '__main__':
    main()
