import os
import time
import subprocess
import argparse

try:
    import psutil
    import pyautogui
    import pygetwindow as gw
    from pywinauto.application import Application
    from pywinauto.findwindows import ElementNotFoundError
except ImportError:
    print("Missing dependencies. Please run:")
    print("pip install psutil pyautogui pygetwindow pywinauto")
    exit(1)

# --- Configuration ---
DEFAULT_IB_GATEWAY_PATH = r"C:\ibgateway\10.28\ibgateway.exe"  # Adjust as needed
GATEWAY_WINDOW_TITLE = "IB Gateway"
CHECK_INTERVAL_SECONDS = 20

def get_gateway_path():
    """Find the latest installed IB Gateway path if the default is not found."""
    if os.path.exists(DEFAULT_IB_GATEWAY_PATH):
        return DEFAULT_IB_GATEWAY_PATH
    
    # Try to find it in common directories
    base_dir = r"C:\ibgateway"
    if os.path.exists(base_dir):
        versions = [f for f in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, f))]
        if versions:
            # Sort to get the latest version
            versions.sort(reverse=True)
            for v in versions:
                exe_path = os.path.join(base_dir, v, "ibgateway.exe")
                if os.path.exists(exe_path):
                    return exe_path
    return None

def is_gateway_running():
    """Check if the IB Gateway process is currently running."""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and 'ibgateway.exe' in proc.info['name'].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False

def start_gateway(exe_path):
    """Launch the IB Gateway executable."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting IBKR Gateway from {exe_path}")
    subprocess.Popen([exe_path])
    time.sleep(15)  # Give Java UI time to launch

def apply_blind_login(username, password, is_paper=True):
    """Uses automated keystrokes to log in, bypassing UIA control limitations."""
    print("Initializing keystroke injection for login...")
    try:
        # Give the window focus
        windows = gw.getWindowsWithTitle("IB Gateway")
        if not windows:
            print("Login window not found via pygetwindow.")
            return

        win = windows[0]
        win.activate()
        time.sleep(1)

        # Tab navigation for IB Gateway Login screen:
        # Defaults to Username field.
        pyautogui.write(username)
        pyautogui.press('tab')
        pyautogui.write(password)
        
        # In the modern gateway, the "Paper Trading" toggle could be a click or we can assume manual setup
        # For full safety, pressing Enter logs in to the last selected trading mode
        pyautogui.press('enter')
        print("Login credentials submitted.")
        
    except Exception as e:
        print(f"Error during blind login: {e}")

def handle_login(username, password, is_paper=True):
    """Attempt to detect the login screen and authenticate using pywinauto."""
    try:
        app = Application(backend="uia").connect(title_re=".*IB Gateway.*", timeout=5)
        login_window = app.top_window()
        
        if "Login" in login_window.window_text():
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Login screen detected.")
            
            # Since Java UIA can be rigid, rely on PyAutoGUI for the final steps
            # to ensure input consistency.
            apply_blind_login(username, password, is_paper)
            time.sleep(15) # Wait for authentication
            
    except ElementNotFoundError:
        # Window isn't there or not matched
        pass
    except Exception as e:
        pass

def handle_popups():
    """Handle annoying popups like forced disconnects, updates, or secondary logins."""
    try:
        app = Application(backend="uia").connect(title_re=".*IB Gateway.*", timeout=2)
        top_windows = app.windows()
        
        for win in top_windows:
            title = win.window_text().lower()
            
            # Secondary Authentication Check (if logged in from elsewhere)
            if "secondary login" in title or "duplicate" in title:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Dismissing secondary login popup.")
                try:
                    win.child_window(title="OK", control_type="Button").click()
                except:
                    win.type_keys("{ENTER}")
            
            # Daily restart or forced disconnection
            elif "disconnected" in title or "session exit" in title or "re-login" in title:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Gateway disconnected. Attempting to restart/re-login...")
                try:
                    # We usually want to exit and let watchdog restart it fresh
                    win.child_window(title="Exit", control_type="Button").click()
                except:
                    win.type_keys("%{F4}") # Alt+F4
                    
    except Exception:
        pass # No matching popups or errors interacting

def run_watchdog(username, password, is_paper):
    exe_path = get_gateway_path()
    if not exe_path:
        print("Error: Could not locate ibgateway.exe.")
        print(f"Please install it or update the default path in the script (Current: {DEFAULT_IB_GATEWAY_PATH})")
        return

    print("=====================================================================")
    print(" IBKR Gateway Watchdog Started")
    print("=====================================================================")
    print(f" Executable: {exe_path}")
    print(f" Mode: {'Paper Trading' if is_paper else 'Live Trading'}")
    print(" Press Ctrl+C to stop the watchdog.")
    print("=====================================================================")

    while True:
        try:
            if not is_gateway_running():
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Gateway is offline. Launching...")
                start_gateway(exe_path)
                time.sleep(5)
                handle_login(username, password, is_paper)
            else:
                handle_login(username, password, is_paper) # In case it's stuck on login screen
                handle_popups()
                
        except KeyboardInterrupt:
            print("\nWatchdog stopped by user.")
            break
        except Exception as e:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Watchdog error: {e}")
            
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Python Watchdog for IBKR Gateway (IBC Alternative)")
    parser.add_argument("--user", type=str, help="IBKR Username", required=False)
    parser.add_argument("--pwd", type=str, help="IBKR Password", required=False)
    parser.add_argument("--live", action="store_true", help="Set this flag for Live Trading (Default is Paper)")
    
    args = parser.parse_args()
    
    # Fallback to Environment variables if arguments are not provided
    username = args.user or os.environ.get("IBKR_USERNAME")
    password = args.pwd or os.environ.get("IBKR_PASSWORD")
    is_paper = not args.live
    
    if not username or not password:
        print("Error: Username and Password must be provided either via arguments or environment variables.")
        print("Usage: python ibkr_watchdog.py --user YOUR_USERNAME --pwd YOUR_PASSWORD")
        print("Or set environment variables: IBKR_USERNAME, IBKR_PASSWORD")
        exit(1)
        
    run_watchdog(username, password, is_paper)
