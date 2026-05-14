#!/usr/bin/env python3
"""
Diagnose email configuration and network connectivity issues.
"""
import os
import sys
import socket
import smtplib
from pathlib import Path

# Set up the path to import app
sys.path.insert(0, os.path.dirname(__file__))

from app import app

def check_env_variables():
    """Check email environment variables."""
    print("=" * 60)
    print("EMAIL CONFIGURATION")
    print("=" * 60)
    
    mail_server = app.config.get("MAIL_SERVER", "smtp.gmail.com")
    mail_port = int(app.config.get("MAIL_PORT", 587))
    mail_username = app.config.get("MAIL_USERNAME")
    mail_use_tls = bool(app.config.get("MAIL_USE_TLS"))
    mail_from = app.config.get("MAIL_FROM")
    
    print(f"MAIL_SERVER:   {mail_server}")
    print(f"MAIL_PORT:     {mail_port}")
    print(f"MAIL_USE_TLS:  {mail_use_tls}")
    print(f"MAIL_USERNAME: {mail_username if mail_username else '(NOT SET)'}")
    print(f"MAIL_FROM:     {mail_from if mail_from else '(NOT SET)'}")
    print(f"MAIL_PASSWORD: {'(SET)' if os.environ.get('MAIL_PASSWORD') else '(NOT SET)'}")
    
    return mail_server, mail_port, mail_use_tls


def check_dns_resolution(host):
    """Test if the hostname can be resolved via DNS."""
    print("\n" + "=" * 60)
    print("DNS RESOLUTION TEST")
    print("=" * 60)
    
    try:
        ip_address = socket.gethostbyname(host)
        print(f"✓ DNS resolution successful")
        print(f"  Hostname: {host}")
        print(f"  IP Address: {ip_address}")
        return True
    except socket.gaierror as e:
        print(f"✗ DNS resolution FAILED")
        print(f"  Error: {e}")
        print(f"  The system cannot resolve '{host}' to an IP address.")
        print(f"  Possible causes:")
        print(f"    - No internet connectivity")
        print(f"    - DNS server is not configured or unreachable")
        print(f"    - Incorrect hostname in MAIL_SERVER")
        return False


def check_network_connectivity(host, port):
    """Test if the SMTP server is reachable."""
    print("\n" + "=" * 60)
    print("NETWORK CONNECTIVITY TEST")
    print("=" * 60)
    
    try:
        print(f"Attempting to connect to {host}:{port}...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result == 0:
            print(f"✓ Network connectivity successful")
            print(f"  Successfully connected to {host}:{port}")
            return True
        else:
            error_code = result
            error_msg = os.strerror(error_code)
            print(f"✗ Network connectivity FAILED")
            print(f"  Error Code: {error_code}")
            print(f"  Error: {error_msg}")
            
            if error_code == 101:
                print(f"  Error 101 = Network is unreachable")
                print(f"  Possible causes:")
                print(f"    - No internet connection")
                print(f"    - System/container is isolated from network")
                print(f"    - Firewall is blocking outbound connections")
                print(f"    - SMTP server IP is unreachable")
            elif error_code == 111:
                print(f"  Error 111 = Connection refused")
                print(f"  Possible causes:")
                print(f"    - SMTP server is not running")
                print(f"    - Wrong port configured")
            elif error_code == 113:
                print(f"  Error 113 = No route to host")
                print(f"  Possible causes:")
                print(f"    - Network route to host doesn't exist")
                print(f"    - Server is offline")
            
            return False
    except socket.timeout:
        print(f"✗ Network connectivity FAILED (TIMEOUT)")
        print(f"  The connection attempt timed out after 5 seconds")
        print(f"  Possible causes:")
        print(f"    - Network is slow or unreliable")
        print(f"    - Firewall is dropping packets")
        print(f"    - SMTP server is not responding")
        return False
    except Exception as e:
        print(f"✗ Network connectivity FAILED")
        print(f"  Error: {e}")
        return False


def check_smtp_connection(host, port, use_tls):
    """Test SMTP connection without credentials."""
    print("\n" + "=" * 60)
    print("SMTP CONNECTION TEST")
    print("=" * 60)
    
    try:
        print(f"Attempting SMTP connection to {host}:{port} (TLS={use_tls})...")
        
        if use_tls:
            server = smtplib.SMTP(host, port, timeout=5)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=5)
        
        response = server.helo()
        server.quit()
        
        print(f"✓ SMTP connection successful")
        print(f"  Server response: {response}")
        return True
    except socket.timeout:
        print(f"✗ SMTP connection FAILED (TIMEOUT)")
        print(f"  The connection attempt timed out")
        return False
    except Exception as e:
        print(f"✗ SMTP connection FAILED")
        print(f"  Error: {type(e).__name__}: {e}")
        return False


