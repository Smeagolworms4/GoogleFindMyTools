import undetected_chromedriver as uc
import os
import shutil
import platform
import time


def find_chrome():
    """
    Chromium-first, compatible with Chrome.
    Override with:
      - GFM_CHROME_BIN=/path/to/chromium-or-chrome
      - CHROME_BIN=/path/to/chromium-or-chrome
    """
    env_bin = os.environ.get("GFM_CHROME_BIN") or os.environ.get("CHROME_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin

    possible_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/local/bin/google-chrome",
        "/opt/google/chrome/chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Chromium\Application\chrome.exe",
        r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in possible_paths:
        if os.path.exists(p):
            return p

    try:
        if platform.system() == "Windows":
            return shutil.which("chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
        return shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    except Exception as e:
        print(f"[ChromeDriver] Error while searching system paths: {e}")
        return None


def _cleanup_profile_locks(profile_dir: str | None):
    if not profile_dir:
        print(f"[cleanup] no profile_dir")
        return

    if not os.path.isdir(profile_dir):
        print(f"[cleanup] profile_dir is not a directory: {profile_dir!r}")
        return

    # Chromium met souvent les locks dans le dossier "Default"
    candidates = [profile_dir, os.path.join(profile_dir, "Default")]

    lock_files = ("SingletonLock", "SingletonCookie", "SingletonSocket", "Lock", "lockfile")

    for d in candidates:
        if not os.path.isdir(d):
            continue
        for fn in lock_files:
            p = os.path.join(d, fn)
            if os.path.lexists(p):
                try:
                    os.remove(p)
                    print(f"[cleanup] removed {p}")
                except Exception as e:
                    print(f"[cleanup] failed removing {p}: {e!r}")


def get_options():
    profile_dir = os.environ.get("GFM_CHROME_PROFILE_DIR", "/data/chrome")
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)

    chrome_options = uc.ChromeOptions()
    if profile_dir:
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")

    chrome_options.add_argument("--profile-directory=Default")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    # Container-friendly
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")

    return chrome_options


def _kill_browsers():
    try:
        if platform.system() == "Windows":
            os.system("taskkill /f /im chrome.exe >nul 2>&1")
            os.system("taskkill /f /im chromium.exe >nul 2>&1")
            os.system("taskkill /f /im chromedriver.exe >nul 2>&1")
        else:
            # important: tuer chromedriver/crashpad aussi, sinon le profil reste lock
            os.system("pkill -f chromedriver >/dev/null 2>&1 || true")
            os.system("pkill -f chrome_crashpad >/dev/null 2>&1 || true")
            os.system("pkill -f chromium >/dev/null 2>&1 || true")
            os.system("pkill -f chromium-browser >/dev/null 2>&1 || true")
            os.system("pkill -f google-chrome >/dev/null 2>&1 || true")
            os.system("pkill -f '\\bchrome\\b' >/dev/null 2>&1 || true")
        time.sleep(1.5)
    except Exception:
        pass


def create_driver():
    try:
        _kill_browsers()

        profile_dir = os.environ.get("GFM_CHROME_PROFILE_DIR")
        _cleanup_profile_locks(profile_dir)

        chrome_options = get_options()

        chrome_path = find_chrome()
        if chrome_path:
            chrome_options.binary_location = chrome_path
        else:
            print("[ChromeDriver] No Chromium/Chrome executable found in known paths.")

        driver_path = os.environ.get("GFM_CHROMEDRIVER_PATH") or ""
        if driver_path and os.path.exists(driver_path):
            driver = uc.Chrome(
                options=chrome_options,
                driver_executable_path=driver_path,
                version_main=None,
            )
        else:
            driver = uc.Chrome(options=chrome_options, version_main=None)

        print("[ChromeDriver] Browser started.")
        return driver

    except Exception as e:
        print(f"[ChromeDriver] Default ChromeDriver creation failed: {e}")
        print("[ChromeDriver] Trying headless mode as last resort...")

        chrome_options = get_options()
        chrome_options.add_argument("--headless=new")

        chrome_path = find_chrome()
        if chrome_path:
            chrome_options.binary_location = chrome_path

        driver_path = os.environ.get("GFM_CHROMEDRIVER_PATH") or ""
        if driver_path and os.path.exists(driver_path):
            return uc.Chrome(
                options=chrome_options,
                driver_executable_path=driver_path,
                version_main=None,
            )
        return uc.Chrome(options=chrome_options, version_main=None)


if __name__ == "__main__":
    create_driver()
