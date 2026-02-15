# preinstall_driver.py
import os
import sys
import shutil
import traceback

def _p(msg: str):
    print(msg, flush=True)

def main():
    # Cache UC (là où il garde chromedriver téléchargé)
    cache_home = os.environ.get("XDG_CACHE_HOME", "/opt/uc-cache")
    os.makedirs(cache_home, exist_ok=True)

    # Forcer binaire navigateur (chromium recommandé)
    chrome_bin = os.environ.get("GFM_CHROME_BIN", "/usr/bin/chromium")
    # Si déjà fourni (ex: /usr/bin/chromedriver)
    forced_driver = os.environ.get("GFM_CHROMEDRIVER_PATH", "")

    # 1) Si chromedriver système existe, rien à télécharger
    if forced_driver and os.path.exists(forced_driver):
        _p(f"[preinstall] Using existing chromedriver: {forced_driver}")
        return 0
    if os.path.exists("/usr/bin/chromedriver"):
        _p("[preinstall] Found system /usr/bin/chromedriver (no download needed).")
        return 0

    # 2) Sinon, on force UC à télécharger chromedriver
    _p("[preinstall] No system chromedriver found. Forcing undetected_chromedriver download...")

    try:
        import undetected_chromedriver as uc

        opts = uc.ChromeOptions()
        if os.path.exists(chrome_bin):
            opts.binary_location = chrome_bin

        # headless pour build-time (pas besoin de X)
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        # NOTE: ne PAS donner driver_executable_path => UC va télécharger le driver
        driver = uc.Chrome(options=opts, version_main=None)
        driver.get("about:blank")
        driver.quit()

        _p(f"[preinstall] UC driver download done. Cache: {cache_home}")
        return 0

    except Exception as e:
        _p("[preinstall] FAILED to download driver via UC.")
        _p(str(e))
        _p(traceback.format_exc())

    # 3) Dernier fallback: appeler ton chrome_driver.py (si tu veux)
    try:
        _p("[preinstall] Fallback: importing chrome_driver.create_driver() ...")
        # ajuste l'import si le fichier est ailleurs
        import chrome_driver
        d = chrome_driver.create_driver()
        try:
            d.get("about:blank")
        except Exception:
            pass
        try:
            d.quit()
        except Exception:
            pass
        _p("[preinstall] chrome_driver fallback OK.")
        return 0
    except Exception as e:
        _p("[preinstall] chrome_driver fallback FAILED.")
        _p(str(e))
        _p(traceback.format_exc())
        return 1

if __name__ == "__main__":
    sys.exit(main())