def check_firewall():
    """Check for common firewall issues."""
    print("\n" + "=" * 60)
    print("FIREWALL & NETWORK CONFIGURATION")
    print("=" * 60)
    
    # Try to detect if running in a container/restricted environment
    in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    
    print(f"Running in container: {in_container}")
    
    if in_container:
        print("  ⚠ Container detected")
        print("  Containers may have restricted network access")
        print("  Check container network configuration")
    
    # Check for common local SMTP servers for development
    local_smtp_servers = ["localhost:1025", "localhost:1587", "127.0.0.1:1025"]
    print(f"\nCommon local SMTP alternatives (for development):")
    for smtp in local_smtp_servers:
        host, port = smtp.split(":")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, int(port)))
            sock.close()
            if result == 0:
                print(f"  ✓ {smtp} is available (MailHog/smtp4dev?)")
            else:
                print(f"  ✗ {smtp} is not available")
        except:
            print(f"  ✗ {smtp} is not available")


def suggest_solutions():
    """Print suggested solutions."""
    print("\n" + "=" * 60)
    print("SUGGESTED SOLUTIONS")
    print("=" * 60)
    
    print("\n1. CHECK INTERNET CONNECTIVITY:")
    print("   - Verify your system/container has internet access")
    print("   - Try: ping 8.8.8.8 (Google DNS)")
    print("   - If ping fails, network access is blocked")
    
    print("\n2. USE LOCAL SMTP SERVER (FOR DEVELOPMENT):")
    print("   - Set MAIL_SERVER=localhost or 127.0.0.1")
    print("   - Set MAIL_PORT=1025 (MailHog) or 1587 (smtp4dev)")
    print("   - Set MAIL_DEV_FALLBACK=1 (uses unauthenticated SMTP)")
    print("   - Start MailHog: mailhog or docker run -p 1025:1025 -p 8025:8025 mailhog/mailhog")
    
    print("\n3. CHECK FIREWALL RULES:")
    print("   - Ensure outbound SMTP (port 587, 465, 25) is allowed")
    print("   - If on cloud platform, check security groups/network ACLs")
    
    print("\n4. ALTERNATIVE EMAIL PROVIDERS:")
    print("   - Gmail:   MAIL_SERVER=smtp.gmail.com, MAIL_PORT=587")
    print("   - SendGrid: MAIL_SERVER=smtp.sendgrid.net, MAIL_PORT=587")
    print("   - AWS SES:  MAIL_SERVER=email-smtp.REGION.amazonaws.com, MAIL_PORT=587")
    
    print("\n5. ENVIRONMENT VARIABLES:")
    print("   - Verify MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD are set")
    print("   - In production (Render): Set these in the dashboard")
    print("   - In development: Add to .env file")
    
    print("\n6. DEBUG MODE:")
    print("   - Set MAIL_DEBUG=1 to see detailed SMTP protocol exchange")
    print("   - Set MAIL_TIMEOUT_SECONDS=10 to increase timeout")
    print("   - Check application logs for detailed error messages")


def main():
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 58 + "║")
    print("║" + "EMAIL DIAGNOSTIC TOOL".center(58) + "║")
    print("║" + " " * 58 + "║")
    print("╚" + "=" * 58 + "╝")
    print()
    
    # Get config
    mail_server, mail_port, mail_use_tls = check_env_variables()
    
    # DNS test
    dns_ok = check_dns_resolution(mail_server)
    
    # Network test
    net_ok = False
    if dns_ok:
        net_ok = check_network_connectivity(mail_server, mail_port)
    
    # SMTP test
    if net_ok:
        check_smtp_connection(mail_server, mail_port, mail_use_tls)
    
    # Firewall check
    check_firewall()
    
    # Solutions
    suggest_solutions()
    
    print("\n" + "=" * 60)
    if dns_ok and net_ok:
        print("✓ Network connectivity looks good!")
        print("  Check email credentials and SMTP authentication.")
    else:
        print("✗ Network connectivity issues detected.")
        print("  See suggested solutions above.")
    print("=" * 60)
    print()


if __name__ == '__main__':
    main()
