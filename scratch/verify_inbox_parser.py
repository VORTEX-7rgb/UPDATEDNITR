import os
import sys
from datetime import datetime

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.nitris.parser import parse_messages_list_html, parse_message_detail_html

def verify_all_messages():
    print("--- Verifying parse_messages_list_html ---")
    file_path = os.path.join(
        os.path.dirname(__file__), 
        "../debug_html/view-source_https___eapplication.nitrkl.ac.in_nitris_Student_Home_AllMessages.aspx.html"
    )
    
    if not os.path.exists(file_path):
        print(f"Error: Mock file not found at {file_path}")
        return
        
    with open(file_path, "r", encoding="utf-8") as f:
        html = f.read()
        
    messages = parse_messages_list_html(html)
    print(f"Successfully parsed {len(messages)} messages from list!")
    
    for idx, msg in enumerate(messages[:3], start=1):
        print(f"\nMessage {idx}:")
        print(f"  Portal Message ID: {msg['portal_message_id']}")
        print(f"  Token: {msg['token'][:25]}...")
        print(f"  Sender: {msg['sender']}")
        print(f"  Subject: {msg['subject']}")
        print(f"  Sent On: {msg['sent_on']}")
        
    assert len(messages) > 0, "No messages parsed!"
    print("\n[PASS] parse_messages_list_html Verification PASS!")

def verify_message_detail():
    print("\n--- Verifying parse_message_detail_html ---")
    file_path = os.path.join(
        os.path.dirname(__file__),
        "../debug_html/view-source_https___eapplication.nitrkl.ac.in_nitris_Student_Home_Message.aspx_i=Mjc2Mjk2NA3d-703j2p9j4TY%3d.html"
    )
    
    if not os.path.exists(file_path):
        print(f"Error: Mock file not found at {file_path}")
        return
        
    with open(file_path, "r", encoding="utf-8") as f:
        html = f.read()
        
    detail = parse_message_detail_html(html)
    print("Successfully parsed message detail:")
    print(f"  Sender: {detail['sender']}")
    print(f"  Subject: {detail['subject']}")
    print(f"  Sent On (Raw): {detail['sent_on_str']}")
    print(f"  Attachment URL: {detail['attachment_url']}")
    print("  Body Snippet:")
    body_snippet = detail['body'][:300] + "..." if len(detail['body']) > 300 else detail['body']
    print(f"    {body_snippet}")
    
    assert detail['sender'] != "Unknown Sender", "Failed to parse sender!"
    assert detail['attachment_url'] is not None, "Failed to parse attachment URL!"
    print("\n[PASS] parse_message_detail_html Verification PASS!")

if __name__ == "__main__":
    verify_all_messages()
    verify_message_detail()
